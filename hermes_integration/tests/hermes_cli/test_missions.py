from __future__ import annotations

import json
import os
import subprocess
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

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
def mission_env(tmp_path, monkeypatch):
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
    with kb.connect_closing(board="default") as conn:
        yield conn, repo


def _contract(repo: Path, *, allowed_paths=None, allow_local_commit=True):
    return mdb.MissionContract(
        outcome="Update the value safely",
        verification=["verify-ok"],
        constraints=["Do not touch unrelated files"],
        boundaries={
            "allowed_roots": [str(repo)],
            "allowed_paths": allowed_paths or ["app.py"],
            "network_destinations": [],
        },
        stop_when=["scope cannot be enforced"],
        allow_local_commit=allow_local_commit,
    )


def _created(conn, repo: Path, *, autonomy=4, budget=None) -> str:
    return mdb.create_mission(
        conn,
        objective="Change app.py",
        contract=_contract(repo, allow_local_commit=(autonomy == 4)),
        autonomy_level=autonomy,
        repo_path=str(repo),
        board="default",
        budget=budget,
    )


def _passing_verifier_response():
    payload = _verdict()
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )


def _verdict(**overrides):
    payload = {
        "verdict": "pass", "requirements_checked": 1,
        "requirements_failed": [], "evidence_missing": [],
        "recommended_action": "commit",
    }
    payload.update(overrides)
    return payload


def _ready_for_verification(conn, repo: Path, *, autonomy=4, budget=None):
    mid = _created(conn, repo, autonomy=autonomy, budget=budget)
    mission = service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit app.py", body="Set VALUE", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    worktree = Path(mission.worktree_path)
    (worktree / "app.py").write_text(f"VALUE = {autonomy}\n")
    service.record_command_result(
        conn, mid, command="verify-ok", cwd=str(worktree), exit_code=0,
        stdout="1 passed",
    )
    assert kb.complete_task(conn, executor, result="implemented", summary="implemented")
    return mid, worktree, executor


def test_mission_requires_explicit_valid_contract(mission_env):
    conn, repo = mission_env
    with pytest.raises(ValueError, match="autonomy_level"):
        mdb.create_mission(
            conn, objective="x", contract=_contract(repo), autonomy_level=5,
            repo_path=str(repo), board="default",
        )
    with pytest.raises(ValueError, match="verification"):
        mdb.MissionContract(
            outcome="x", verification=[],
            boundaries={"allowed_roots": [str(repo)]},
        ).validate()
    with pytest.raises(ValueError, match="safe relative paths"):
        mdb.MissionContract(
            outcome="x", verification=["verify-ok"],
            boundaries={"allowed_roots": [str(repo)], "allowed_paths": ["../escape"]},
        ).validate()


def test_execution_rejects_unenforceable_network_scope(mission_env):
    conn, repo = mission_env
    contract = _contract(repo)
    contract = mdb.MissionContract(
        outcome=contract.outcome,
        verification=contract.verification,
        constraints=contract.constraints,
        boundaries=contract.boundaries | {"network_destinations": ["pypi.org"]},
        stop_when=contract.stop_when,
    )
    mid = mdb.create_mission(
        conn, objective="Networked change", contract=contract,
        autonomy_level=4, repo_path=str(repo), board="default",
    )
    service.prepare_mission(conn, mid)
    service.add_execution_task(conn, mid, title="Edit", body="Edit", assignee="default")
    service.finish_plan(conn, mid, verifier_assignee="default")
    with pytest.raises(service.MissionError, match="cannot enforce destination"):
        service.start_mission(conn, mid)
    assert mdb.get_mission(conn, mid).status == "ready"


def test_autonomy_levels_gate_inspection_preparation_and_execution(mission_env):
    conn, repo = mission_env
    advisory = _created(conn, repo, autonomy=0)
    with pytest.raises(service.MissionError, match="level 1"):
        service.inspect_mission(conn, advisory)
    with pytest.raises(service.MissionError, match="level 2"):
        service.prepare_mission(conn, advisory)

    inspect_only = _created(conn, repo, autonomy=1)
    assert service.inspect_mission(conn, inspect_only).status == "draft"
    assert mdb.list_evidence(conn, inspect_only)[-1]["kind"] == "inspection"
    with pytest.raises(service.MissionError, match="level 2"):
        service.prepare_mission(conn, inspect_only)

    draft_only = _created(conn, repo, autonomy=2)
    service.prepare_mission(conn, draft_only)
    service.add_execution_task(conn, draft_only, title="Draft", body="Draft", assignee="default")
    service.finish_plan(conn, draft_only, verifier_assignee="default")
    with pytest.raises(service.MissionError, match="level 3"):
        service.start_mission(conn, draft_only)


def test_illegal_transition_is_rejected(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    with pytest.raises(ValueError, match="illegal mission transition"):
        mdb.transition_mission(conn, mid, "succeeded")


def test_prepare_isolates_worktree_and_preserves_source_checkout(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    mission = service.prepare_mission(conn, mid)

    assert mission.status == "planning"
    assert Path(mission.worktree_path).is_dir()
    assert not Path(mission.worktree_path).is_relative_to(repo)
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", mission.rollback_ref) == mission.base_commit
    task = kb.get_task(conn, mission.root_task_id)
    assert task is not None
    row = conn.execute(
        "SELECT mission_id, mission_role FROM tasks WHERE id=?", (task.id,)
    ).fetchone()
    assert tuple(row) == (mid, "controller")


def test_prepare_is_idempotent_after_verified_artifacts_exist(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    first = service.prepare_mission(conn, mid)
    second = service.prepare_mission(conn, mid)
    assert second.worktree_path == first.worktree_path
    assert second.base_commit == first.base_commit
    assert second.root_task_id == first.root_task_id
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE mission_id=? AND mission_role='controller'",
        (mid,),
    ).fetchone()[0] == 1
    assert sum(row["kind"] == "environment" for row in mdb.list_evidence(conn, mid)) == 1


def test_prepare_adopts_exact_git_artifacts_after_crash(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    base = _git(repo, "rev-parse", "HEAD")
    branch = f"hermes/mission-{mid[2:]}"
    rollback_ref = f"refs/hermes/rollback/{mid}"
    worktree = repo.parent / ".hermes-worktrees" / repo.name / mid
    mdb.begin_preparation(
        conn, mid, worktree_path=str(worktree), branch_name=branch,
        base_commit=base, rollback_ref=rollback_ref,
    )
    _git(repo, "update-ref", rollback_ref, base)
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    adopted = service.prepare_mission(conn, mid)
    assert adopted.status == "planning"
    assert adopted.worktree_path == str(worktree)
    assert adopted.base_commit == base
    assert adopted.root_task_id
    assert _git(worktree, "rev-parse", "HEAD") == base


def test_explicit_start_gate_prevents_early_dispatch(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit app.py", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    assert kb.get_task(conn, executor).status == "todo"
    spawned = []
    before = kb.dispatch_once(
        conn, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id),
    )
    assert before.spawned == []
    assert spawned == []

    service.start_mission(conn, mid)
    assert kb.get_task(conn, executor).status == "ready"


def test_predispatch_recovery_blocks_orphaned_intent_before_respawn(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    mdb.begin_intent(
        conn, mid, task_id=executor, action="execute_task",
        details={"workspace": kb.get_task(conn, executor).workspace_path},
    )
    spawned = []
    result = kb.dispatch_once(
        conn, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id),
    )
    assert result.missions_reconciled == [mid]
    assert spawned == []
    assert mdb.get_mission(conn, mid).status == "blocked"
    assert kb.get_task(conn, executor).status == "ready"


def test_predispatch_recovery_leaves_live_intent_running(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid=?,claim_expires=?,started_at=? WHERE id=?",
            (os.getpid(), int(time.time()) + 300, int(time.time()), executor),
        )
    mdb.begin_intent(
        conn, mid, task_id=executor, action="execute_task",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
        details={"boot_id": service._current_boot_id()},
    )
    result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)
    assert result.missions_reconciled == []
    assert mdb.get_mission(conn, mid).status == "running"


def test_predispatch_recovery_rejects_intent_owned_by_another_run(
    mission_env, monkeypatch,
):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid=?,last_heartbeat_at=? WHERE id=?",
            (os.getpid(), int(time.time()), executor),
        )
    mdb.begin_intent(
        conn, mid, task_id=executor, action="old-run",
        run_id=int(claimed.current_run_id) + 1, claim_token=claimed.claim_lock,
    )
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *_args: {})
    monkeypatch.setattr(kb, "_worker_survived_termination", lambda _result: False)
    monkeypatch.setattr(
        service, "_remove_mission_containers", lambda *_args, **_kwargs: None,
    )
    result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)
    assert result.missions_reconciled == [mid]
    assert mdb.get_mission(conn, mid).status == "blocked"


def test_evidence_chain_detects_tampering(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    mdb.record_evidence(conn, mid, kind="one", status="passed", metadata={"x": 1})
    mdb.record_evidence(conn, mid, kind="two", status="passed", metadata={"x": 2})
    assert mdb.verify_evidence_chain(conn, mid)
    conn.execute("UPDATE mission_evidence SET status='failed' WHERE kind='one'")
    conn.commit()
    assert not mdb.verify_evidence_chain(conn, mid)


def test_fail_closed_verifier_and_local_commit_gate(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo, autonomy=4)
    mission = service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit app.py", body="Set VALUE to 2", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    worktree = Path(mission.worktree_path)
    (worktree / "app.py").write_text("VALUE = 2\n")
    assert kb.complete_task(conn, executor, result="edited", summary="edited")

    blocked = service.submit_verifier_verdict(
        conn,
        mid,
        {
            "verdict": "pass",
            "requirements_checked": 1,
            "requirements_failed": [],
            "evidence_missing": [],
            "recommended_action": "commit",
        },
    )
    assert blocked.status == "blocked"
    assert blocked.blocked_reason

    mdb.transition_mission(conn, mid, "running", phase="execution")
    service.record_command_result(
        conn, mid, command="verify-ok", cwd=str(worktree), exit_code=0,
        stdout="1 passed", stderr="",
    )
    gated = service.submit_verifier_verdict(
        conn,
        mid,
        {
            "verdict": "pass",
            "requirements_checked": 1,
            "requirements_failed": [],
            "evidence_missing": [],
            "recommended_action": "commit",
        },
    )
    assert gated.status == "committing"
    completed = service.commit_mission(conn, mid)
    assert completed.status == "succeeded"
    assert completed.verified_commit
    assert _git(Path(completed.worktree_path), "status", "--porcelain") == ""
    assert _git(repo, "status", "--porcelain") == ""


def test_scope_violation_blocks_verification(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    mission = service.prepare_mission(conn, mid)
    executor = service.add_execution_task(conn, mid, title="Edit", body="Edit", assignee="default")
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    worktree = Path(mission.worktree_path)
    (worktree / "other.py").write_text("BAD = True\n")
    service.record_command_result(
        conn, mid, command="verify-ok", cwd=str(worktree), exit_code=0,
    )
    report = service.deterministic_verification(conn, mid)
    assert not report["pass"]
    assert report["scope_violations"] == ["other.py"]


def test_worker_policy_is_fail_closed_and_records_file_hash(mission_env, monkeypatch):
    conn, repo = mission_env
    mid = _created(conn, repo)
    mission = service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    worktree = Path(mission.worktree_path)
    policy = {
        "mission_id": mid, "mission_role": "executor", "worktree_path": str(worktree),
        "allowed_paths": ["app.py"], "allowed_terminal_backends": ["docker"],
        "read_only": False,
    }
    monkeypatch.setenv("HERMES_MISSION_POLICY", json.dumps(policy))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("HERMES_KANBAN_DB", db_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", executor)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(claimed.claim_lock))
    monkeypatch.setenv("TERMINAL_CWD", str(worktree))
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
    from agent.mission_scope import (
        record_file_evidence,
        require_terminal,
        require_write_path,
        terminal_host_cwd,
    )

    require_write_path(worktree / "app.py")
    with pytest.raises(PermissionError, match="out-of-scope"):
        require_write_path(worktree / "other.py")
    with pytest.raises(PermissionError, match="Docker terminal backend"):
        require_terminal(worktree, "local")
    require_terminal("/workspace", "docker")
    assert terminal_host_cwd("/workspace/tests", "docker") == worktree / "tests"
    monkeypatch.setenv("TERMINAL_CWD", str(repo))
    with pytest.raises(PermissionError, match="does not match"):
        require_terminal("/workspace", "docker")
    monkeypatch.setenv("TERMINAL_CWD", str(worktree))
    with pytest.raises(PermissionError, match="background processes"):
        require_terminal("/workspace", "docker", background=True)
    (worktree / "app.py").write_text("VALUE = 7\n")
    evidence_id = record_file_evidence(
        [str(worktree / "app.py")], task_id=executor, action="write_file",
    )
    assert evidence_id is not None
    row = mdb.list_evidence(conn, mid)[-1]
    assert row["kind"] == "file_mutation"
    assert row["metadata"]["files"][0]["sha256"]

    service.pause_mission(conn, mid)
    with pytest.raises(PermissionError, match="no longer active"):
        require_write_path(worktree / "app.py")
    service.resume_mission(conn, mid)

    policy["read_only"] = True
    monkeypatch.setenv("HERMES_MISSION_POLICY", json.dumps(policy))
    with pytest.raises(PermissionError, match="read-only"):
        require_write_path(worktree / "app.py")
    with pytest.raises(PermissionError, match="no terminal capability"):
        require_terminal("/workspace", "docker")


def test_mission_spawn_pins_minimal_role_toolsets(mission_env, monkeypatch):
    conn, repo = mission_env
    mid = _created(conn, repo)
    mission = service.prepare_mission(conn, mid)
    executor_id = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    verifier_id = conn.execute(
        "SELECT id FROM tasks WHERE mission_id=? AND mission_role='verifier'", (mid,),
    ).fetchone()[0]
    captured = []

    class FakeProc:
        pid = 1234

    def fake_popen(cmd, *args, **kwargs):
        captured.append((list(cmd), dict(kwargs["env"])))
        return FakeProc()

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(service, "begin_task_intent", lambda task, board=None: None)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kb._default_spawn(kb.get_task(conn, executor_id), mission.worktree_path, board="default")
    kb._default_spawn(kb.get_task(conn, verifier_id), mission.worktree_path, board="default")

    executor_tools = captured[0][0][captured[0][0].index("--toolsets") + 1].split(",")
    verifier_tools = captured[1][0][captured[1][0].index("--toolsets") + 1].split(",")
    assert executor_tools == ["file", "todo", "terminal"]
    assert verifier_tools == ["file", "todo"]
    assert json.loads(captured[0][1]["HERMES_MISSION_POLICY"])["read_only"] is False
    assert json.loads(captured[1][1]["HERMES_MISSION_POLICY"])["read_only"] is True
    assert captured[0][1]["TERMINAL_ENV"] == "docker"
    assert captured[0][1]["TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE"] == "true"
    assert captured[0][1]["TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES"] == "false"
    assert captured[0][1]["TERMINAL_DOCKER_NETWORK"] == "false"


def test_level_three_requires_explicit_commit_approval(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo, autonomy=3)
    mission = service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    worktree = Path(mission.worktree_path)
    (worktree / "app.py").write_text("VALUE = 3\n")
    service.record_command_result(conn, mid, command="verify-ok", cwd=str(worktree), exit_code=0)
    assert kb.complete_task(conn, executor, result="edited", summary="edited")
    waiting = service.submit_verifier_verdict(
        conn, mid,
        {"verdict": "pass", "requirements_checked": 1, "requirements_failed": [],
         "evidence_missing": [], "recommended_action": "commit"},
    )
    assert waiting.status == "awaiting_approval"
    assert service.approve_commit(conn, mid).status == "committing"


def test_controller_tick_auto_verifies_and_commits_level_four(mission_env, monkeypatch):
    conn, repo = mission_env
    mid, worktree, _executor = _ready_for_verification(conn, repo, autonomy=4)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kwargs: _passing_verifier_response())
    result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)
    mission = mdb.get_mission(conn, mid)
    assert mission.status == "succeeded"
    assert mission.verified_commit == _git(worktree, "rev-parse", "HEAD")
    assert any(item == {"mission_id": mid, "action": "succeeded"} for item in result.mission_actions)
    verifier = conn.execute(
        "SELECT status FROM tasks WHERE mission_id=? AND mission_role='verifier'", (mid,),
    ).fetchone()
    assert verifier["status"] == "done"


def test_controller_tick_waits_for_level_three_explicit_commit(mission_env, monkeypatch):
    conn, repo = mission_env
    mid, _worktree, _executor = _ready_for_verification(conn, repo, autonomy=3)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kwargs: _passing_verifier_response())
    kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)
    waiting = mdb.get_mission(conn, mid)
    assert waiting.status == "awaiting_approval"
    assert waiting.verified_commit is None
    service.approve_commit(conn, mid)
    result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)
    approved = mdb.get_mission(conn, mid)
    assert approved.status == "committing"
    assert approved.verified_commit is None
    assert result.mission_actions == []
    assert service.commit_mission(conn, mid).status == "succeeded"


def test_controller_blocks_after_verifier_retry_budget(mission_env, monkeypatch):
    conn, repo = mission_env
    mid, _worktree, _executor = _ready_for_verification(
        conn, repo, autonomy=4, budget={"max_verifier_retries": 0},
    )
    def fail_verifier(**kwargs):
        raise RuntimeError("verifier unavailable")
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fail_verifier)
    result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)
    mission = mdb.get_mission(conn, mid)
    assert mission.status == "blocked"
    assert "after 1 attempts" in mission.blocked_reason
    assert {"mission_id": mid, "action": "verification_blocked"} in result.mission_actions


def test_controller_runs_after_board_dispatch_lock_is_released(mission_env, monkeypatch):
    conn, _repo = mission_env
    observed = []

    def probe_controller(probe_conn):
        with kb._dispatch_tick_lock(
            service._connection_db_path(probe_conn), fail_open=False,
        ) as held:
            observed.append(held)
        return []

    monkeypatch.setattr(service, "controller_tick", probe_controller)
    kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None)
    assert observed == [True]


def test_commit_gate_recovers_crash_after_git_commit(mission_env):
    conn, repo = mission_env
    mid, worktree, _executor = _ready_for_verification(conn, repo, autonomy=4)
    gated = service.submit_verifier_verdict(
        conn, mid,
        {"verdict": "pass", "requirements_checked": 1, "requirements_failed": [],
         "evidence_missing": [], "recommended_action": "commit"},
    )
    assert gated.status == "committing"
    _git(worktree, "add", "--all")
    _git(worktree, "commit", "-m", f"mission({mid}): Change app.py")
    durable_head = _git(worktree, "rev-parse", "HEAD")
    completed = service.commit_mission(conn, mid)
    assert completed.status == "succeeded"
    assert completed.verified_commit == durable_head
    assert _git(worktree, "rev-list", "--count", f"{completed.base_commit}..HEAD") == "1"


def test_run_scoped_completion_resolves_only_current_intent(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    current_id = mdb.begin_intent(
        conn, mid, task_id=executor, action="execute_task",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
    )
    old_id = mdb.begin_intent(
        conn, mid, task_id=executor, action="execute_task",
        run_id=int(claimed.current_run_id) + 1000, claim_token="old-claim",
    )
    assert kb.complete_task(
        conn, executor, expected_run_id=claimed.current_run_id,
        result="done", summary="done",
    )
    rows = {
        row["id"]: row for row in conn.execute(
            "SELECT id,status,resolved_at FROM mission_intents WHERE id IN (?,?)",
            (current_id, old_id),
        )
    }
    assert rows[current_id]["status"] == "completed"
    assert rows[current_id]["resolved_at"] is not None
    assert rows[old_id]["status"] == "open"
    assert rows[old_id]["resolved_at"] is None


def test_commit_rejects_changes_after_independent_verification(mission_env):
    conn, repo = mission_env
    mid, worktree, _executor = _ready_for_verification(conn, repo, autonomy=3)
    assert service.submit_verifier_verdict(conn, mid, _verdict()).status == "awaiting_approval"
    assert service.approve_commit(conn, mid).status == "committing"
    (worktree / "app.py").write_text("VALUE = 33\n")
    service.record_command_result(
        conn, mid, command="verify-ok", cwd=str(worktree), exit_code=0,
    )
    blocked = service.commit_mission(conn, mid)
    assert blocked.status == "blocked"
    assert blocked.blocked_reason == "worktree changed after independent verification"
    assert blocked.verified_commit is None
    assert _git(worktree, "rev-parse", "HEAD") == blocked.base_commit


def test_verdict_is_rejected_before_executor_completion(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    service.add_execution_task(conn, mid, title="Edit", body="Edit", assignee="default")
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    with pytest.raises(service.MissionError, match="all executor tasks"):
        service.submit_verifier_verdict(conn, mid, _verdict())
    assert mdb.get_mission(conn, mid).status == "running"
    assert not any(
        row["kind"] in {"independent_verifier", "verification_seal"}
        for row in mdb.list_evidence(conn, mid)
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"requirements_checked": True},
        {"requirements_checked": "1"},
        {"requirements_failed": "failure"},
        {"evidence_missing": None},
        {"recommended_action": "push"},
        {"requirements_failed": [1]},
        {"extra": "field"},
    ],
)
def test_malformed_verdict_is_rejected_without_state_change(mission_env, overrides):
    conn, repo = mission_env
    mid, _worktree, _executor = _ready_for_verification(conn, repo)
    with pytest.raises(service.MissionError, match="malformed verifier verdict"):
        service.submit_verifier_verdict(conn, mid, _verdict(**overrides))
    assert mdb.get_mission(conn, mid).status == "running"
    assert not any(
        row["kind"] in {"independent_verifier", "verification_seal"}
        for row in mdb.list_evidence(conn, mid)
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"requirements_checked": 0},
        {"requirements_failed": ["criterion failed"]},
        {"evidence_missing": ["test log"]},
        {"recommended_action": "replan"},
        {"verdict": "fail", "recommended_action": "replan"},
    ],
)
def test_coherent_nonpassing_verdict_blocks_without_seal(mission_env, overrides):
    conn, repo = mission_env
    mid, _worktree, _executor = _ready_for_verification(conn, repo)
    blocked = service.submit_verifier_verdict(conn, mid, _verdict(**overrides))
    assert blocked.status == "blocked"
    evidence = mdb.list_evidence(conn, mid)
    assert evidence[-1]["kind"] == "independent_verifier"
    assert evidence[-1]["status"] == "failed"
    assert not any(row["kind"] == "verification_seal" for row in evidence)


def test_evidence_chain_detects_corrupted_blob(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    mission = service.prepare_mission(conn, mid)
    evidence_id = service.record_command_result(
        conn, mid, command="verify-ok", cwd=mission.worktree_path,
        exit_code=0, stdout="passed",
    )
    row = conn.execute(
        "SELECT blob_path FROM mission_evidence WHERE id=?", (evidence_id,),
    ).fetchone()
    assert mdb.verify_evidence_chain(conn, mid)
    Path(row["blob_path"]).write_bytes(b"not a gzip stream")
    assert not mdb.verify_evidence_chain(conn, mid)


@pytest.mark.parametrize("mode", ["spoof", "multiple"])
def test_commit_recovery_rejects_unattributable_history(mission_env, mode):
    conn, repo = mission_env
    mid, worktree, _executor = _ready_for_verification(conn, repo, autonomy=4)
    assert service.submit_verifier_verdict(conn, mid, _verdict()).status == "committing"
    _git(worktree, "add", "--all")
    subject = (
        f"mission({mid}): forged by executor"
        if mode == "spoof"
        else f"mission({mid}): Change app.py"
    )
    _git(worktree, "commit", "-m", subject)
    if mode == "multiple":
        _git(worktree, "commit", "--allow-empty", "-m", "extra")
    blocked = service.commit_mission(conn, mid)
    assert blocked.status == "blocked"
    assert blocked.blocked_reason == "worktree HEAD advanced outside the mission commit gate"
    assert blocked.verified_commit is None
    assert not any(row["kind"] == "commit" for row in mdb.list_evidence(conn, mid))


def test_second_reconciliation_blocks_intent_orphaned_during_reclaim(
    mission_env, monkeypatch,
):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid=?,last_heartbeat_at=? WHERE id=?",
            (os.getpid(), int(time.time()), executor),
        )
    mdb.begin_intent(
        conn, mid, task_id=executor, action="execute_task",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
    )

    def fake_timeout(timeout_conn):
        with kb.write_txn(timeout_conn):
            timeout_conn.execute(
                """UPDATE tasks SET status='ready',claim_lock=NULL,
                          claim_expires=NULL,worker_pid=NULL WHERE id=?""",
                (executor,),
            )
        return [executor]

    monkeypatch.setattr(kb, "enforce_max_runtime", fake_timeout)
    spawned = []
    result = kb.dispatch_once(
        conn, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id),
    )
    assert result.timed_out == [executor]
    assert result.missions_reconciled == [mid]
    assert spawned == []
    assert kb.get_task(conn, executor).status == "ready"
    assert mdb.get_mission(conn, mid).status == "blocked"


def test_dry_run_does_not_advance_or_reconcile_missions(mission_env, monkeypatch):
    conn, repo = mission_env
    mid, worktree, executor = _ready_for_verification(conn, repo)
    mdb.begin_intent(conn, mid, task_id=executor, action="stale-test")
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: pytest.fail("dry-run invoked mission verifier"),
    )
    before_mission = mdb.get_mission(conn, mid)
    before_evidence = mdb.list_evidence(conn, mid)
    before_intents = mdb.open_intents(conn, mid)
    before_head = _git(worktree, "rev-parse", "HEAD")
    result = kb.dispatch_once(
        conn, spawn_fn=lambda *args, **kwargs: pytest.fail("dry-run spawned"),
        dry_run=True,
    )
    assert result.mission_actions == []
    assert mdb.get_mission(conn, mid) == before_mission
    assert mdb.list_evidence(conn, mid) == before_evidence
    assert mdb.open_intents(conn, mid) == before_intents
    assert _git(worktree, "rev-parse", "HEAD") == before_head


def test_paused_mission_cannot_be_claimed(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    service.pause_mission(conn, mid)
    assert kb.get_task(conn, executor).status == "ready"
    assert kb.claim_task(conn, executor) is None


def test_cancel_quiesces_claim_and_resolves_exact_intent(mission_env, monkeypatch):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    intent_id = mdb.begin_intent(
        conn, mid, task_id=executor, action="execute_task",
        run_id=claimed.current_run_id, claim_token=claimed.claim_lock,
    )
    removed = []
    monkeypatch.setattr(
        service, "_remove_mission_containers",
        lambda _conn, _mission_id, task_ids: removed.extend(task_ids),
    )
    cancelled = service.cancel_mission(conn, mid)
    assert cancelled.status == "cancelled"
    assert removed == [executor]
    task = kb.get_task(conn, executor)
    assert task.status == "blocked"
    assert task.claim_lock is None and task.worker_pid is None
    intent = conn.execute(
        "SELECT status,resolved_at FROM mission_intents WHERE id=?", (intent_id,),
    ).fetchone()
    assert intent["status"] == "cancelled"
    assert intent["resolved_at"] is not None


@pytest.mark.parametrize(
    "binding", ["current", "legacy", "legacy_wrong_mount", "mismatched"],
)
def test_container_cleanup_requires_exact_board_mission_task_and_run_binding(
    mission_env, monkeypatch, binding,
):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None

    from tools.environments import docker as docker_env

    container_id = "a" * 64
    removed = False
    calls = []
    db_label = docker_env._mission_db_fingerprint(str(service._connection_db_path(conn)))
    labels = {
        "hermes-agent": "1",
        "hermes-task-id": executor,
        "hermes-mission-id": "m_wrong" if binding == "mismatched" else mid,
        "hermes-board": "default",
        "hermes-board-db": db_label,
        "hermes-run-id": str(claimed.current_run_id),
    }
    if binding.startswith("legacy"):
        for key in (
            "hermes-mission-id", "hermes-board", "hermes-board-db",
            "hermes-run-id",
        ):
            labels.pop(key)
    mounts = [{
        "Type": "bind",
        "Source": (
            str(repo) if binding == "legacy_wrong_mount"
            else mdb.get_mission(conn, mid).worktree_path
        ),
        "Destination": "/workspace",
    }]

    def fake_run(cmd, **kwargs):
        nonlocal removed
        calls.append(list(cmd))
        if cmd[1:3] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=("" if removed else container_id + "\n"), stderr="",
            )
        if cmd[1] == "inspect":
            payload = mounts if ".Mounts" in cmd[3] else labels
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        if cmd[1:3] == ["rm", "-f"]:
            removed = True
            return subprocess.CompletedProcess(cmd, 0, stdout=container_id + "\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(service.subprocess, "run", fake_run)

    if binding in {"mismatched", "legacy_wrong_mount"}:
        with pytest.raises(service.MissionError, match="mismatched mission binding"):
            service._remove_mission_containers(conn, mid, [executor])
        assert not removed
    else:
        service._remove_mission_containers(conn, mid, [executor])
        assert removed
        probe = next(cmd for cmd in calls if cmd[1:3] == ["ps", "-aq"])
        assert "label=hermes-agent=1" in probe
        assert f"label=hermes-task-id={executor}" in probe
        assert not any("hermes-mission-id" in part for part in probe)


def test_pause_quiesces_active_executor_and_resume_only_requeues_pause_marker(
    mission_env, monkeypatch,
):
    conn, repo = mission_env
    mid = _created(conn, repo)
    prepared = service.prepare_mission(conn, mid)
    active = service.add_execution_task(
        conn, mid, title="Active edit", body="Edit", assignee="default",
    )
    genuinely_blocked = service.add_execution_task(
        conn,
        mid,
        title="Needs a decision",
        body="Wait",
        assignee="default",
        parents=[prepared.root_task_id],
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, active)
    assert claimed is not None
    intent_id = mdb.begin_intent(
        conn,
        mid,
        task_id=active,
        action="execute_task",
        run_id=claimed.current_run_id,
        claim_token=claimed.claim_lock,
    )
    assert kb.block_task(
        conn,
        genuinely_blocked,
        reason="real human decision required",
        kind="needs_input",
    )

    removed = []
    monkeypatch.setattr(
        service, "_remove_mission_containers",
        lambda _conn, _mission_id, task_ids: removed.extend(task_ids),
    )
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda _pid, _claim: {"survived": False},
    )
    monkeypatch.setattr(kb, "_worker_survived_termination", lambda _result: False)

    paused = service.pause_mission(conn, mid)
    assert paused.status == "waiting_for_user"
    assert set(removed) == {active, genuinely_blocked}
    assert kb.get_task(conn, active).status == "blocked"
    assert kb.get_task(conn, active).claim_lock is None
    assert kb.get_task(conn, genuinely_blocked).status == "blocked"
    intent = conn.execute(
        "SELECT status,resolved_at FROM mission_intents WHERE id=?", (intent_id,),
    ).fetchone()
    assert intent["status"] == "paused"
    assert intent["resolved_at"] is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='mission_paused'",
        (active,),
    ).fetchone()[0] == 1

    resumed = service.resume_mission(conn, mid)
    assert resumed.status == "running"
    assert kb.get_task(conn, active).status == "ready"
    assert kb.get_task(conn, genuinely_blocked).status == "blocked"
    assert conn.execute(
        "SELECT status FROM mission_intents WHERE id=?", (intent_id,),
    ).fetchone()[0] == "resumed"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='mission_resumed'",
        (active,),
    ).fetchone()[0] == 1


def test_pause_revokes_mission_state_before_quiescence_and_resume_recovers(
    mission_env, monkeypatch,
):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)

    real_quiesce = service._quiesce_mission_executors
    monkeypatch.setattr(
        service,
        "_quiesce_mission_executors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            service.MissionError("simulated controller interruption")
        ),
    )
    with pytest.raises(service.MissionError, match="simulated controller interruption"):
        service.pause_mission(conn, mid)
    interrupted = mdb.get_mission(conn, mid)
    assert interrupted.status == "waiting_for_user"
    assert interrupted.phase == "pausing"

    monkeypatch.setattr(service, "_quiesce_mission_executors", real_quiesce)
    resumed = service.resume_mission(conn, mid)
    assert resumed.status == "running"
    assert resumed.phase == "execution"


def test_restart_reconciliation_finishes_durable_pausing_state(
    mission_env, monkeypatch,
):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    intent_id = mdb.begin_intent(
        conn,
        mid,
        task_id=executor,
        action="execute_task",
        run_id=claimed.current_run_id,
        claim_token=claimed.claim_lock,
    )
    mdb.transition_mission(
        conn,
        mid,
        "waiting_for_user",
        phase="pausing",
        blocked_reason="pause in progress",
    )
    removed = []
    monkeypatch.setattr(
        service, "_remove_mission_containers",
        lambda _conn, _mission_id, task_ids: removed.extend(task_ids),
    )
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *_args: {})
    monkeypatch.setattr(kb, "_worker_survived_termination", lambda _result: False)

    assert service.reconcile_active_missions(conn) == [mid]
    recovered = mdb.get_mission(conn, mid)
    assert recovered.status == "waiting_for_user"
    assert recovered.phase == "paused"
    assert removed == [executor]
    assert kb.get_task(conn, executor).status == "blocked"
    assert conn.execute(
        "SELECT status FROM mission_intents WHERE id=?", (intent_id,),
    ).fetchone()[0] == "paused"


def test_restart_reconciliation_finishes_durable_cancelling_state(
    mission_env, monkeypatch,
):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    intent_id = mdb.begin_intent(
        conn,
        mid,
        task_id=executor,
        action="execute_task",
        run_id=claimed.current_run_id,
        claim_token=claimed.claim_lock,
    )
    mdb.transition_mission(
        conn,
        mid,
        "waiting_for_user",
        phase="cancelling",
        blocked_reason="cancellation in progress",
    )
    removed = []
    monkeypatch.setattr(
        service, "_remove_mission_containers",
        lambda _conn, _mission_id, task_ids: removed.extend(task_ids),
    )
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *_args: {})
    monkeypatch.setattr(kb, "_worker_survived_termination", lambda _result: False)

    assert service.reconcile_active_missions(conn) == [mid]
    recovered = mdb.get_mission(conn, mid)
    assert recovered.status == "cancelled"
    assert recovered.phase == "cancelled"
    assert removed == [executor]
    assert kb.get_task(conn, executor).status == "blocked"
    assert conn.execute(
        "SELECT status FROM mission_intents WHERE id=?", (intent_id,),
    ).fetchone()[0] == "cancelled"


def test_blocked_reconciliation_removes_orphan_container_once_per_run_generation(
    mission_env, monkeypatch,
):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    claimed = kb.claim_task(conn, executor)
    assert claimed is not None
    mdb.begin_intent(
        conn,
        mid,
        task_id=executor,
        action="execute_task",
        run_id=claimed.current_run_id,
        claim_token=claimed.claim_lock,
    )
    mdb.transition_mission(
        conn,
        mid,
        "blocked",
        phase="recovery",
        blocked_reason="worker disappeared",
    )
    # Simulate crash detection clearing the live DB fields while the Docker
    # command/container from that run remains orphaned outside SQLite.
    assert kb.block_task(
        conn,
        executor,
        reason="worker disappeared",
        kind="transient",
        expected_run_id=claimed.current_run_id,
    )
    removed = []
    monkeypatch.setattr(
        service, "_remove_mission_containers",
        lambda _conn, _mission_id, task_ids: removed.extend(task_ids),
    )

    assert service.reconcile_active_missions(conn) == [mid]
    assert removed == [executor]
    assert not service._mission_needs_quiescence(conn, mid)
    assert any(
        row["kind"] == "quiescence" and row["status"] == "passed"
        for row in mdb.list_evidence(conn, mid)
    )
    assert service.reconcile_active_missions(conn) == []
    assert removed == [executor]


def test_pause_cancel_and_verified_rollback(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo, autonomy=4)
    mission = service.prepare_mission(conn, mid)
    executor = service.add_execution_task(conn, mid, title="Edit", body="Edit", assignee="default")
    service.finish_plan(conn, mid, verifier_assignee="default")
    assert service.start_mission(conn, mid).status == "running"
    assert service.pause_mission(conn, mid).status == "waiting_for_user"
    assert service.resume_mission(conn, mid).status == "running"
    worktree = Path(mission.worktree_path)
    (worktree / "app.py").write_text("VALUE = 9\n")
    service.record_command_result(conn, mid, command="verify-ok", cwd=str(worktree), exit_code=0)
    assert kb.complete_task(conn, executor, result="edited", summary="edited")
    gated = service.submit_verifier_verdict(
        conn, mid,
        {"verdict": "pass", "requirements_checked": 1, "requirements_failed": [],
         "evidence_missing": [], "recommended_action": "commit"},
    )
    assert gated.status == "committing"
    committed = service.commit_mission(conn, mid)
    assert committed.status == "succeeded"
    rolled_back = service.rollback_mission(conn, mid)
    assert rolled_back.status == "rolled_back"
    assert (worktree / "app.py").read_text() == "VALUE = 1\n"
    assert _git(repo, "status", "--porcelain") == ""

    cancel_id = _created(conn, repo)
    assert service.cancel_mission(conn, cancel_id).status == "cancelled"


def test_public_creation_requires_registered_clean_project(mission_env):
    conn, repo = mission_env
    from hermes_cli import projects_db as pdb

    with pytest.raises(service.MissionError, match="registered Hermes project"):
        service.create_mission(
            conn, objective="No project", contract=_contract(repo, allow_local_commit=False),
            autonomy_level=2, repo_path=str(repo), board="default", project_id="",
        )
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(project_conn, name="Mission Project", primary_path=str(repo))
    (repo / "dirty.txt").write_text("dirty\n")
    with pytest.raises(service.MissionError, match="source checkout must be clean"):
        service.create_mission(
            conn, objective="Dirty", contract=_contract(repo, allow_local_commit=False),
            autonomy_level=2, repo_path=str(repo), board="default", project_id=project_id,
        )
    (repo / "dirty.txt").unlink()
    created = service.create_mission(
        conn, objective="Prepared", contract=_contract(repo, allow_local_commit=False),
        autonomy_level=2, repo_path=str(repo), board="default", project_id=project_id,
    )
    assert created.status == "planning"
    assert created.project_id == project_id
    assert Path(created.worktree_path).is_dir()


def test_level_four_requires_explicit_local_commit_authority(mission_env):
    conn, repo = mission_env
    mid = mdb.create_mission(
        conn, objective="No implicit commit",
        contract=_contract(repo, allow_local_commit=False),
        autonomy_level=4, repo_path=str(repo), board="default",
    )
    mission = service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Edit", body="Edit", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    worktree = Path(mission.worktree_path)
    (worktree / "app.py").write_text("VALUE = 8\n")
    service.record_command_result(conn, mid, command="verify-ok", cwd=str(worktree), exit_code=0)
    assert kb.complete_task(conn, executor, result="edited", summary="edited")
    blocked = service.submit_verifier_verdict(conn, mid, _verdict())
    assert blocked.status == "blocked"
    assert blocked.blocked_reason == "autonomy level 4 lacks explicit local-commit authority"


def test_operator_deny_and_safe_retry(mission_env):
    conn, repo = mission_env
    mid, _worktree, _executor = _ready_for_verification(conn, repo, autonomy=3)
    waiting = service.submit_verifier_verdict(conn, mid, _verdict())
    assert waiting.status == "awaiting_approval"
    denied = service.deny_mission(conn, mid)
    assert denied.status == "blocked"
    assert denied.blocked_reason == "local commit denied by operator"
    retried = service.retry_mission(conn, mid)
    assert retried.status == "running"
    actions = [row["metadata"].get("action") for row in mdb.list_evidence(conn, mid)]
    assert "deny" in actions
    assert "retry" in actions


def test_execution_retry_requeues_blocked_executor_via_controller(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo, autonomy=3)
    service.prepare_mission(conn, mid)
    executor = service.add_execution_task(
        conn, mid, title="Needs correction", body="Edit app.py", assignee="default",
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    service.start_mission(conn, mid)
    assert kb.block_task(
        conn, executor, reason="path correction required", kind="needs_input",
    )
    actions = service.controller_tick(conn)
    assert {item["action"] for item in actions} == {"executor_blocked"}
    assert mdb.get_mission(conn, mid).status == "blocked"

    retried = service.retry_mission(conn, mid)

    assert retried.status == "running"
    task = kb.get_task(conn, executor)
    assert task is not None
    assert task.status == "ready"
    assert task.block_kind == "needs_input"
    assert task.block_recurrences == 1
    evidence = mdb.list_evidence(conn, mid)
    retry = next(row for row in reversed(evidence) if row["metadata"].get("action") == "retry")
    assert retry["metadata"]["retried_tasks"] == [executor]
    events = conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (executor,),
    ).fetchall()
    assert any(row["kind"] == "unblocked" for row in events)


def test_mission_report_exposes_durable_task_graph(mission_env):
    conn, repo = mission_env
    mid = _created(conn, repo)
    service.prepare_mission(conn, mid)
    first = service.add_execution_task(conn, mid, title="First", body="First", assignee="default")
    second = service.add_execution_task(
        conn, mid, title="Second", body="Second", assignee="default", parents=[first],
    )
    service.finish_plan(conn, mid, verifier_assignee="default")
    report = service.mission_report(conn, mid)
    links = {(row["parent_id"], row["child_id"]) for row in report["links"]}
    assert (first, second) in links
    assert all(
        conn.execute("SELECT mission_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == mid
        for edge in report["links"] for task_id in (edge["parent_id"], edge["child_id"])
    )


def test_slash_and_cli_adapter_share_state(mission_env):
    _conn, repo = mission_env
    from hermes_cli import projects_db as pdb
    from hermes_cli.missions import run_slash

    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(project_conn, name="Adapter Project", primary_path=str(repo))
    out = run_slash(
        "create 'Adapter mission' --outcome 'Done' --verify verify-ok "
        f"--allowed-root {shlex_quote(str(repo))} --allowed-path app.py "
        f"--autonomy 3 --repo {shlex_quote(str(repo))} --project {project_id} --json"
    )
    payload = json.loads(out)
    listed = json.loads(run_slash("list --json"))
    assert listed[0]["id"] == payload["id"]


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)
