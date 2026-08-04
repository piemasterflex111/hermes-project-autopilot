#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RELEASE_DIR=${1:?usage: replay_release.sh <release-directory> [destination]}
DEST=${2:-/tmp/hermes-project-autopilot-release-replay}
if [[ "$RELEASE_DIR" != /* ]]; then RELEASE_DIR="$ROOT/$RELEASE_DIR"; fi
MANIFEST="$RELEASE_DIR/release.json"
[[ -f "$MANIFEST" ]] || { echo "missing release manifest: $MANIFEST" >&2; exit 1; }
readarray -t META < <(python3 - "$MANIFEST" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))
print(p["upstream_repository"])
print(p["base_commit"])
print(p["source_head_commit"])
print(p["expected_final_tree"])
print(p["patch_count"])
PY
)
UPSTREAM=${META[0]}; BASE=${META[1]}; SOURCE_HEAD=${META[2]}; EXPECTED_TREE=${META[3]}; EXPECTED_PATCH_COUNT=${META[4]}
mapfile -t PATCHES < <(find "$RELEASE_DIR/patches" -maxdepth 1 -type f -name "*.patch" -print | sort)
[[ ${#PATCHES[@]} -eq $EXPECTED_PATCH_COUNT ]] || { echo "patch count mismatch" >&2; exit 1; }
rm -rf "$DEST"
git clone --quiet "$UPSTREAM" "$DEST"
git -C "$DEST" checkout --quiet "$BASE"
git -C "$DEST" config user.name "Autopilot release replay"
git -C "$DEST" config user.email "autopilot-replay@users.noreply.github.com"
git -C "$DEST" am --committer-date-is-author-date --whitespace=nowarn "${PATCHES[@]}"
ACTUAL_HEAD=$(git -C "$DEST" rev-parse HEAD)
ACTUAL_TREE=$(git -C "$DEST" rev-parse "HEAD^{tree}")
[[ "$ACTUAL_TREE" == "$EXPECTED_TREE" ]] || { echo "tree mismatch: expected $EXPECTED_TREE, got $ACTUAL_TREE" >&2; exit 1; }
echo "Release replay succeeded: tree $ACTUAL_TREE (source head $SOURCE_HEAD; replay head $ACTUAL_HEAD)"
