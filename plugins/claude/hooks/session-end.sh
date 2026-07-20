#!/usr/bin/env bash
# SessionEnd: detach this session from the shared svg-mcp backend. sharedserver is
# refcounted, so svg-mcp keeps running while other sessions hold it and stops (after
# the grace period) once the last client leaves.
#
# Must resolve the switch exactly as session-start.sh does: when a combiner serves
# svg-mcp we never started the backend, so we must not unuse it — an unbalanced
# unuse would decrement a refcount we never took.
set -euo pipefail

NAME=svg-mcp

_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    ''|0|false|no|off) return 1 ;;
    *) return 0 ;;
  esac
}

combiner_serves() {
  local name per per_set
  name=$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')
  eval "per=\${MCP_COMBINER_SERVES_$name-}"
  eval "per_set=\${MCP_COMBINER_SERVES_$name+set}"
  if [ -n "$per_set" ]; then _truthy "$per"; return; fi
  _truthy "${MCP_COMBINER-}"
}

# Combiner-served: we never took a reference, so we must not release one.
combiner_serves "$NAME" && exit 0

# The same vendored wrapper session-start.sh used. It must resolve to the same binary
# that took the reference, or the detach matches nothing and the refcount leaks until
# sharedserver's dead-client poller reclaims it.
ss="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bin/sharedserver"
[[ -x "$ss" ]] && "$ss" unuse "$NAME" --pid "$PPID" >/dev/null 2>&1 || true
exit 0
