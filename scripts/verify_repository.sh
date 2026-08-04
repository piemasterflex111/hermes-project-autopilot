#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
python3 scripts/verify_snapshot.py
python3 scripts/verify_release_evidence.py
python3 scripts/verify_compatibility_release.py
python3 scripts/verify_runtime_repair_release.py
python3 scripts/verify_ha_one_command_release.py
python3 scripts/scan_publication.py
python3 -m unittest discover -s tests -v
