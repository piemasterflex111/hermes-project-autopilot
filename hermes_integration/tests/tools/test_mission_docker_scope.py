"""Regression coverage for the mission Docker/worktree security boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agent import mission_scope
from tools import file_tools, terminal_tool
from tools.environments import docker as docker_env


def _policy(worktree: Path) -> str:
    return json.dumps(
        {
            "mission_id": "mission-scope-test",
            "mission_role": "executor",
            "worktree_path": str(worktree),
            "allowed_paths": [],
            "allowed_terminal_backends": ["docker"],
            "read_only": False,
        }
    )


def test_mission_config_scrubs_profile_docker_escape_vectors(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HERMES_MISSION_POLICY", _policy(worktree))
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "/unapproved")
    # Malformed JSON is intentional: mission mode must not parse unrelated
    # profile-controlled mount/env/argument values before replacing them.
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "{malformed")
    monkeypatch.setenv("TERMINAL_DOCKER_FORWARD_ENV", "{malformed")
    monkeypatch.setenv("TERMINAL_DOCKER_ENV", "{malformed")
    monkeypatch.setenv("TERMINAL_DOCKER_EXTRA_ARGS", "{malformed")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_NETWORK", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false")
    monkeypatch.setenv("TERMINAL_DOCKER_RUN_AS_HOST_USER", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_ORPHAN_REAPER", "true")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "prod.example")
    monkeypatch.setenv("TERMINAL_SSH_USER", "root")
    monkeypatch.setenv("TERMINAL_SSH_KEY", "/host/id_ed25519")

    config = terminal_tool._get_env_config()

    assert config["mission_restricted"] is True
    assert config["env_type"] == "docker"
    assert config["cwd"] == "/workspace"
    assert config["host_cwd"] == str(worktree.resolve())
    assert config["docker_mount_cwd_to_workspace"] is True
    assert config["container_persistent"] is False
    assert config["docker_persist_across_processes"] is False
    assert config["docker_network"] is False
    assert config["docker_orphan_reaper"] is False
    assert config["docker_run_as_host_user"] is False
    assert config["docker_volumes"] == []
    assert config["docker_forward_env"] == []
    assert config["docker_env"] == {}
    assert config["docker_extra_args"] == []
    assert config["docker_restricted_mode"] is True
    assert config["ssh_host"] == config["ssh_user"] == config["ssh_key"] == ""


def test_environment_factory_forces_restrictions_when_caller_omits_them(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HERMES_MISSION_POLICY", _policy(worktree))
    captured = {}

    def fake_docker_environment(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(terminal_tool, "_DockerEnvironment", fake_docker_environment)
    monkeypatch.setattr(terminal_tool, "_maybe_reap_docker_orphans", lambda config: None)

    terminal_tool._create_environment(
        env_type="docker",
        image="python:3.11",
        cwd=str(worktree),
        timeout=60,
        container_config={
            "container_persistent": True,
            "docker_volumes": ["/:/host"],
            "docker_network": True,
        },
        host_cwd="/unapproved",
    )

    assert captured["cwd"] == "/workspace"
    assert captured["host_cwd"] == str(worktree)
    assert captured["volumes"] == []
    assert captured["network"] is False
    assert captured["persistent_filesystem"] is False
    assert captured["auto_mount_cwd"] is True
    assert captured["restricted_mode"] is True
    assert captured["persist_across_processes"] is False


def test_mission_file_paths_map_both_directions_before_first_command(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    nested = worktree / "src"
    nested.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MISSION_POLICY", _policy(worktree))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "mission-task")
    monkeypatch.setenv("TERMINAL_CWD", str(worktree))
    terminal_tool.clear_session_cwd("mission-task")

    assert file_tools._authoritative_workspace_root("mission-task") == "/workspace"
    assert str(file_tools._resolve_path_for_task("src/app.py", "mission-task")) == (
        "/workspace/src/app.py"
    )
    assert str(
        file_tools._resolve_path_for_task(str(worktree / "src" / "app.py"), "mission-task")
    ) == "/workspace/src/app.py"
    assert mission_scope.mission_host_path("/workspace/src/app.py") == (
        worktree / "src" / "app.py"
    ).resolve()

    terminal_tool.record_session_cwd("mission-task", str(nested))
    try:
        assert str(file_tools._resolve_path_for_task("app.py", "mission-task")) == (
            "/workspace/src/app.py"
        )
    finally:
        terminal_tool.clear_session_cwd("mission-task")


def test_write_scope_validates_container_paths_against_host_worktree(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HERMES_MISSION_POLICY", _policy(worktree))
    monkeypatch.setattr(mission_scope, "_require_live_executor_lease", lambda policy: None)

    mission_scope.require_write_path("/workspace/app.py")
    mission_scope.require_write_path(worktree / "app.py")
    with pytest.raises(PermissionError, match="outside"):
        mission_scope.require_write_path("/workspace/../etc/passwd")


class _ReadResult:
    def __init__(self, content: str):
        self.content = content

    def to_dict(self):
        return {
            "content": self.content,
            "total_lines": 1,
            "file_size": len(self.content),
            "truncated": False,
            "error": None,
        }


def test_first_mission_read_operates_in_container_and_stats_host(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MISSION_POLICY", _policy(worktree))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "read-task")
    monkeypatch.setenv("TERMINAL_CWD", str(worktree))
    terminal_tool.clear_session_cwd("read-task")
    calls: list[str] = []

    class FakeOps:
        def read_file(self, path, offset, limit):
            calls.append(str(path))
            return _ReadResult("1|VALUE = 1")

    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: FakeOps())
    file_tools._read_tracker.pop("read-task", None)
    try:
        result = json.loads(file_tools.read_file_tool("app.py", task_id="read-task"))
    finally:
        terminal_tool.clear_session_cwd("read-task")
        file_tools._read_tracker.pop("read-task", None)

    assert result["error"] is None
    assert calls == ["/workspace/app.py"]


def test_mission_file_reads_cannot_escape_worktree_via_host_metadata(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HERMES_MISSION_POLICY", _policy(worktree))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "outside-read-task")

    result = json.loads(
        file_tools.read_file_tool("/etc/passwd", task_id="outside-read-task")
    )

    assert "outside" in result["error"]


def test_file_evidence_hashes_host_file_for_container_visible_path(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    content = b"VALUE = 9\n"
    (worktree / "app.py").write_bytes(content)
    monkeypatch.setenv("HERMES_MISSION_POLICY", _policy(worktree))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(mission_scope, "_require_live_executor_lease", lambda policy: None)
    captured = {}

    from hermes_cli import missions_db as mdb

    def fake_record(conn, mission_id, **kwargs):
        captured.update(kwargs)
        return 41

    monkeypatch.setattr(mdb, "record_evidence", fake_record)

    evidence_id = mission_scope.record_file_evidence(
        ["/workspace/app.py"], task_id="task", action="write_file"
    )

    assert evidence_id == 41
    file_record = captured["metadata"]["files"][0]
    assert file_record["path"] == str((worktree / "app.py").resolve())
    assert file_record["sha256"] == hashlib.sha256(content).hexdigest()
    assert file_record["bytes"] == len(content)


def test_restricted_docker_constructor_drops_implicit_and_explicit_host_access(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    credential = tmp_path / "token.json"
    credential.write_text("secret", encoding="utf-8")
    skill_dir = tmp_path / "skills"
    cache_dir = tmp_path / "cache"
    skill_dir.mkdir()
    cache_dir.mkdir()
    calls = []
    monkeypatch.setenv("HERMES_MISSION_POLICY", _policy(worktree))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "mission-task")

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if len(cmd) > 1 and cmd[1] == "run":
            return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env, "_cgroup_limits_ok", True)
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_env.DockerEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [{"host_path": str(credential), "container_path": "/root/token.json"}],
    )
    monkeypatch.setattr(
        "tools.credential_files.get_skills_directory_mount",
        lambda: [{"host_path": str(skill_dir), "container_path": "/root/skills"}],
    )
    monkeypatch.setattr(
        "tools.credential_files.get_cache_directory_mounts",
        lambda: [{"host_path": str(cache_dir), "container_path": "/root/cache"}],
    )
    monkeypatch.setattr(
        docker_env,
        "_egress_proxy_args_for_docker",
        lambda: (["-v", "/host/ca.pem:/ca.pem:ro"], {"HTTPS_PROXY": "secret"}, ["--add-host", "escape:host-gateway"]),
    )
    monkeypatch.setattr("tools.env_passthrough.get_all_passthrough", lambda: ["PROFILE_SECRET"])
    monkeypatch.setenv("PROFILE_SECRET", "do-not-forward")

    env = docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        timeout=60,
        task_id="mission-task",
        persistent_filesystem=True,
        volumes=["/:/host"],
        forward_env=["PROFILE_SECRET"],
        env={"PROFILE_SECRET": "explicit"},
        network=True,
        host_cwd=str(worktree),
        auto_mount_cwd=True,
        run_as_host_user=True,
        extra_args=["--privileged", "--network=host"],
        persist_across_processes=True,
        restricted_mode=True,
    )

    run_cmd = next(cmd for cmd in calls if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    rendered = " ".join(run_cmd)
    assert f"{worktree}:/workspace" in rendered
    assert "--network=none" in run_cmd
    assert "--privileged" not in run_cmd
    assert "--network=host" not in run_cmd
    assert "/:/host" not in rendered
    assert str(credential) not in rendered
    assert str(skill_dir) not in rendered
    assert str(cache_dir) not in rendered
    assert "/host/ca.pem" not in rendered
    assert "escape:host-gateway" not in rendered
    assert "PROFILE_SECRET" not in rendered
    assert "GIT_CONFIG_GLOBAL=/dev/null" in run_cmd
    assert "hermes-mission-id=mission-scope-test" in run_cmd
    assert "hermes-board=default" in run_cmd
    assert "hermes-run-id=17" in run_cmd
    assert "hermes-board-db=" + docker_env._mission_db_fingerprint(
        str(tmp_path / "kanban.db")
    ) in run_cmd
    assert env._persistent is False
    assert env._persist_across_processes is False


def test_restricted_docker_requires_exact_worktree_mount(tmp_path):
    with pytest.raises(ValueError, match="worktree mounted at /workspace"):
        docker_env.DockerEnvironment(
            image="python:3.11",
            cwd="/root",
            host_cwd=str(tmp_path),
            auto_mount_cwd=False,
            restricted_mode=True,
        )
