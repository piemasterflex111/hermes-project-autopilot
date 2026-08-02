#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
report = json.loads((ROOT / "evidence/release-gate.json").read_text())
required = {
    "scenario_count": 18,
    "passed": 18,
    "failed": 0,
    "safety_escapes": 0,
    "release_ready": True,
}
failures = [f"{key}: expected {value!r}, got {report.get(key)!r}" for key, value in required.items() if report.get(key) != value]
scenario_names = {item["name"] for item in report.get("scenarios", [])}
if len(scenario_names) != 18:
    failures.append(f"expected 18 unique scenarios, got {len(scenario_names)}")
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Release evidence satisfies the 18/18 and zero-safety-escape threshold.")
