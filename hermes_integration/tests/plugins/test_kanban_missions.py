"""Native dashboard API contract for Project Autopilot missions."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


def _load_router():
    source = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_mission_api_test", source)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def api(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "api@example.test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "API Test"], check=True)
    (repo / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
    with pdb.connect_closing() as project_conn:
        pdb.create_project(project_conn, name="API Project", slug="api-project", primary_path=str(repo))
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_router(), prefix="/api/plugins/kanban")
    return TestClient(app), repo


def test_create_list_and_lifecycle_actions(api):
    client, repo = api
    created = client.post(
        "/api/plugins/kanban/missions",
        json={
            "objective": "Update app", "outcome": "Value updated",
            "verification": ["verify-ok"], "constraints": [],
            "boundaries": {"allowed_roots": [str(repo)], "allowed_paths": ["app.py"]},
            "stop_when": [], "autonomy_level": 3, "repo_path": str(repo), "project_id": "api-project",
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["mission_id"]
    listed = client.get("/api/plugins/kanban/missions").json()["missions"]
    assert listed[0]["id"] == mid
    detail = client.get(f"/api/plugins/kanban/missions/{mid}")
    assert detail.status_code == 200
    report = detail.json()
    assert report["mission"]["id"] == mid
    assert report["mission"]["contract"]["outcome"] == "Value updated"
    assert report["evidence_chain_valid"] is True
    assert report["tasks"]
    assert {"links", "evidence", "open_intents"} <= report.keys()
    prepared = client.post(
        f"/api/plugins/kanban/missions/{mid}/actions", json={"action": "prepare"},
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["mission"]["status"] == "planning"
    cancelled = client.post(
        f"/api/plugins/kanban/missions/{mid}/actions", json={"action": "cancel"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["mission"]["status"] == "cancelled"


def test_create_rejects_repo_outside_contract_root(api, tmp_path):
    client, repo = api
    response = client.post(
        "/api/plugins/kanban/missions",
        json={
            "objective": "Escape", "outcome": "No", "verification": ["x"],
            "boundaries": {"allowed_roots": [str(tmp_path / "elsewhere")]},
            "autonomy_level": 4, "repo_path": str(repo), "project_id": "api-project",
        },
    )
    assert response.status_code == 400
    assert "allowed root" in response.json()["detail"]


def test_mission_api_maps_invalid_status_and_policy_errors_to_client_errors(api):
    client, repo = api

    invalid_status = client.get("/api/plugins/kanban/missions?status=not-a-status")
    assert invalid_status.status_code == 400
    assert "invalid mission status" in invalid_status.json()["detail"]

    invalid_risk = client.post(
        "/api/plugins/kanban/missions",
        json={
            "objective": "Bad risk", "outcome": "No", "verification": ["x"],
            "boundaries": {"allowed_roots": [str(repo)]},
            "autonomy_level": 3, "repo_path": str(repo), "project_id": "api-project", "risk_level": "extreme",
        },
    )
    assert invalid_risk.status_code == 422

    created = client.post(
        "/api/plugins/kanban/missions",
        json={
            "objective": "State error", "outcome": "No", "verification": ["x"],
            "boundaries": {"allowed_roots": [str(repo)]},
            "autonomy_level": 3, "repo_path": str(repo), "project_id": "api-project",
        },
    )
    mid = created.json()["mission_id"]
    invalid_action = client.post(
        f"/api/plugins/kanban/missions/{mid}/actions", json={"action": "start"},
    )
    assert invalid_action.status_code == 409
    assert "cannot start mission from planning" in invalid_action.json()["detail"]


def test_dispatch_response_exposes_mission_controller_telemetry(api, monkeypatch):
    client, _repo = api
    result = kb.DispatchResult(
        missions_reconciled=["m_recovered"],
        mission_actions=[{"mission_id": "m_advanced", "action": "succeeded"}],
        skipped_locked=True,
    )
    monkeypatch.setattr(kb, "dispatch_once", lambda conn, **kwargs: result)

    response = client.post("/api/plugins/kanban/dispatch")
    assert response.status_code == 200
    payload = response.json()
    assert payload["missions_reconciled"] == ["m_recovered"]
    assert payload["mission_actions"] == [
        {"mission_id": "m_advanced", "action": "succeeded"},
    ]
    assert payload["skipped_locked"] is True
