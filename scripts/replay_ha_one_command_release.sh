#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST=${1:-/tmp/hermes-project-autopilot-ha-r3}
BASE_RELEASE="$ROOT/releases/hermes-0.20-runtime-repair-r2"
DELTA_RELEASE="$ROOT/releases/hermes-0.20-ha-one-command-r3"
"$ROOT/scripts/replay_runtime_repair_release.sh" "$DEST"
mapfile -t PATCHES < <(find "$DELTA_RELEASE/patches" -maxdepth 1 -type f -name '*.patch' -print | sort)
EXPECTED_COUNT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["patch_count"])' "$DELTA_RELEASE/release.json")
[[ ${#PATCHES[@]} -eq $EXPECTED_COUNT ]] || { echo 'ha delta patch count mismatch' >&2; exit 1; }
git -C "$DEST" am --committer-date-is-author-date --whitespace=nowarn "${PATCHES[@]}"
EXPECTED_TREE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expected_final_tree"])' "$DELTA_RELEASE/release.json")
ACTUAL_TREE=$(git -C "$DEST" rev-parse 'HEAD^{tree}')
[[ "$ACTUAL_TREE" == "$EXPECTED_TREE" ]] || { echo "tree mismatch: expected $EXPECTED_TREE, got $ACTUAL_TREE" >&2; exit 1; }
echo "ha one-command replay succeeded: tree $ACTUAL_TREE"
