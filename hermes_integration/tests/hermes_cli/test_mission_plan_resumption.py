"""Crash-boundary tests for durable, idempotent mission planning."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import auxiliary_client
from hermes_cli import kanban_db as kb
from hermes_cli import mission_service as service
from hermes_cli import missions_db as mdb


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def prepared_mission(tmp_path: Path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(hermes_home / "kanban"))
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "planner@example.test")
    _git(repo, "config", "user.name", "Planner Test")
    (repo / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "base")
    with kb.connect_closing(board="default") as conn:
        resumed_connections = []

        def reopen():
            resumed = kb.connect(service._connection_db_path(conn))
            resumed_connections.append(resumed)
            return resumed

        contract = mdb.MissionContract(
            outcome="Update app.py safely",
            verification=["python -m pytest"],
            constraints=["Do not touch unrelated files"],
            boundaries={
                "allowed_roots": [str(repo)],
                "allowed_paths": ["app.py"],
                "network_destinations": [],
            },
        )
        mission_id = mdb.create_mission(
            conn,
            objective="Update app.py",
            contract=contract,
            autonomy_level=4,
            repo_path=str(repo),
            board="default",
        )
        service.prepare_mission(conn, mission_id)
        try:
            yield conn, mission_id, reopen
        finally:
            for resumed in resumed_connections:
                resumed.close()


def _planner_response(tasks: list[tuple[str, str]]):
    payload = {
        "tasks": [
            {"title": f"  {title}  ", "body": f"  {body}  "}
            for title, body in tasks
        ],
    }
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)),
        )],
    )


def _mission_cards(conn, mission_id: str, role: str):
    return conn.execute(
        """SELECT * FROM tasks WHERE mission_id=? AND mission_role=?
             ORDER BY idempotency_key""",
        (mission_id, role),
    ).fetchall()


def test_plan_resumes_without_llm_after_crash_mid_executors(
    prepared_mission, monkeypatch,
):
    conn, mission_id, reopen = prepared_mission
    calls = {"llm": 0, "executors": 0}

    def planned_response(**kwargs):
        calls["llm"] += 1
        return _planner_response([
            ("Inspect", "Inspect and state verification"),
            ("Patch", "Patch and state verification"),
            ("Test", "Test and state expected output"),
        ])

    monkeypatch.setattr(auxiliary_client, "call_llm", planned_response)
    original_create = kb.create_task

    def crash_during_creation(*args, **kwargs):
        key = str(kwargs.get("idempotency_key") or "")
        if ":executor:" in key:
            calls["executors"] += 1
            if calls["executors"] == 2:
                raise RuntimeError("simulated crash between executor cards")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(kb, "create_task", crash_during_creation)
    with pytest.raises(RuntimeError, match="between executor cards"):
        service.plan_with_model(
            conn, mission_id,
            executor_assignee="DEFAULT",
            verifier_assignee="default",
        )

    plan = mdb.get_plan(conn, mission_id)
    assert plan is not None
    assert calls["llm"] == 1
    assert len(_mission_cards(conn, mission_id, "executor")) == 1
    assert not _mission_cards(conn, mission_id, "verifier")
    assert mdb.get_mission(conn, mission_id).status == "planning"
    assert json.loads(plan["payload_json"])["tasks"][0]["title"] == "Inspect"

    def llm_must_not_run(**kwargs):
        raise AssertionError("durable plan retry must not call the model")

    monkeypatch.setattr(auxiliary_client, "call_llm", llm_must_not_run)
    monkeypatch.setattr(kb, "create_task", original_create)
    conn = reopen()
    resumed = service.plan_with_model(
        conn, mission_id,
        executor_assignee="changed-after-crash",
        verifier_assignee="changed-after-crash",
    )
    assert resumed.status == "ready"
    executors = _mission_cards(conn, mission_id, "executor")
    verifiers = _mission_cards(conn, mission_id, "verifier")
    assert len(executors) == 3
    assert len(verifiers) == 1
    assert {row["assignee"] for row in executors} == {"default"}
    assert verifiers[0]["assignee"] == "default"
    assert kb.parent_ids(conn, executors[0]["id"]) == [resumed.root_task_id]
    assert kb.parent_ids(conn, executors[1]["id"]) == [executors[0]["id"]]
    assert kb.parent_ids(conn, executors[2]["id"]) == [executors[1]["id"]]
    assert set(kb.parent_ids(conn, verifiers[0]["id"])) == {
        row["id"] for row in executors
    }

    repeated = service.plan_with_model(
        conn, mission_id,
        executor_assignee="ignored",
        verifier_assignee="ignored",
    )
    assert repeated.status == "ready"
    assert len(_mission_cards(conn, mission_id, "executor")) == 3
    assert len(_mission_cards(conn, mission_id, "verifier")) == 1
    plan_evidence = [
        row for row in mdb.list_evidence(conn, mission_id)
        if row["kind"] == "plan"
    ]
    assert len(plan_evidence) == 1
    assert plan_evidence[0]["metadata"]["plan_hash"] == plan["plan_hash"]


def test_plan_resumes_after_crash_immediately_after_verifier_creation(
    prepared_mission, monkeypatch,
):
    conn, mission_id, reopen = prepared_mission
    monkeypatch.setattr(
        auxiliary_client,
        "call_llm",
        lambda **kwargs: _planner_response([
            ("Patch", "Produce patch and verification"),
            ("Test", "Produce test evidence"),
        ]),
    )
    original_record = mdb.record_evidence
    crashed = {"value": False}

    def crash_before_plan_evidence(*args, **kwargs):
        if kwargs.get("kind") == "plan" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated crash after verifier creation")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(mdb, "record_evidence", crash_before_plan_evidence)
    with pytest.raises(RuntimeError, match="after verifier creation"):
        service.plan_with_model(
            conn, mission_id,
            executor_assignee="default",
            verifier_assignee="default",
        )

    assert mdb.get_plan(conn, mission_id) is not None
    assert len(_mission_cards(conn, mission_id, "executor")) == 2
    assert len(_mission_cards(conn, mission_id, "verifier")) == 1
    assert mdb.get_mission(conn, mission_id).status == "planning"

    monkeypatch.setattr(
        auxiliary_client,
        "call_llm",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("retry unexpectedly called the model")
        ),
    )
    monkeypatch.setattr(mdb, "record_evidence", original_record)
    conn = reopen()
    resumed = service.plan_with_model(
        conn, mission_id,
        executor_assignee="default",
        verifier_assignee="default",
    )
    assert resumed.status == "ready"
    assert len(_mission_cards(conn, mission_id, "executor")) == 2
    assert len(_mission_cards(conn, mission_id, "verifier")) == 1
    assert len([
        row for row in mdb.list_evidence(conn, mission_id)
        if row["kind"] == "plan"
    ]) == 1


def test_finish_manual_plan_is_idempotent(prepared_mission):
    conn, mission_id, _ = prepared_mission
    service.add_execution_task(
        conn, mission_id,
        title="Patch",
        body="Produce patch and verification",
        assignee="default",
    )
    first = service.finish_plan(
        conn, mission_id, verifier_assignee="default",
    )
    second = service.finish_plan(
        conn, mission_id, verifier_assignee="default",
    )
    assert first.status == second.status == "ready"
    assert len(_mission_cards(conn, mission_id, "verifier")) == 1


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("spec", "canonical spec: body"),
        ("edge", "unexpected dependencies"),
    ],
)
def test_retry_rejects_tampered_adopted_card(
    prepared_mission, monkeypatch, tamper, error,
):
    conn, mission_id, _ = prepared_mission
    monkeypatch.setattr(
        auxiliary_client,
        "call_llm",
        lambda **kwargs: _planner_response([
            ("Patch", "Produce patch and verification"),
            ("Test", "Produce test evidence"),
        ]),
    )
    original_create = kb.create_task
    created = {"count": 0}

    def crash_on_second_executor(*args, **kwargs):
        if ":executor:" in str(kwargs.get("idempotency_key") or ""):
            created["count"] += 1
            if created["count"] == 2:
                raise RuntimeError("crash")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(kb, "create_task", crash_on_second_executor)
    with pytest.raises(RuntimeError, match="crash"):
        service.plan_with_model(
            conn, mission_id,
            executor_assignee="default",
            verifier_assignee="default",
        )
    first = _mission_cards(conn, mission_id, "executor")[0]
    if tamper == "spec":
        conn.execute("UPDATE tasks SET body='tampered' WHERE id=?", (first["id"],))
    else:
        conn.execute("DELETE FROM task_links WHERE child_id=?", (first["id"],))
    conn.commit()
    monkeypatch.setattr(kb, "create_task", original_create)
    monkeypatch.setattr(
        auxiliary_client,
        "call_llm",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("retry unexpectedly called the model")
        ),
    )
    with pytest.raises(service.MissionError, match=error):
        service.plan_with_model(
            conn, mission_id,
            executor_assignee="default",
            verifier_assignee="default",
        )
