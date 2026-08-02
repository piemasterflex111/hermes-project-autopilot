#!/usr/bin/env python3
"""Run the Project Autopilot v1 safety release gate.

Each scenario is a focused, deterministic acceptance test. The report is JSON
so a human, Continuous Integration system, or Hermes itself can decide whether
v1 is safe to release. Any failed safety scenario counts as a safety escape and
keeps ``release_ready`` false.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCENARIOS = (
    ("mission_success", "tests/hermes_cli/test_missions.py::test_controller_tick_auto_verifies_and_commits_level_four", False, 0),
    ("false_success_prevention", "tests/hermes_cli/test_missions.py::test_fail_closed_verifier_and_local_commit_gate", True, 0),
    ("scope_escape_prevention", "tests/hermes_cli/test_missions.py::test_scope_violation_blocks_verification", True, 0),
    ("source_checkout_preservation", "tests/hermes_cli/test_missions.py::test_prepare_isolates_worktree_and_preserves_source_checkout", True, 0),
    ("registered_clean_project_gate", "tests/hermes_cli/test_missions.py::test_public_creation_requires_registered_clean_project", True, 0),
    ("explicit_commit_authority", "tests/hermes_cli/test_missions.py::test_level_four_requires_explicit_local_commit_authority", True, 0),
    ("operator_deny_and_retry", "tests/hermes_cli/test_missions.py::test_operator_deny_and_safe_retry", False, 1),
    ("restart_recovery", "tests/hermes_cli/test_missions.py::test_predispatch_recovery_blocks_orphaned_intent_before_respawn", True, 0),
    ("rollback_success", "tests/hermes_cli/test_missions.py::test_pause_cancel_and_verified_rollback", True, 0),
    ("git_identity_tamper_prevention", "tests/hermes_cli/test_mission_git_integrity.py::test_gitdir_identity_tamper_blocks_control_mutation[commit]", True, 0),
    ("rollback_ref_tamper_prevention", "tests/hermes_cli/test_mission_git_integrity.py::test_moved_rollback_ref_is_rejected_before_reset", True, 0),
    ("graph_mutation_prevention", "tests/hermes_cli/test_mission_graph_integrity.py::test_generic_kanban_structural_mutators_reject_mission_cards", True, 0),
    ("sandbox_escape_prevention", "tests/tools/test_mission_docker_scope.py::test_mission_config_scrubs_profile_docker_escape_vectors", True, 0),
    ("network_scope_prevention", "tests/hermes_cli/test_missions.py::test_execution_rejects_unenforceable_network_scope", True, 0),
    ("gateway_action_replay_prevention", "tests/hermes_cli/test_mission_gateway.py::test_authorized_action_is_one_use_and_records_evidence", True, 1),
    ("gateway_operator_binding", "tests/hermes_cli/test_mission_gateway.py::test_cross_identity_rejection_does_not_consume_token", True, 0),
    ("gateway_expiry_fail_closed", "tests/hermes_cli/test_mission_gateway.py::test_expired_and_malformed_tokens_fail_closed", True, 0),
    ("mission_detail_surface_contract", "tests/plugins/test_kanban_missions.py::test_create_list_and_lifecycle_actions", False, 0),
)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    default_python = root / "venv" / "bin" / "python"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the JSON report to this path")
    parser.add_argument(
        "--pytest",
        type=Path,
        default=default_python if default_python.is_file() else Path(sys.executable),
        help="Python executable containing pytest (defaults to the repository venv)",
    )
    args = parser.parse_args()

    results: list[dict[str, object]] = []
    started = time.monotonic()
    for name, nodeid, safety_critical, human_interventions in SCENARIOS:
        scenario_started = time.monotonic()
        proc = subprocess.run(
            [str(args.pytest), "-m", "pytest", "-q", nodeid],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        results.append(
            {
                "name": name,
                "nodeid": nodeid,
                "passed": proc.returncode == 0,
                "safety_critical": safety_critical,
                "human_interventions": human_interventions,
                "completion_time_seconds": round(time.monotonic() - scenario_started, 3),
                "exit_code": proc.returncode,
                "output_tail": "\n".join(proc.stdout.splitlines()[-12:]),
            }
        )

    failed = [item for item in results if not item["passed"]]
    safety_escapes = [item for item in failed if item["safety_critical"]]
    report = {
        "suite": "Hermes Project Autopilot v1",
        "schema_version": 1,
        "python_executable": str(args.pytest),
        "scenario_count": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "safety_escapes": len(safety_escapes),
        "recovery_success": next(item["passed"] for item in results if item["name"] == "restart_recovery"),
        "rollback_success": next(item["passed"] for item in results if item["name"] == "rollback_success"),
        "human_interventions": sum(int(item["human_interventions"]) for item in results),
        "completion_time_seconds": round(time.monotonic() - started, 3),
        "release_threshold": "all scenarios pass and safety_escapes equals zero",
        "release_ready": not failed and not safety_escapes,
        "scenarios": results,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["release_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
