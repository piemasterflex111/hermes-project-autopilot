from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryIntegrityTests(unittest.TestCase):
    def test_commit_chain_is_linear(self):
        data = json.loads((ROOT / "provenance/commits.json").read_text())
        commits = data["commits"]
        self.assertEqual(5, len(commits))
        self.assertEqual(data["local_feature_base_commit"], commits[0]["parents"][0])
        prerequisite = data["prerequisite_commits"][0]
        self.assertEqual(data["public_replay_base_commit"], prerequisite["parents"][0])
        self.assertEqual(data["local_feature_base_commit"], prerequisite["commit"])
        for previous, current in zip(commits, commits[1:]):
            self.assertEqual([previous["commit"]], current["parents"])
        self.assertEqual(data["head_commit"], commits[-1]["commit"])

    def test_required_core_files_are_exported(self):
        required = {
            "hermes_cli/missions_db.py",
            "hermes_cli/mission_service.py",
            "hermes_cli/missions.py",
            "hermes_cli/mission_gateway.py",
            "scripts/evaluate_project_autopilot.py",
            "ui-tui/src/components/missionCenter.tsx",
            "apps/desktop/src/plugins/kanban/board.tsx",
            "plugins/kanban/dashboard/plugin_api.py",
        }
        manifest = json.loads((ROOT / "provenance/source-manifest.json").read_text())
        paths = {item["path"] for item in manifest["files"]}
        self.assertTrue(required <= paths, required - paths)

    def test_release_report_contains_all_scenarios(self):
        report = json.loads((ROOT / "evidence/release-gate.json").read_text())
        self.assertTrue(report["release_ready"])
        self.assertEqual(18, len(report["scenarios"]))
        self.assertTrue(all(item["passed"] for item in report["scenarios"]))

    def test_documentation_set_exists(self):
        required = [
            "docs/architecture.md", "docs/lifecycle.md", "docs/security-model.md",
            "docs/gateway-authorization.md", "docs/integration.md",
            "docs/release-evidence.md", "docs/provenance.md", "docs/scalability.md",
            "docs/containment-acceptance.md", "docs/model-benchmark.md",
            "docs/release-process.md",
        ]
        for rel in required:
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_current_compatibility_release_is_consistent(self):
        release_dir = ROOT / "releases/hermes-0.20-compat"
        release = json.loads((release_dir / "release.json").read_text())
        evidence = json.loads((release_dir / "evidence/summary.json").read_text())
        self.assertEqual(25, release["patch_count"])
        self.assertEqual(release["source_head_commit"], evidence["source_head_commit"])
        self.assertEqual(215, evidence["focused_tests"]["passed"])
        self.assertEqual(0, evidence["focused_tests"]["failed"])
        scale = {item["executor_tasks"]: item for item in evidence["scale_runs"]}
        self.assertEqual({1000, 5000}, set(scale))
        for run in scale.values():
            self.assertTrue(run["pass"])
            self.assertEqual(1, run["duplicate_claim_winners"])
            self.assertEqual(0, run["open_runs"])
            self.assertEqual("ok", run["sqlite_integrity"])

    def test_containment_acceptance_is_complete(self):
        report = json.loads((ROOT / "releases/hermes-0.20-compat/evidence/containment-acceptance.json").read_text())
        self.assertTrue(report["release_ready"])
        self.assertEqual(7, report["scenario_count"])
        self.assertEqual(7, report["passed"])
        self.assertEqual(0, report["failed"])
        self.assertEqual(0, report["safety_escapes"])
        self.assertTrue(all(item["passed"] for item in report["scenarios"]))

    def test_model_benchmark_satisfies_issue_one(self):
        report = json.loads((ROOT / "releases/hermes-0.20-compat/evidence/model-benchmark.json").read_text())
        self.assertTrue(report["pass"])
        self.assertEqual(5, report["repository_count"])
        self.assertEqual(20, report["mission_count"])
        self.assertEqual(20, report["passed"])
        self.assertEqual(0, report["failed"])
        self.assertEqual(0, report["false_success_count"])
        self.assertEqual(0, report["safety_escapes"])
        self.assertEqual(5, report["rollback_cases"])
        self.assertEqual(5, report["rollback_successes"])
        self.assertEqual({"L3", "L4"}, set(report["by_autonomy"]))

    def test_release_engineering_files_exist(self):
        required = [
            "scripts/run_model_benchmark.py", "scripts/run_containment_acceptance.py",
            "scripts/generate_sbom.py", "scripts/build_release_bundle.py",
            "release/keys/payam-adloo-ed25519.pub", "release/keys/allowed_signers",
        ]
        for rel in required:
            self.assertTrue((ROOT / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
