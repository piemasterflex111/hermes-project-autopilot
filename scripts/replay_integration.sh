#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST=${1:-/tmp/hermes-project-autopilot-replay}
PUBLIC_BASE=$(python3 -c 'import json; print(json.load(open("'"$ROOT"'/provenance/replay.json"))["public_base_commit"])')
EXPECTED_TREE=$(python3 -c 'import json; print(json.load(open("'"$ROOT"'/provenance/replay.json"))["expected_final_tree"])')

rm -rf "$DEST"
git clone --quiet https://github.com/NousResearch/hermes-agent.git "$DEST"
git -C "$DEST" checkout --quiet "$PUBLIC_BASE"
git -C "$DEST" config user.name "Autopilot replay"
git -C "$DEST" config user.email "autopilot-replay@users.noreply.github.com"
git -C "$DEST" am --whitespace=nowarn "$ROOT"/prerequisites/*.patch
git -C "$DEST" am --whitespace=nowarn "$ROOT"/patches/*.patch

ACTUAL_TREE=$(git -C "$DEST" rev-parse 'HEAD^{tree}')
if [[ "$ACTUAL_TREE" != "$EXPECTED_TREE" ]]; then
  echo "tree mismatch: expected $EXPECTED_TREE, got $ACTUAL_TREE" >&2
  exit 1
fi

python3 -m compileall -q   "$DEST/hermes_cli/missions_db.py"   "$DEST/hermes_cli/mission_service.py"   "$DEST/hermes_cli/missions.py"   "$DEST/hermes_cli/mission_gateway.py"   "$DEST/scripts/evaluate_project_autopilot.py"

echo "Integration replay succeeded: $ACTUAL_TREE"
