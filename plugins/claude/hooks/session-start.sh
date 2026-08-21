#!/usr/bin/env bash
# SessionStart: converge svg-mcp's MCP registration to match the environment, and
# keep one warm svg-mcp behind it when we own the registration.
#
# See mcp-companion's docs/designs/backend-self-registration.md. The switch is a
# GLOBAL toggle — set once in zshenv, never varied per session (the user-scope MCP
# registry it drives is global, so two sessions disagreeing would thrash):
#
#   MCP_COMBINER=1                   a combiner serves my MCPs -> don't register
#   MCP_COMBINER_SERVES_SVG_MCP=0/1  per-backend override (wins)
#   (nothing set)                    standalone -> register + warm svg-mcp
#
# Both branches mutate, so this converges either way: setting the switch flips to
# combiner and removes our entry, unsetting it flips back and re-adds. The env is the
# source of truth, not the registry.
#
# WHY HTTP + sharedserver rather than the old per-session stdio `uv run`:
#   * the registered URL is stable across plugin versions. A stdio command would
#     have to bake ${CLAUDE_PLUGIN_ROOT} — a VERSIONED path — into global config,
#     going stale on every plugin update.
#   * one warm process is shared by every session instead of paying svg-mcp's cold
#     start (numpy + pillow) per session.
# This is safe because svg-mcp partitions state per MCP session: _SESSION_STORES is
# a WeakKeyDictionary[ServerSession, DocumentStore], so each connection gets its own
# documents exactly as separate stdio processes did. (Behind a combiner the same
# isolation needs `isolate: true`, which opens a distinct upstream session per chat.)
#
# WHICH svg-mcp RUNS:
#   default              uvx svg-mcp@<plugin version>   (published release, pinned)
#   SVG_MCP_DEV=<path>   uv run --project <path>        (a dev checkout)
#   SVG_MCP_DEV=1        uv run --project <plugin dir>  (the bundled copy)
set -euo pipefail
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NAME=svg-mcp
PORT="${SVG_MCP_PORT:-7731}"
URL="http://127.0.0.1:${PORT}/mcp"

# Nudge the session to author/edit diagrams and SVGs through svg-mcp's tools rather than
# hand-writing raw SVG XML. Emitted as SessionStart additionalContext on EVERY exit path
# (the combiner and missing-binary branches exit early, so a trap is used instead of a
# tail fall-through), mirroring the sibling cribsheet plugin's instructions.txt pattern.
# Canonical source: CLAUDE.md.example at the repo root. instructions.txt is a
# committed COPY of it, re-synced by scripts/bump-version.sh — never a symlink:
# marketplace installs copy the plugin subtree into a cache with no repo root,
# where a ../../ symlink dangles and this hook silently emits nothing (that bit
# mcp-companion 0.10.x; whether an install dereferences or preserves the link is
# undocumented copy behavior).
# stdout IS the SessionStart payload, so it must carry exactly ONE JSON object.
# Warnings go inside it as `systemMessage`: printing them separately produced two
# concatenated objects and one was silently dropped. (SessionStart stderr is invisible
# at exit 0, so stderr alone would not be seen either.)
_warnings=""
warn() {
  _warnings="${_warnings}${_warnings:+ }$1"
  echo "$1" >&2
}

_emit_instructions() {
  local txt="$dir/instructions.txt" ctx=""
  [[ -f "$txt" ]] && ctx="$(cat "$txt")"
  [[ -z "$ctx" && -z "$_warnings" ]] && return 0
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg ctx "$ctx" --arg sys "$_warnings" \
      '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}
       + (if $sys == "" then {} else {systemMessage:$sys} end)'
  else
    # Pure-bash JSON escaping. Backslash first (it escapes everything after), newline
    # last (so the \n it introduces is not re-escaped), then delete raw C0 controls —
    # JSON forbids all of U+0000–U+001F, and one stray byte would invalidate the
    # envelope and lose the instructions AND the warnings.
    local ctx_e="$ctx" sys_e="$_warnings" f s
    for f in ctx_e sys_e; do
      s="${!f}"
      s=${s//\\/\\\\}; s=${s//\"/\\\"}
      s=${s//$'\t'/\\t}; s=${s//$'\r'/\\r}; s=${s//$'\n'/\\n}
      s=${s//[$'\x01'-$'\x1f']/}
      printf -v "$f" '%s' "$s"
    done
    if [[ -n "$_warnings" ]]; then
      printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"},"systemMessage":"%s"}\n' "$ctx_e" "$sys_e"
    else
      printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$ctx_e"
    fi
  fi
}
trap _emit_instructions EXIT

_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    ''|0|false|no|off) return 1 ;;
    *) return 0 ;;
  esac
}

# Does a combiner serve $1? The per-backend override wins over the global switch.
combiner_serves() {
  local name per per_set
  name=$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')
  eval "per=\${MCP_COMBINER_SERVES_$name-}"
  eval "per_set=\${MCP_COMBINER_SERVES_$name+set}"
  if [ -n "$per_set" ]; then _truthy "$per"; return; fi
  _truthy "${MCP_COMBINER-}"
}

# Is $NAME already in the user-scope MCP config?
#
# The fast path reads the config JSON directly (~35ms) because this runs on EVERY
# session start; `claude mcp get` is authoritative but costs ~1.7s, which in steady
# state is pure waste. This is only the CHECK — every mutation still goes through the
# supported CLI. Exit 2 ("can't tell") falls back to the slow-but-correct probe
# rather than guessing "absent", which would re-add and reload MCP every session.
_registered() {
  local rc=0
  python3 -c '
import json, os, sys
try:
    cands = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        cands.append(os.path.join(os.path.expanduser(cfg), ".claude.json"))
    cands += [os.path.expanduser("~/.claude.json"),
              os.path.expanduser("~/.config/claude/.claude.json")]
    for p in cands:
        if os.path.exists(p):
            with open(p) as fh:
                d = json.load(fh)
            sys.exit(0 if sys.argv[1] in (d.get("mcpServers") or {}) else 1)
    sys.exit(2)
except Exception:
    sys.exit(2)
' "$NAME" || rc=$?
  if [ "$rc" -le 1 ]; then return "$rc"; fi
  claude mcp get "$NAME" >/dev/null 2>&1
}

# The argv that serves svg-mcp: a pinned published release by default, or a local
# checkout when SVG_MCP_DEV is set. Pinning to the PLUGIN's own version keeps the
# marketplace version and the served code in lockstep — note this means every plugin
# version must have a matching PyPI release, or uvx cannot resolve it.
_version_ge() { # _version_ge A B -> success when dotted-numeric prefix of A >= B
  local a b i x y
  local IFS=.
  read -r -a a <<<"${1%%[!0-9.]*}"
  read -r -a b <<<"${2%%[!0-9.]*}"
  for i in 0 1 2; do
    x="${a[i]:-0}"; y="${b[i]:-0}"
    ((10#${x:-0} > 10#${y:-0})) && return 0
    ((10#${x:-0} < 10#${y:-0})) && return 1
  done
  return 0
}

_serve_argv() {
  local project ver
  if [ -n "${SVG_MCP_DEV:-}" ]; then
    if [ -d "$SVG_MCP_DEV" ]; then project="$SVG_MCP_DEV"; else project="$dir"; fi
    printf '%s\0' uv run --project "$project" svg-mcp \
      --transport streamable-http --port "$PORT"
    return
  fi
  ver=$(python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh)["version"])
except Exception:
    print("")
' "$dir/.claude-plugin/plugin.json" 2>/dev/null || true)

  # An svg-mcp already on PATH wins when it is at least the version this plugin ships
  # against — it is the user's own `uv tool install`, and costs no fetch. Previously
  # this went straight to uvx, silently ignoring it. Too old and we fall through to the
  # pinned release rather than limp, so staleness self-heals.
  if command -v svg-mcp >/dev/null 2>&1; then
    local path_ver
    path_ver="$(svg-mcp --version 2>/dev/null | awk '{print $NF}')"
    if [ -n "$path_ver" ] && [ -n "$ver" ] && _version_ge "$path_ver" "$ver"; then
      printf '%s\0' svg-mcp --transport streamable-http --port "$PORT"
      return
    fi
  fi

  if [ -n "$ver" ]; then
    printf '%s\0' uvx "svg-mcp@${ver}" --transport streamable-http --port "$PORT"
  else
    printf '%s\0' uvx svg-mcp --transport streamable-http --port "$PORT"
  fi
}

# All `claude`/sharedserver output is silenced: this hook's stdout IS the
# SessionStart JSON payload, and a stray line would corrupt it.
if combiner_serves "$NAME"; then
  # The combiner is the MCP. Ensure we are not registered alongside it, and do not
  # start (or sync) anything — the combiner owns svg-mcp's lifecycle.
  if _registered; then
    claude mcp remove "$NAME" --scope user >/dev/null 2>&1 || true
  fi
  exit 0
fi

# Standalone: ensure we are registered (with the inbound-auth bearer when
# SVG_MCP_AUTH_TOKEN is set), and keep one warm svg-mcp behind the URL.
#
# This hook runs with the FULL environment — SessionStart hooks are NOT subject to
# Claude Code's headersHelper env-redaction (which strips *TOKEN*-named vars from a
# per-connection helper) — so it can read SVG_MCP_AUTH_TOKEN and bake a STATIC
# `Authorization: Bearer <token>` header into the user-scope entry via `claude mcp
# add -H`. No headersHelper, so the redaction that bites the combiner does not apply.
# The backend enforces the SAME token (server.py resolves SVG_MCP_AUTH_TOKEN and gates
# /mcp), and sharedserver inherits this hook's env — so set the token before launching
# `claude` and the client header and the server gate light up together; unset it and
# both stay open. Steady state is zero-write; a mismatch (first run, token rotated,
# toggled on/off) reconciles with one remove+add, which is also the only path that
# reloads MCP.
_desired_auth="${SVG_MCP_AUTH_TOKEN:-}"

_svg_register() {
  if [ -n "$_desired_auth" ]; then
    claude mcp add --transport http "$NAME" "$URL" \
      -H "Authorization: Bearer $_desired_auth" --scope user >/dev/null 2>&1 || true
  else
    claude mcp add --transport http "$NAME" "$URL" --scope user >/dev/null 2>&1 || true
  fi
}

# 0 = registered with matching url AND Authorization (do nothing); 1 = needs
# (re)register; 2 = can't tell (fall back to a presence-only check). Comparing the
# header is what makes token rotation/toggle converge without re-adding every session.
_in_sync() {
  python3 -c '
import json, os, sys
name, url, want = sys.argv[1], sys.argv[2], sys.argv[3]
cands = []
cfg = os.environ.get("CLAUDE_CONFIG_DIR")
if cfg:
    cands.append(os.path.join(os.path.expanduser(cfg), ".claude.json"))
cands += [os.path.expanduser("~/.claude.json"),
          os.path.expanduser("~/.config/claude/.claude.json")]
for p in cands:
    if os.path.exists(p):
        try:
            with open(p) as fh:
                d = json.load(fh)
        except Exception:
            sys.exit(2)
        srv = (d.get("mcpServers") or {}).get(name)
        if not srv:
            sys.exit(1)
        if srv.get("url") != url:
            sys.exit(1)
        cur = (srv.get("headers") or {}).get("Authorization") or ""
        sys.exit(0 if cur == want else 1)
sys.exit(2)
' "$NAME" "$URL" "${_desired_auth:+Bearer $_desired_auth}"
}

# NB: `set -e` is in effect — capture the non-zero return via `||`, never a bare
# `_in_sync; rc=$?` (that would exit the hook before rc is read).
_sync_rc=0
_in_sync || _sync_rc=$?
if [ "$_sync_rc" -eq 1 ]; then
  claude mcp remove "$NAME" --scope user >/dev/null 2>&1 || true
  _svg_register
elif [ "$_sync_rc" -eq 2 ]; then
  _registered || _svg_register
fi

# bin/sharedserver resolves $SHAREDSERVER_BIN -> PATH -> standard dirs and downloads a
# release when none is usable, so nothing needs installing by hand. It is vendored
# byte-identical from georgeharker/sharedserver by scripts/sync-vendored.sh; per-repo
# policy lives in bin/sharedserver.conf beside it.
ss="$dir/bin/sharedserver"
if [[ ! -x "$ss" ]]; then
  warn 'svg-mcp: the bundled bin/sharedserver wrapper is missing or not executable — the svg-mcp backend will not start.'
  exit 0
fi
if [ -z "${SVG_MCP_DEV:-}" ] && ! command -v uvx >/dev/null 2>&1; then
  warn 'svg-mcp: `uvx` not on PATH — the svg-mcp backend will not start. Install uv: https://docs.astral.sh/uv/getting-started/installation/'
  exit 0
fi

argv=()
while IFS= read -r -d '' a; do argv+=("$a"); done < <(_serve_argv)
"$ss" use "$NAME" --pid "$PPID" --grace-period 1h -- "${argv[@]}" >/dev/null 2>&1 || true
exit 0
