from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.session import SessionSource
from hermes_cli.mission_gateway import MissionActionToken
from tests.gateway.relay.stub_connector import StubConnector


def _mission(status: str = "awaiting_approval"):
    return SimpleNamespace(
        id="m_123456789abc",
        status=status,
        autonomy_level=3,
        objective="Approve the verified local commit",
        blocked_reason=None,
        updated_at=100,
    )


def _actions():
    return [
        MissionActionToken("approve", "Approve commit gate", "primary", "ma1.ZGVmYXVsdA.secretsecretsecretsecret"),
        MissionActionToken("deny", "Deny commit", "danger", "ma1.ZGVmYXVsdA.othersecretsecretsecret"),
    ]


def _relay():
    descriptor = CapabilityDescriptor(
        contract_version=CONTRACT_VERSION,
        platform="telegram",
        label="Telegram",
        max_message_length=4096,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="markdown_v2",
        len_unit="utf16",
        supported_ops=("send", "prompt"),
    )
    stub = StubConnector(descriptor)
    return RelayAdapter(PlatformConfig(), descriptor, transport=stub), stub


@pytest.mark.asyncio
async def test_relay_mission_actions_use_durable_tokens_without_memory_registry():
    adapter, stub = _relay()
    result = await adapter.send_mission_actions("chat-1", _mission(), _actions())

    assert result.success
    prompt = stub.sent[-1]
    assert prompt["op"] == "prompt"
    assert prompt["prompt_kind"] == "approval"
    assert [option["id"] for option in prompt["options"]] == [item.token for item in _actions()]
    assert prompt["prompt_id"].startswith("mission-")
    assert prompt["prompt_id"] not in adapter._pending_prompts


@pytest.mark.asyncio
async def test_relay_mission_callback_consumes_durable_token(monkeypatch):
    adapter, stub = _relay()
    calls = []

    def resolve(token, identity):
        calls.append((token, identity))
        return {
            "mission_id": "m_123456789abc",
            "action": "approve",
            "status": "committing",
        }

    monkeypatch.setattr("hermes_cli.mission_gateway.resolve_action_token", resolve)
    event = MessageEvent(
        text="Approve",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type="dm",
            user_id="user-1",
        ),
        prompt_response={"prompt_id": "mission-abc", "option_id": _actions()[0].token},
    )

    assert await adapter._consume_prompt_response(event) is True
    assert calls[0][0] == _actions()[0].token
    assert calls[0][1].principal_id == "user-1"
    assert stub.sent[-1]["op"] == "send"
    assert "committing" in stub.sent[-1]["content"]


def test_qq_mission_keyboard_uses_opaque_token_payloads():
    from gateway.platforms.qqbot.keyboards import (
        build_mission_action_keyboard,
        parse_mission_action_button_data,
    )

    keyboard = build_mission_action_keyboard(_actions()).to_dict()
    buttons = keyboard["content"]["rows"][0]["buttons"]
    assert len(buttons) == 2
    payload = buttons[0]["action"]["data"]
    assert parse_mission_action_button_data(payload) == _actions()[0].token
    assert parse_mission_action_button_data("approve:session:allow-once") is None


@pytest.mark.asyncio
async def test_gateway_mission_action_command_resolves_current_operator(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._active_profile_name = lambda: "default"
    runner._thread_metadata_for_source = lambda *_args: {}
    runner._reply_anchor_for_event = lambda _event: None
    resolved = []

    def resolve(token, identity):
        resolved.append((token, identity))
        return {
            "mission_id": "m_123456789abc",
            "action": "deny",
            "status": "blocked",
        }

    monkeypatch.setattr("hermes_cli.mission_gateway.resolve_action_token", resolve)
    event = MessageEvent(
        text="/mission action ma1.ZGVmYXVsdA.secretsecretsecretsecret",
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="channel-1",
            chat_type="group",
            thread_id="thread-1",
            user_id="operator-1",
            scope_id="workspace-1",
        ),
    )

    output = await runner._handle_mission_command(event)
    assert "deny accepted" in output
    assert resolved[0][1].chat_id == "channel-1"
    assert resolved[0][1].thread_id == "thread-1"
    assert resolved[0][1].principal_id == "operator-1"
