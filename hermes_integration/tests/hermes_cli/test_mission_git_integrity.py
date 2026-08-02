from __future__ import annotations

import gzip
import hashlib
import os
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
def mission_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(hermes_home / "kanban"))

    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "mission@example.test")
    _git(repo, "config", "user.name", "Mission Test")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "base")

    with kb.connect_closing(board="default") as conn:
        yield conn, repo


def _contract(repo: Path) -> mdb.MissionContract:
    return mdb.MissionContract(
        outcome="Update the value safely",
        verification=["verify-ok"],
        constraints=["Do not touch unrelated files"],
        boundaries={
            "allowed_roots": [str(repo)],
            "allowed_paths": ["app.py"],
            "network_destinations": [],
        },
        stop_when=["scope cannot be enforced"],
        allow_local_commit=True,
    )


def _create(conn, repo: Path, *, autonomy: int = 4) -> str:
    return mdb.create_mission(
        conn,
        objective="Change app.py",
        contract=_contract(repo),
        autonomy_level=autonomy,
        repo_path=str(repo),
        board="default",
    )


def _verdict() -> dict[str, object]:
    return {
        "verdict": "pass",
        "requirements_checked": 1,
        "requirements_failed": [],
        "evidence_missing": [],
        "recommended_action": "commit",
    }


def _ready_for_verification(conn, repo: Path, *, autonomy: int = 4):
    mission_id = _create(conn, repo, autonomy=autonomy)
    mission = service.prepare_mission(conn, mission_id)
    executor_id = service.add_execution_task(
        conn,
        mission_id,
        title="Edit app.py",
        body="Set VALUE",
        assignee="default",
    )
    service.finish_plan(conn, mission_id, verifier_assignee="default")
    service.start_mission(conn, mission_id)
    worktree = Path(mission.worktree_path)
    (worktree / "app.py").write_text(
        f"VALUE = {autonomy}\n", encoding="utf-8",
    )
    service.record_command_result(
        conn,
        mission_id,
        command="verify-ok",
        cwd=str(worktree),
        exit_code=0,
        stdout="1 passed",
    )
    assert kb.complete_task(
        conn, executor_id, result="implemented", summary="implemented",
    )
    return mission_id, worktree, executor_id


def test_predictable_mission_commit_before_seal_fails_verification(mission_env):
    conn, repo = mission_env
    mission_id, worktree, _executor_id = _ready_for_verification(conn, repo)

    _git(worktree, "add", "--all")
    _git(worktree, "commit", "-m", f"mission({mission_id}): Change app.py")
    forged_head = _git(worktree, "rev-parse", "HEAD")

    blocked = service.submit_verifier_verdict(conn, mission_id, _verdict())

    assert blocked.status == "blocked"
    deterministic = [
        row for row in mdb.list_evidence(conn, mission_id)
        if row["kind"] == "deterministic_verifier"
    ][-1]
    assert deterministic["status"] == "failed"
    assert deterministic["metadata"]["head_unchanged"] is False
    assert not any(
        row["kind"] == "verification_seal"
        for row in mdb.list_evidence(conn, mission_id)
    )
    assert _git(worktree, "rev-parse", "HEAD") == forged_head
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("action", ["commit", "rollback"])
def test_gitdir_identity_tamper_blocks_control_mutation(mission_env, action):
    conn, repo = mission_env
    if action == "commit":
        mission_id, worktree, _executor_id = _ready_for_verification(conn, repo)
        assert service.submit_verifier_verdict(
            conn, mission_id, _verdict(),
        ).status == "committing"
    else:
        mission_id = _create(conn, repo)
        mission = service.prepare_mission(conn, mission_id)
        worktree = Path(mission.worktree_path)
        (worktree / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
        mdb.transition_mission(
            conn,
            mission_id,
            "blocked",
            phase="execution",
            blocked_reason="test rollback boundary",
        )

    source_head = _git(repo, "rev-parse", "HEAD")
    source_status = _git(repo, "status", "--porcelain")
    worktree_bytes = (worktree / "app.py").read_bytes()
    git_file = worktree / ".git"
    assert git_file.is_file()
    git_file.write_text(f"gitdir: {repo / '.git'}\n", encoding="utf-8")

    with pytest.raises(service.MissionError, match="identity mismatch"):
        if action == "commit":
            service.commit_mission(conn, mission_id)
        else:
            service.rollback_mission(conn, mission_id)

    assert _git(repo, "rev-parse", "HEAD") == source_head
    assert _git(repo, "status", "--porcelain") == source_status
    assert (worktree / "app.py").read_bytes() == worktree_bytes
    assert mdb.get_mission(conn, mission_id).status in {"committing", "blocked"}


def test_moved_rollback_ref_is_rejected_before_reset(mission_env):
    conn, repo = mission_env
    mission_id = _create(conn, repo)
    mission = service.prepare_mission(conn, mission_id)
    worktree = Path(mission.worktree_path)
    (worktree / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
    mdb.transition_mission(
        conn,
        mission_id,
        "blocked",
        phase="execution",
        blocked_reason="test rollback boundary",
    )

    source_head = _git(repo, "rev-parse", "HEAD")
    source_status = _git(repo, "status", "--porcelain")
    worktree_head = _git(worktree, "rev-parse", "HEAD")
    worktree_bytes = (worktree / "app.py").read_bytes()
    tree = _git(repo, "rev-parse", f"{mission.base_commit}^{{tree}}")
    moved_target = _git(
        repo,
        "commit-tree",
        tree,
        "-p",
        mission.base_commit,
        "-m",
        "moved rollback target",
    )
    _git(repo, "update-ref", mission.rollback_ref, moved_target)

    with pytest.raises(service.MissionError, match="rollback ref"):
        service.rollback_mission(conn, mission_id)

    assert _git(worktree, "rev-parse", "HEAD") == worktree_head
    assert (worktree / "app.py").read_bytes() == worktree_bytes
    assert _git(repo, "rev-parse", "HEAD") == source_head
    assert _git(repo, "status", "--porcelain") == source_status


def test_executable_bit_only_change_is_sealed_and_committed(mission_env):
    conn, repo = mission_env
    mission_id = _create(conn, repo)
    mission = service.prepare_mission(conn, mission_id)
    executor_id = service.add_execution_task(
        conn,
        mission_id,
        title="Make app.py executable",
        body="Change only the executable bit",
        assignee="default",
    )
    service.finish_plan(conn, mission_id, verifier_assignee="default")
    service.start_mission(conn, mission_id)
    worktree = Path(mission.worktree_path)
    os.chmod(worktree / "app.py", 0o755)
    service.record_command_result(
        conn,
        mission_id,
        command="verify-ok",
        cwd=str(worktree),
        exit_code=0,
        stdout="mode verified",
    )
    assert kb.complete_task(
        conn, executor_id, result="implemented", summary="implemented",
    )

    assert service.submit_verifier_verdict(
        conn, mission_id, _verdict(),
    ).status == "committing"
    seal = [
        row for row in mdb.list_evidence(conn, mission_id)
        if row["kind"] == "verification_seal"
    ][-1]["metadata"]["workspace_snapshot"]
    sealed_app = next(item for item in seal["files"] if item["path"] == "app.py")
    assert sealed_app["mode"] == "100755"

    completed = service.commit_mission(conn, mission_id)

    assert completed.status == "succeeded"
    assert _git(worktree, "ls-tree", completed.verified_commit, "app.py").startswith(
        "100755 blob "
    )
    assert (worktree / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(repo, "status", "--porcelain") == ""


def test_verification_and_commit_reject_open_intents(mission_env):
    conn, repo = mission_env
    verify_id, verify_worktree, verify_executor = _ready_for_verification(conn, repo)
    mdb.begin_intent(
        conn,
        verify_id,
        task_id=verify_executor,
        action="unresolved-write",
    )

    with pytest.raises(service.MissionError, match="unresolved mutation intent"):
        service.submit_verifier_verdict(conn, verify_id, _verdict())
    assert mdb.get_mission(conn, verify_id).status == "running"
    assert _git(verify_worktree, "rev-parse", "HEAD") == mdb.get_mission(
        conn, verify_id,
    ).base_commit

    commit_id, commit_worktree, commit_executor = _ready_for_verification(conn, repo)
    assert service.submit_verifier_verdict(
        conn, commit_id, _verdict(),
    ).status == "committing"
    mdb.begin_intent(
        conn,
        commit_id,
        task_id=commit_executor,
        action="unresolved-write",
    )

    with pytest.raises(service.MissionError, match="unresolved mutation intent"):
        service.commit_mission(conn, commit_id)
    assert mdb.get_mission(conn, commit_id).status == "committing"
    assert _git(commit_worktree, "rev-parse", "HEAD") == mdb.get_mission(
        conn, commit_id,
    ).base_commit
    assert _git(repo, "status", "--porcelain") == ""


def test_referenced_corrupt_blob_cannot_be_silently_replaced(mission_env):
    conn, repo = mission_env
    mission_id = _create(conn, repo)
    mission = service.prepare_mission(conn, mission_id)
    evidence_id = service.record_command_result(
        conn,
        mission_id,
        command="verify-ok",
        cwd=mission.worktree_path,
        exit_code=0,
        stdout="passed",
    )
    row = conn.execute(
        "SELECT blob_path FROM mission_evidence WHERE id=?", (evidence_id,),
    ).fetchone()
    target = Path(row["blob_path"])
    target.write_bytes(b"corrupt referenced blob")
    count_before = conn.execute(
        "SELECT COUNT(*) FROM mission_evidence WHERE mission_id=?", (mission_id,),
    ).fetchone()[0]

    with pytest.raises(service.MissionError, match="referenced evidence blob"):
        service.record_command_result(
            conn,
            mission_id,
            command="verify-ok",
            cwd=mission.worktree_path,
            exit_code=0,
            stdout="passed",
        )

    assert target.read_bytes() == b"corrupt referenced blob"
    assert conn.execute(
        "SELECT COUNT(*) FROM mission_evidence WHERE mission_id=?", (mission_id,),
    ).fetchone()[0] == count_before
    assert not mdb.verify_evidence_chain(conn, mission_id)


def test_unreferenced_corrupt_blob_recovers_atomically_after_replace_failure(
    mission_env, monkeypatch,
):
    conn, repo = mission_env
    mission_id = _create(conn, repo)
    mission = service.prepare_mission(conn, mission_id)
    raw = b"STDOUT\npassed\n\nSTDERR\n"
    digest = hashlib.sha256(raw).hexdigest()
    root = kb.board_dir(mission.board) / "mission-evidence" / mission_id
    target = root / f"{digest}.txt.gz"
    target.write_bytes(b"corrupt unreferenced blob")
    count_before = conn.execute(
        "SELECT COUNT(*) FROM mission_evidence WHERE mission_id=?", (mission_id,),
    ).fetchone()[0]

    with monkeypatch.context() as patch:
        patch.setattr(
            service.os,
            "replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
        )
        with pytest.raises(OSError, match="replace failed"):
            service.record_command_result(
                conn,
                mission_id,
                command="verify-ok",
                cwd=mission.worktree_path,
                exit_code=0,
                stdout="passed",
            )

    assert target.read_bytes() == b"corrupt unreferenced blob"
    assert conn.execute(
        "SELECT COUNT(*) FROM mission_evidence WHERE mission_id=?", (mission_id,),
    ).fetchone()[0] == count_before
    assert not list(root.glob(f".{digest}.*.tmp"))

    evidence_id = service.record_command_result(
        conn,
        mission_id,
        command="verify-ok",
        cwd=mission.worktree_path,
        exit_code=0,
        stdout="passed",
    )

    assert evidence_id > 0
    with gzip.open(target, "rb") as fh:
        assert fh.read() == raw
    assert mdb.verify_evidence_chain(conn, mission_id)
