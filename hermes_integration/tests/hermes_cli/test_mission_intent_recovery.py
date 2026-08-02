"""Focused persistence tests for mission ownership and spawn recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import missions_db as mdb


@pytest.fixture
def mission_store(tmp_path: Path):
    conn = kb.connect(tmp_path / "kanban.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        yield conn, repo
    finally:
        conn.close()


def _mission(conn, repo: Path) -> str:
    return mdb.create_mission(
        conn,
        objective="Make a scoped change",
        contract=mdb.MissionContract(
            outcome="Change one file",
            verification=["test -f app.py"],
            boundaries={"allowed_roots": [str(repo)], "allowed_paths": ["app.py"]},
        ),
        autonomy_level=4,
        repo_path=str(repo),
        board="default",
    )


def _task(conn) -> str:
    return kb.create_task(
        conn,
        title="mission executor",
        assignee="default",
        workspace_kind="dir",
        workspace_path="/tmp/mission-test-workspace",
    )


def _running_executor(conn, repo: Path):
    mission_id = _mission(conn, repo)
    task_id = _task(conn)
    mdb.link_task(conn, mission_id, task_id, "executor")
    mdb.transition_mission(conn, mission_id, "planning")
    mdb.transition_mission(conn, mission_id, "ready")
    mdb.transition_mission(conn, mission_id, "running")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    assert claimed.current_run_id is not None
    assert claimed.claim_lock
    return mission_id, task_id, claimed


def test_link_task_is_idempotent_but_never_steals_ownership(mission_store):
    conn, repo = mission_store
    first = _mission(conn, repo)
    second = _mission(conn, repo)
    task_id = _task(conn)

    mdb.link_task(conn, first, task_id, "executor")
    mdb.link_task(conn, first, task_id, "executor")

    with pytest.raises(ValueError, match="already linked"):
        mdb.link_task(conn, second, task_id, "executor")
    with pytest.raises(ValueError, match="already linked"):
        mdb.link_task(conn, first, task_id, "verifier")

    row = conn.execute(
        "SELECT mission_id,mission_role FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    assert tuple(row) == (first, "executor")
    with pytest.raises(KeyError):
        mdb.link_task(conn, first, "t_missing", "executor")


def test_begin_intent_is_idempotent_for_exact_run_claim_and_action(mission_store):
    conn, repo = mission_store
    mission_id, task_id, claimed = _running_executor(conn, repo)

    first = mdb.begin_intent(
        conn, mission_id, task_id=task_id, action="execute_task",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
        checkpoint_ref="checkpoint-one", details={"attempt": 1},
    )
    retried = mdb.begin_intent(
        conn, mission_id, task_id=task_id, action="execute_task",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
        checkpoint_ref="checkpoint-two", details={"attempt": 2},
    )
    different_action = mdb.begin_intent(
        conn, mission_id, task_id=task_id, action="another_action",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
    )
    different_run = mdb.begin_intent(
        conn, mission_id, task_id=task_id, action="execute_task",
        run_id=int(claimed.current_run_id) + 1, claim_token=claimed.claim_lock,
    )

    assert retried == first
    assert different_action != first
    assert different_run != first
    assert conn.execute(
        "SELECT COUNT(*) FROM mission_intents WHERE mission_id=?", (mission_id,),
    ).fetchone()[0] == 3


def test_spawn_failure_resolves_only_the_exact_executor_intent(mission_store):
    conn, repo = mission_store
    mission_id, task_id, claimed = _running_executor(conn, repo)
    exact = mdb.begin_intent(
        conn, mission_id, task_id=task_id, action="execute_task",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
    )
    other_run = mdb.begin_intent(
        conn, mission_id, task_id=task_id, action="execute_task",
        run_id=int(claimed.current_run_id) + 1, claim_token=claimed.claim_lock,
    )

    assert not kb._record_spawn_failure(conn, task_id, "process creation failed")

    rows = {
        row["id"]: row for row in conn.execute(
            "SELECT id,status,resolved_at FROM mission_intents WHERE id IN (?,?)",
            (exact, other_run),
        )
    }
    assert rows[exact]["status"] == "spawn_failed"
    assert rows[exact]["resolved_at"] is not None
    assert rows[other_run]["status"] == "open"
    assert kb.get_task(conn, task_id).status == "ready"


def test_spawn_failure_intent_and_task_release_are_atomic(
    mission_store, monkeypatch,
):
    conn, repo = mission_store
    mission_id, task_id, claimed = _running_executor(conn, repo)
    intent_id = mdb.begin_intent(
        conn, mission_id, task_id=task_id, action="execute_task",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
    )

    def fail_event(*args, **kwargs):
        raise RuntimeError("simulated SQLite-boundary failure")

    monkeypatch.setattr(kb, "_append_event", fail_event)
    with pytest.raises(RuntimeError, match="simulated SQLite-boundary failure"):
        kb._record_spawn_failure(conn, task_id, "process creation failed")

    intent = conn.execute(
        "SELECT status,resolved_at FROM mission_intents WHERE id=?", (intent_id,),
    ).fetchone()
    task = kb.get_task(conn, task_id)
    assert tuple(intent) == ("open", None)
    assert task.status == "running"
    assert task.current_run_id == claimed.current_run_id
    assert task.claim_lock == claimed.claim_lock
