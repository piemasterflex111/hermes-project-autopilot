"""Project Autopilot JSON-RPC surface used by the native TUI and desktop."""

from __future__ import annotations

import subprocess

import tui_gateway.server as server
from hermes_cli import projects_db as pdb


def _call(name, params):
    response = server._methods[name](1, params)
    assert "error" not in response, response.get("error")
    return response["result"]


def test_mission_methods_registered_and_roundtrip(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "rpc@example.test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "RPC Test"], check=True)
    (repo / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(project_conn, name="RPC Project", primary_path=str(repo))

    for name in ("missions.list", "missions.get", "missions.create", "missions.action", "missions.plan_auto"):
        assert name in server._methods
    created = _call(
        "missions.create",
        {
            "objective": "RPC mission", "outcome": "Done", "verification": ["verify-ok"],
            "boundaries": {"allowed_roots": [str(repo)], "allowed_paths": ["app.py"]},
            "autonomy_level": 3, "repo_path": str(repo), "project_id": project_id, "board": "default",
        },
    )
    mid = created["mission_id"]
    assert _call("missions.list", {"board": "default"})["missions"][0]["id"] == mid
    prepared = _call("missions.action", {"id": mid, "action": "prepare", "board": "default"})
    assert prepared["mission"]["status"] == "planning"
    report = _call("missions.get", {"id": mid, "board": "default"})
    assert report["evidence_chain_valid"] is True

    invalid_status = server._methods["missions.list"](
        2, {"board": "default", "status": "not-a-status"},
    )
    assert invalid_status["error"]["code"] == 5072
    assert "invalid mission status" in invalid_status["error"]["message"]

    policy_error = server._methods["missions.action"](
        3, {"board": "default", "id": mid, "action": "start"},
    )
    assert policy_error["error"]["code"] == 5072
    assert "cannot start mission from planning" in policy_error["error"]["message"]

    invalid_risk = server._methods["missions.create"](
        4,
        {
            "objective": "Bad risk", "outcome": "No", "verification": ["x"],
            "boundaries": {"allowed_roots": [str(repo)]},
            "autonomy_level": 3, "repo_path": str(repo), "project_id": project_id, "board": "default",
            "risk_level": "extreme",
        },
    )
    assert invalid_risk["error"]["code"] == 5072
    assert "risk_level must be one of" in invalid_risk["error"]["message"]
