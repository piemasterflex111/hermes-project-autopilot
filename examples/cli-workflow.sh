#!/usr/bin/env bash
set -euo pipefail

MISSION_ID=$(hermes mission create "Repair the parser" \
  --outcome "All parser tests pass without disabled tests" \
  --verify "pytest -q tests/parser" \
  --allowed-root /work/parser \
  --allowed-path src \
  --allowed-path tests \
  --autonomy 3 \
  --repo /work/parser \
  --project parser-project \
  --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

hermes mission plan-auto "$MISSION_ID" --executor engineer --verifier reviewer
hermes mission start "$MISSION_ID"
hermes mission show "$MISSION_ID"
