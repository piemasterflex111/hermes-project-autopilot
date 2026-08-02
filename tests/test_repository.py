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
            "docs/release-evidence.md", "docs/provenance.md",
        ]
        for rel in required:
            self.assertTrue((ROOT / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
