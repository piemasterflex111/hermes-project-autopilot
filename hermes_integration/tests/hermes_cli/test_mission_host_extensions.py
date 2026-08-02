from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from types import SimpleNamespace


def _task(kb, task_id: str, *, mission: bool):
    return kb.Task(
        id=task_id,
        title="host extension isolation",
        body=None,
        assignee="default",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="claim",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
        mission_id="m_isolated" if mission else None,
        mission_role="verifier" if mission else None,
    )


def test_mission_spawn_disables_host_extensions_but_ordinary_worker_keeps_hooks(
    monkeypatch, tmp_path: Path,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli import missions_db as mdb

    root = tmp_path / ".hermes"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)

    mission = SimpleNamespace(
        id="m_isolated",
        board="default",
        autonomy_level=4,
        worktree_path=str(workspace),
        contract=SimpleNamespace(
            boundaries={
                "allowed_paths": ["."],
                "network_destinations": [],
                "allowed_terminal_backends": ["docker"],
            }
        ),
    )
    monkeypatch.setattr(kb, "connect_closing", lambda board=None: contextlib.nullcontext(object()))
    monkeypatch.setattr(mdb, "get_mission", lambda _conn, _mid: mission)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: root / "kanban.db")
    monkeypatch.setattr(kb, "workspaces_root", lambda board=None: root / "workspaces")
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: root / "logs")

    spawned: list[tuple[list[str], dict[str, str]]] = []

    class Proc:
        pid = 1234

    def fake_popen(cmd, *args, **kwargs):
        spawned.append((list(cmd), dict(kwargs["env"])))
        return Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    kb._default_spawn(_task(kb, "t_mission", mission=True), str(workspace), board="default")
    kb._default_spawn(_task(kb, "t_ordinary", mission=False), str(workspace), board="default")

    mission_cmd, mission_env = spawned[0]
    ordinary_cmd, ordinary_env = spawned[1]
    assert mission_env["HERMES_SAFE_MODE"] == "1"
    assert "HERMES_ACCEPT_HOOKS" not in mission_env
    assert "--accept-hooks" not in mission_cmd
    # Do not pass CLI --safe-mode: mission workers still need their assigned
    # profile's model/provider settings; the internal env switch isolates only
    # host-side extensions and MCP startup.
    assert "--safe-mode" not in mission_cmd

    assert "HERMES_SAFE_MODE" not in ordinary_env
    assert ordinary_env["HERMES_ACCEPT_HOOKS"] == "1"
    assert "--accept-hooks" in ordinary_cmd


def test_mission_policy_blocks_late_hook_and_python_plugin_execution(monkeypatch, tmp_path):
    from agent import shell_hooks
    from hermes_cli.plugins import PluginManager
    import plugins.context_engine as context_plugins
    import plugins.memory as memory_plugins
    import providers

    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    monkeypatch.setenv("HERMES_MISSION_POLICY", "{}")

    plugin_calls: list[str] = []
    manager = PluginManager()
    manager._hooks["pre_tool_call"] = [lambda **_kwargs: plugin_calls.append("hook")]
    manager._middleware["tool"] = [lambda **_kwargs: plugin_calls.append("middleware")]
    monkeypatch.setattr(manager, "_discover_and_load_inner", lambda: plugin_calls.append("load"))

    manager.discover_and_load()
    assert manager.invoke_hook("pre_tool_call") == []
    assert manager.invoke_middleware("tool") == []
    assert manager.has_hook("pre_tool_call") is False
    assert manager.has_middleware("tool") is False
    assert plugin_calls == []

    shell_calls: list[str] = []
    monkeypatch.setattr(
        shell_hooks.subprocess,
        "run",
        lambda *_args, **_kwargs: shell_calls.append("spawn"),
    )
    spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command="user-hook")
    assert shell_hooks.register_from_config(
        {"hooks": {"pre_tool_call": [{"command": "user-hook"}]}},
        accept_hooks=True,
    ) == []
    result = shell_hooks.run_once(spec, {"tool_name": "terminal", "args": {}})
    assert result["error"] == "host-side extension execution is disabled"
    assert shell_calls == []

    def unexpected(*_args, **_kwargs):
        raise AssertionError("plugin loader reached under mission policy")

    bundled_root = tmp_path / "bundled-providers"
    bundled_profile = bundled_root / "trusted"
    bundled_profile.mkdir(parents=True)
    provider_imports: list[tuple[Path, str]] = []
    monkeypatch.setattr(providers, "_BUNDLED_PLUGINS_DIR", bundled_root)
    monkeypatch.setattr(
        providers,
        "_import_plugin_dir",
        lambda path, source: provider_imports.append((path, source)),
    )
    monkeypatch.setattr(providers, "_user_plugins_dir", unexpected)
    monkeypatch.setattr(providers.importlib, "import_module", unexpected)
    monkeypatch.setattr(providers, "_discovered", False)
    providers._discover_providers()
    assert providers._discovered is True
    assert provider_imports == [(bundled_profile, "bundled")]

    monkeypatch.setattr(memory_plugins, "find_provider_dir", unexpected)
    assert memory_plugins.discover_memory_providers() == []
    assert memory_plugins.discover_plugin_cli_commands() == []
    assert memory_plugins.load_memory_provider("user-plugin") is None

    monkeypatch.setattr(context_plugins, "_load_engine_from_dir", unexpected)
    assert context_plugins.discover_context_engines() == []
    assert context_plugins.load_context_engine("user-plugin") is None
