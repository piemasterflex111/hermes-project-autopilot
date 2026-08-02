"""Containment tests for Project Autopilot mission Kanban workers."""
from __future__ import annotations

import json

import pytest


MISSION_SAFE_KANBAN_TOOLS = {
    "kanban_show",
    "kanban_complete",
    "kanban_block",
    "kanban_heartbeat",
    "kanban_comment",
}


@pytest.fixture
def mission_worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_mission_owned")
    monkeypatch.setenv(
        "HERMES_MISSION_POLICY",
        json.dumps({"mission_id": "m_test", "task_id": "t_mission_owned"}),
    )


def _error(result: str) -> str:
    return str(json.loads(result).get("error") or "")


def test_mission_worker_schema_exposes_only_safe_kanban_lifecycle_tools(
    monkeypatch,
):
    """Mission state is part of the schema cache key and narrows Kanban."""
    import tools.kanban_tools  # noqa: F401 -- ensure registrations exist
    from model_tools import _clear_tool_defs_cache, get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_mission_owned")
    monkeypatch.delenv("HERMES_MISSION_POLICY", raising=False)
    _clear_tool_defs_cache()
    invalidate_check_fn_cache()

    ordinary_worker = {
        item["function"]["name"]
        for item in get_tool_definitions(
            enabled_toolsets=["kanban"], quiet_mode=True
        )
    }
    assert "kanban_create" in ordinary_worker
    assert "kanban_attach_url" in ordinary_worker

    # Do not clear the schema cache here: this assertion also proves that
    # HERMES_MISSION_POLICY participates in the cache key.
    monkeypatch.setenv("HERMES_MISSION_POLICY", '{"mission_id":"m_test"}')
    mission_worker_tools = {
        item["function"]["name"]
        for item in get_tool_definitions(
            enabled_toolsets=["kanban"], quiet_mode=True
        )
    }
    assert mission_worker_tools == MISSION_SAFE_KANBAN_TOOLS


@pytest.mark.parametrize(
    ("handler_name", "args"),
    [
        ("_handle_list", {}),
        ("_handle_create", {"title": "escape", "assignee": "techlead"}),
        ("_handle_link", {"parent_id": "a", "child_id": "b"}),
        ("_handle_attach", {"filename": "x", "content_base64": "eA=="}),
        ("_handle_attach_url", {"url": "https://example.com/x"}),
        ("_handle_unblock", {"task_id": "t_mission_owned"}),
        ("_handle_attachments", {}),
    ],
)
def test_mission_worker_runtime_rejects_unsafe_kanban_handlers(
    mission_worker,
    handler_name,
    args,
):
    """Direct/stale-schema dispatch cannot bypass schema containment."""
    from tools import kanban_tools as kt

    result = getattr(kt, handler_name)(args)
    assert "mission workers" in _error(result).lower()


@pytest.mark.parametrize(
    ("handler_name", "args"),
    [
        ("_handle_show", {"task_id": "t_foreign"}),
        ("_handle_comment", {"task_id": "t_foreign", "body": "poison"}),
    ],
)
def test_mission_worker_read_and_comment_are_exact_task_scoped(
    mission_worker,
    handler_name,
    args,
):
    from tools import kanban_tools as kt

    result = getattr(kt, handler_name)(args)
    error = _error(result)
    assert "scoped to task t_mission_owned" in error
    assert "t_foreign" in error


@pytest.mark.parametrize(
    ("handler_name", "args"),
    [
        ("_handle_show", {"board": "other"}),
        ("_handle_complete", {"summary": "done", "board": "other"}),
        ("_handle_block", {"reason": "blocked", "board": "other"}),
        ("_handle_heartbeat", {"board": "other"}),
        (
            "_handle_comment",
            {"task_id": "t_mission_owned", "body": "note", "board": "other"},
        ),
    ],
)
def test_mission_worker_cannot_override_pinned_board(
    mission_worker,
    handler_name,
    args,
):
    from tools import kanban_tools as kt

    assert "cannot override" in _error(getattr(kt, handler_name)(args))


@pytest.mark.parametrize(
    "handoff",
    [
        {"created_cards": []},
        {"created_cards": ["t_other"]},
        {"artifacts": []},
        {"artifacts": ["/etc/passwd"]},
    ],
)
def test_mission_completion_rejects_controller_owned_handoff_fields(
    mission_worker,
    handoff,
):
    from tools import kanban_tools as kt

    error = _error(kt._handle_complete({"summary": "done", **handoff}))
    assert "created_cards or host-side artifacts" in error
