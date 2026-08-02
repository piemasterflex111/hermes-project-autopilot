from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import mission_gateway as mg
from hermes_cli import mission_service as service
from hermes_cli import missions_db as mdb


@pytest.fixture
def gateway_mission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(hermes_home / "kanban"))
    repo = tmp_path / "project"
    repo.mkdir()
    contract = mdb.MissionContract(
        outcome="Ship a verified change",
        verification=["true"],
        constraints=["stay in scope"],
        boundaries={
            "allowed_roots": [str(repo)],
            "allowed_paths": ["app.py"],
            "network_destinations": [],
        },
        stop_when=["scope cannot be enforced"],
        allow_local_commit=False,
    )
    with kb.connect_closing(board="default") as conn:
        mission_id = mdb.create_mission(
            conn,
            objective="Gateway approval test",
            contract=contract,
            autonomy_level=3,
            repo_path=str(repo),
            board="default",
        )
        for status, phase in (
            ("planning", "planning"),
            ("ready", "planning"),
            ("running", "execution"),
            ("verifying", "verification"),
            ("awaiting_approval", "approval"),
        ):
            mdb.transition_mission(conn, mission_id, status, phase=phase)
        yield conn, mission_id


def _identity(
    *,
    platform: str = "telegram",
    chat_id: str = "chat-1",
    principal_id: str = "user-1",
    thread_id: str = "thread-1",
) -> mg.GatewayIdentity:
    return mg.GatewayIdentity(
        platform=platform,
        chat_id=chat_id,
        chat_type="group",
        thread_id=thread_id,
        principal_id=principal_id,
        scope_id="workspace-1",
    )


def test_gateway_identity_refuses_anonymous_group_operator():
    with pytest.raises(mg.MissionGatewayError, match="stable operator identity"):
        mg.GatewayIdentity(
            platform="slack",
            chat_id="channel-1",
            chat_type="group",
            principal_id="",
        ).validated()


def test_prompt_claim_is_single_owner_and_redelivers_after_status_change(gateway_mission):
    conn, mission_id = gateway_mission
    identity = _identity()
    mg.add_subscription(
        conn,
        mission_id,
        identity,
        notifier_profile="default",
        delivery_metadata={"reply_to_message_id": "message-1"},
    )

    claimed = mg.claim_prompt_candidates(conn, "default")
    assert len(claimed) == 1
    assert claimed[0].mission.id == mission_id
    assert claimed[0].delivery_metadata["reply_to_message_id"] == "message-1"
    assert mg.claim_prompt_candidates(conn, "default") == []

    assert mg.release_prompt(conn, claimed[0])
    reclaimed = mg.claim_prompt_candidates(conn, "default")
    assert len(reclaimed) == 1
    assert mg.complete_prompt(conn, reclaimed[0])
    assert mg.claim_prompt_candidates(conn, "default") == []

    service.deny_mission(conn, mission_id)
    changed = mg.claim_prompt_candidates(conn, "default")
    assert len(changed) == 1
    assert changed[0].mission.status == "blocked"


def test_authorized_action_is_one_use_and_records_evidence(gateway_mission):
    conn, mission_id = gateway_mission
    identity = _identity()
    mission = mdb.get_mission(conn, mission_id)
    assert mission is not None
    tokens = mg.issue_action_tokens(conn, "default", mission, identity)
    approve = next(item for item in tokens if item.action == "approve")

    result = mg.resolve_action_token(approve.token, identity)
    assert result == {
        "board": "default",
        "mission_id": mission_id,
        "action": "approve",
        "status": "committing",
        "phase": "commit",
    }
    assert mdb.get_mission(conn, mission_id).status == "committing"
    assert any(
        row["kind"] == "gateway_authorization"
        and row["status"] == "passed"
        and row["metadata"]["action"] == "approve"
        for row in mdb.list_evidence(conn, mission_id)
    )

    with pytest.raises(mg.MissionGatewayError, match="already used"):
        mg.resolve_action_token(approve.token, identity)


def test_cross_identity_rejection_does_not_consume_token(gateway_mission):
    conn, mission_id = gateway_mission
    identity = _identity()
    mission = mdb.get_mission(conn, mission_id)
    assert mission is not None
    approve = next(
        item
        for item in mg.issue_action_tokens(conn, "default", mission, identity)
        if item.action == "approve"
    )

    with pytest.raises(mg.MissionGatewayError, match="does not belong"):
        mg.resolve_action_token(approve.token, _identity(principal_id="attacker"))
    with pytest.raises(mg.MissionGatewayError, match="does not belong"):
        mg.resolve_action_token(approve.token, _identity(chat_id="other-chat"))

    assert mg.resolve_action_token(approve.token, identity)["status"] == "committing"


def test_expired_and_malformed_tokens_fail_closed(gateway_mission):
    conn, mission_id = gateway_mission
    identity = _identity()
    mission = mdb.get_mission(conn, mission_id)
    assert mission is not None
    approve = next(
        item
        for item in mg.issue_action_tokens(conn, "default", mission, identity)
        if item.action == "approve"
    )
    conn.execute(
        "UPDATE mission_gateway_actions SET expires_at=? WHERE token_hash=?",
        (int(time.time()) - 1, mg._hash_token(approve.token)),
    )
    conn.commit()

    with pytest.raises(mg.MissionGatewayError, match="expired"):
        mg.resolve_action_token(approve.token, identity)
    row = conn.execute(
        "SELECT status FROM mission_gateway_actions WHERE token_hash=?",
        (mg._hash_token(approve.token),),
    ).fetchone()
    assert row["status"] == "expired"

    for token in ("", "ma1.bad", "ma1.Li4.secretsecretsecretsecret", "ma1.ZGVmYXVsdA.short"):
        with pytest.raises(mg.MissionGatewayError):
            mg.resolve_action_token(token, identity)


def test_failed_action_consumes_token_and_records_failure(gateway_mission):
    conn, mission_id = gateway_mission
    identity = _identity()
    mission = mdb.get_mission(conn, mission_id)
    assert mission is not None
    approve = next(
        item
        for item in mg.issue_action_tokens(conn, "default", mission, identity)
        if item.action == "approve"
    )
    service.deny_mission(conn, mission_id)

    with pytest.raises(mg.MissionGatewayError, match="not awaiting"):
        mg.resolve_action_token(approve.token, identity)
    row = conn.execute(
        "SELECT status FROM mission_gateway_actions WHERE token_hash=?",
        (mg._hash_token(approve.token),),
    ).fetchone()
    assert row["status"] == "failed"
    assert any(
        evidence["kind"] == "gateway_authorization"
        and evidence["status"] == "failed"
        for evidence in mdb.list_evidence(conn, mission_id)
    )


def test_revoke_pending_tokens_after_failed_delivery(gateway_mission):
    conn, mission_id = gateway_mission
    identity = _identity()
    mission = mdb.get_mission(conn, mission_id)
    assert mission is not None
    tokens = mg.issue_action_tokens(conn, "default", mission, identity)
    assert mg.revoke_action_tokens(conn, tokens) == len(tokens)
    statuses = {
        row["status"]
        for row in conn.execute("SELECT status FROM mission_gateway_actions").fetchall()
    }
    assert statuses == {"revoked"}


def test_watcher_issues_tokens_only_for_exact_claimed_mission_version(gateway_mission):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    conn, mission_id = gateway_mission
    identity = _identity()
    mg.add_subscription(conn, mission_id, identity)
    candidate = mg.claim_prompt_candidates(conn, "default")[0]

    fresh, tokens = GatewayKanbanWatchersMixin._mission_issue_tokens(candidate)
    assert fresh is not None
    assert fresh.id == mission_id
    assert {token.action for token in tokens} == {"approve", "deny"}
    assert mg.revoke_action_tokens(conn, tokens) == 2


def test_watcher_releases_claim_when_mission_changed_before_delivery(gateway_mission):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    conn, mission_id = gateway_mission
    identity = _identity()
    mg.add_subscription(conn, mission_id, identity)
    candidate = mg.claim_prompt_candidates(conn, "default")[0]
    service.deny_mission(conn, mission_id)

    fresh, tokens = GatewayKanbanWatchersMixin._mission_issue_tokens(candidate)
    assert fresh is None
    assert tokens == []
    subscription = mg.list_subscriptions(conn, mission_id)[0]
    assert subscription["prompt_claim_owner"] is None
    assert mg.claim_prompt_candidates(conn, "default")[0].mission.status == "blocked"
