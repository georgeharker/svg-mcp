#!/usr/bin/env bash
#
# Bump the version in lockstep across the Python package and the Claude Code
# plugin, then commit and tag.
#
# Single source of truth: this script writes ONE version into both
#   - pyproject.toml                       (version = "X.Y.Z")
#   - the plugin.json under .claude-plugin ("version": "X.Y.Z", auto-detected)
# and creates the annotated tag vX.Y.Z. (The marketplace manifest carries no
# version of its own — it points at the plugin, whose plugin.json is the one
# Claude Code reads.)
#
# Usage:
#   scripts/bump-version.sh 0.3.0        # set an explicit version
#   scripts/bump-version.sh patch        # bump the patch component
#   scripts/bump-version.sh minor        # bump the minor component, zero patch
#   scripts/bump-version.sh major        # bump the major component, zero minor+patch
#
# Options:
#   --no-tag       update + commit, but skip the git tag
#   --no-commit    update files only (implies --no-tag)
#   -n, --dry-run  print what would change; touch nothing
#
# It refuses to run on a dirty tree (unless --no-commit) so the version bump
# lands as its own clean commit. Pushing is left to you.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYPROJECT="$ROOT/pyproject.toml"

die() { echo "error: $*" >&2; exit 1; }

# Locate the plugin.json: either top-level .claude-plugin/plugin.json or one
# nested under plugins/*/.claude-plugin/plugin.json. Exactly one is expected.
mapfile -t _pj < <(find "$ROOT" -path '*/.claude-plugin/plugin.json' -not -path '*/node_modules/*' 2>/dev/null)
[ "${#_pj[@]}" -ge 1 ] || die "no .claude-plugin/plugin.json found under $ROOT"
[ "${#_pj[@]}" -eq 1 ] || die "multiple plugin.json found; edit this script to pick one:"$'\n'"$(printf '  %s\n' "${_pj[@]}")"
PLUGIN_JSON="${_pj[0]}"

do_commit=1
do_tag=1
dry_run=0
arg=""

for a in "$@"; do
  case "$a" in
    --no-tag)    do_tag=0 ;;
    --no-commit) do_commit=0; do_tag=0 ;;
    -n|--dry-run) dry_run=1 ;;
    -h|--help)   sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)          die "unknown option: $a" ;;
    *)           [ -z "$arg" ] || die "unexpected extra argument: $a"; arg="$a" ;;
  esac
done

[ -n "$arg" ] || die "need a version or bump type (X.Y.Z | patch | minor | major); see --help"
[ -f "$PYPROJECT" ]   || die "not found: $PYPROJECT"
[ -f "$PLUGIN_JSON" ] || die "not found: $PLUGIN_JSON"

# Read the current versions from each file.
py_cur="$(grep -E '^version *= *"' "$PYPROJECT" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
pl_cur="$(grep -E '"version" *:' "$PLUGIN_JSON" | head -1 | sed -E 's/.*"version" *: *"([^"]+)".*/\1/')"
[ -n "$py_cur" ] || die "could not read version from $PYPROJECT"
[ -n "$pl_cur" ] || die "could not read version from $PLUGIN_JSON"

# Baseline = the highest of the two current versions (they may have drifted).
highest() {
  printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1
}
base="$(highest "$py_cur" "$pl_cur")"

# Resolve the requested version.
case "$arg" in
  major|minor|patch)
    IFS=. read -r M m p <<<"$base"
    [[ "$M" =~ ^[0-9]+$ && "$m" =~ ^[0-9]+$ && "$p" =~ ^[0-9]+$ ]] \
      || die "baseline version '$base' is not numeric X.Y.Z; pass an explicit version"
    case "$arg" in
      major) M=$((M+1)); m=0; p=0 ;;
      minor) m=$((m+1)); p=0 ;;
      patch) p=$((p+1)) ;;
    esac
    new="$M.$m.$p"
    ;;
  *)
    [[ "$arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "'$arg' is not a valid X.Y.Z version"
    new="$arg"
    ;;
esac

tag="v$new"

echo "pyproject.toml : $py_cur"
echo "plugin.json    : $pl_cur   ($PLUGIN_JSON)"
echo "baseline (max) : $base"
echo "new version    : $new   (tag $tag)"

# Guard: don't move backwards unless explicitly re-setting the same number.
if [ "$(highest "$base" "$new")" = "$base" ] && [ "$new" != "$base" ]; then
  die "new version $new is lower than baseline $base; refusing to go backwards"
fi

if [ "$dry_run" = 1 ]; then
  echo "(dry run) no files changed"
  exit 0
fi

if [ "$do_commit" = 1 ]; then
  # Only the version files may be dirty going in — keep the bump commit clean.
  pj_rel="$(git -C "$ROOT" rev-parse --show-prefix >/dev/null 2>&1 && realpath --relative-to="$ROOT" "$PLUGIN_JSON")"
  if [ -n "$(git -C "$ROOT" status --porcelain -- ":!pyproject.toml" ":!$pj_rel")" ]; then
    die "working tree has unrelated changes; commit or stash them first"
  fi
fi

if git -C "$ROOT" rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  die "tag $tag already exists"
fi

# Rewrite in place.
sed -i -E "0,/^version *= *\"[^\"]+\"/s//version = \"$new\"/" "$PYPROJECT"
sed -i -E "0,/\"version\" *: *\"[^\"]+\"/s//\"version\": \"$new\"/" "$PLUGIN_JSON"

echo "updated pyproject.toml and plugin.json -> $new"

if [ "$do_commit" = 0 ]; then
  echo "files updated; skipped commit (--no-commit)"
  exit 0
fi

git -C "$ROOT" add "$PYPROJECT" "$PLUGIN_JSON"
git -C "$ROOT" commit -m "release: v$new — bump pyproject + plugin in lockstep"
echo "committed."

if [ "$do_tag" = 1 ]; then
  git -C "$ROOT" tag -a "$tag" -m "$new"
  echo "tagged $tag"
  echo
  echo "push with:  git push && git push origin $tag"
else
  echo "skipped tag (--no-tag)"
fi
