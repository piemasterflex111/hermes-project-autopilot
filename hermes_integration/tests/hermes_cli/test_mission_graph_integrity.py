from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import mission_service as service
from hermes_cli import missions_db as mdb


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def planned_mission(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(hermes_home / "kanban"))
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "mission@example.test")
    _git(repo, "config", "user.name", "Mission Test")
    (repo / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "base")
    contract = mdb.MissionContract(
        outcome="Update app.py safely",
        verification=["verify-ok"],
        constraints=["Do not modify unrelated files"],
        boundaries={
            "allowed_roots": [str(repo)],
            "allowed_paths": ["app.py"],
            "network_destinations": [],
        },
        stop_when=["scope cannot be enforced"],
    )
    with kb.connect_closing(board="default") as conn:
        mission_id = mdb.create_mission(
            conn,
            objective="Change app.py",
            contract=contract,
            autonomy_level=4,
            repo_path=str(repo),
            board="default",
        )
        mission = service.prepare_mission(conn, mission_id)
        executor = service.add_execution_task(
            conn,
            mission_id,
            title="Edit app.py",
            body="Set VALUE and produce verification evidence",
            assignee="default",
        )
        service.finish_plan(conn, mission_id, verifier_assignee="default")
        yield conn, mission_id, mission, executor


def _tamper(conn, executor: str, kind: str) -> None:
    if kind == "edit":
        conn.execute("UPDATE tasks SET body='tampered' WHERE id=?", (executor,))
    elif kind == "delete":
        conn.execute("DELETE FROM tasks WHERE id=?", (executor,))
    elif kind == "reassign":
        conn.execute("UPDATE tasks SET assignee='attacker' WHERE id=?", (executor,))
    elif kind == "relink":
        conn.execute("DELETE FROM task_links WHERE child_id=?", (executor,))
    else:  # pragma: no cover - test helper guard
        raise AssertionError(kind)
    conn.commit()


def test_generic_kanban_structural_mutators_reject_mission_cards(planned_mission):
    conn, mission_id, _mission, executor = planned_mission
    root = mdb.get_mission(conn, mission_id).root_task_id
    ordinary = kb.create_task(conn, title="ordinary")

    with pytest.raises(PermissionError, match="mission-linked"):
        kb.assign_task(conn, executor, "other")
    with pytest.raises(PermissionError, match="mission-linked"):
        kb.reassign_task(conn, executor, "other")
    with pytest.raises(PermissionError, match="mission-linked"):
        kb.unlink_tasks(conn, root, executor)
    with pytest.raises(PermissionError, match="mission-linked"):
        kb.link_tasks(conn, executor, ordinary)
    with pytest.raises(PermissionError, match="mission-linked"):
        kb.archive_task(conn, executor)
    with pytest.raises(PermissionError, match="mission-linked"):
        kb.delete_task(conn, executor)


@pytest.mark.parametrize("kind", ["edit", "delete", "reassign", "relink"])
def test_start_revalidates_accepted_cards_and_edges(planned_mission, kind):
    conn, mission_id, _mission, executor = planned_mission
    _tamper(conn, executor, kind)

    with pytest.raises(service.MissionError, match="mission task"):
        service.start_mission(conn, mission_id)


@pytest.mark.parametrize("kind", ["edit", "delete", "reassign", "relink"])
def test_verification_revalidates_started_cards_and_edges(planned_mission, kind):
    conn, mission_id, mission, executor = planned_mission
    service.start_mission(conn, mission_id)
    worktree = Path(mission.worktree_path)
    (worktree / "app.py").write_text("VALUE = 2\n")
    service.record_command_result(
        conn,
        mission_id,
        command="verify-ok",
        cwd=str(worktree),
        exit_code=0,
        stdout="passed",
    )
    assert kb.complete_task(conn, executor, result="done", summary="done")
    _tamper(conn, executor, kind)

    with pytest.raises(service.MissionError, match="mission task"):
        service.submit_verifier_verdict(
            conn,
            mission_id,
            {
                "verdict": "pass",
                "requirements_checked": 1,
                "requirements_failed": [],
                "evidence_missing": [],
                "recommended_action": "commit",
            },
        )


def test_claim_revalidates_manifest_workspace(planned_mission):
    conn, mission_id, _mission, executor = planned_mission
    service.start_mission(conn, mission_id)
    conn.execute(
        "UPDATE tasks SET workspace_path='/tmp/substituted' WHERE id=?",
        (executor,),
    )
    conn.commit()

    with pytest.raises(service.MissionError, match="mission task"):
        kb.claim_task(conn, executor)


def test_spawn_validates_workspace_before_checkpoint(planned_mission, monkeypatch):
    conn, mission_id, _mission, executor = planned_mission
    service.start_mission(conn, mission_id)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    conn.execute(
        "UPDATE tasks SET workspace_path='/tmp/substituted' WHERE id=?",
        (executor,),
    )
    conn.commit()

    from tools.checkpoint_manager import CheckpointManager

    monkeypatch.setattr(
        CheckpointManager,
        "ensure_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("checkpoint ran before graph validation")
        ),
    )
    with pytest.raises(service.MissionError, match="mission task"):
        service.begin_task_intent(claimed, board="default")


def test_pre_manifest_mission_is_durably_blocked_with_recovery_guidance(
    planned_mission,
):
    conn, mission_id, _mission, _executor = planned_mission
    with kb.write_txn(conn):
        conn.execute(
            "DELETE FROM mission_task_manifest_cards WHERE mission_id=?",
            (mission_id,),
        )
        conn.execute(
            "DELETE FROM mission_task_manifests WHERE mission_id=?",
            (mission_id,),
        )

    with pytest.raises(service.MissionError, match="recreate the mission"):
        service.start_mission(conn, mission_id)
    blocked = mdb.get_mission(conn, mission_id)
    assert blocked.status == "blocked"
    assert blocked.phase == "migration"
    assert "cannot be authenticated safely" in blocked.blocked_reason
