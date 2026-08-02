"""Durable, identity-bound gateway controls for Project Autopilot missions.

Interactive platform buttons are treated as authorization capabilities, not UI
state.  The opaque capability is stored only as a SHA-256 hash in the board
SQLite database, expires quickly, can be claimed exactly once, and is bound to
the originating platform/chat/thread/operator identity.  The board slug is
encoded in the public token solely to locate the correct per-board database;
the random secret remains the authorization material.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import missions_db as mdb

TOKEN_PREFIX = "ma1"
TOKEN_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
ACTION_TOKEN_TTL_SECONDS = 15 * 60
PROMPT_CLAIM_STALE_SECONDS = 90
SUPPORTED_ACTIONS = frozenset(
    {"approve", "deny", "retry", "reconcile", "resume", "cancel", "commit"}
)
ACTIONABLE_STATUSES = frozenset(
    {"awaiting_approval", "blocked", "waiting_for_user", "committing"}
)


class MissionGatewayError(RuntimeError):
    """A gateway action failed authorization, expiry, or mission policy."""


@dataclass(frozen=True)
class GatewayIdentity:
    platform: str
    chat_id: str
    chat_type: str
    principal_id: str
    thread_id: str = ""
    scope_id: str = ""

    @classmethod
    def from_source(cls, source: Any) -> "GatewayIdentity":
        if source is None:
            raise MissionGatewayError("mission gateway action requires a message source")
        raw_platform = getattr(source, "platform", "")
        platform = str(getattr(raw_platform, "value", raw_platform) or "").strip().lower()
        chat_id = str(getattr(source, "chat_id", "") or "").strip()
        chat_type = str(getattr(source, "chat_type", "") or "dm").strip().lower()
        thread_id = str(getattr(source, "thread_id", "") or "").strip()
        scope_id = str(
            getattr(source, "scope_id", None)
            or getattr(source, "guild_id", None)
            or ""
        ).strip()
        principal = str(
            getattr(source, "user_id_alt", None)
            or getattr(source, "user_id", None)
            or (chat_id if chat_type in {"dm", "c2c", "private"} else "")
        ).strip()
        return cls(
            platform=platform,
            chat_id=chat_id,
            chat_type=chat_type,
            principal_id=principal,
            thread_id=thread_id,
            scope_id=scope_id,
        ).validated()

    def validated(self) -> "GatewayIdentity":
        if not self.platform or not self.chat_id or not self.chat_type:
            raise MissionGatewayError("platform, chat, and chat type are required")
        if not self.principal_id:
            raise MissionGatewayError(
                "mission buttons require a stable operator identity; group-wide anonymous authorization is refused"
            )
        return self

    def matches_row(self, row: Mapping[str, Any]) -> bool:
        return all(
            str(row.get(key) or "") == value
            for key, value in (
                ("platform", self.platform),
                ("chat_id", self.chat_id),
                ("chat_type", self.chat_type),
                ("thread_id", self.thread_id),
                ("principal_id", self.principal_id),
                ("scope_id", self.scope_id),
            )
        )


@dataclass(frozen=True)
class MissionActionToken:
    action: str
    label: str
    style: str
    token: str


@dataclass(frozen=True)
class PromptCandidate:
    board: str
    mission: mdb.Mission
    identity: GatewayIdentity
    notifier_profile: str
    delivery_metadata: dict[str, Any]
    claim_owner: str


def _claim_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


def _metadata_json(metadata: Optional[Mapping[str, Any]]) -> str:
    safe: dict[str, Any] = {}
    for key, value in dict(metadata or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)


def _metadata_value(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _board_segment(board: str) -> str:
    normalized = kb._normalize_board_slug(board)
    if normalized is None:
        raise MissionGatewayError("board slug is required")
    return base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_token(token: str) -> tuple[str, str]:
    parts = str(token or "").split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not TOKEN_SECRET_RE.fullmatch(parts[2]):
        raise MissionGatewayError("invalid or malformed mission action token")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        board = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise MissionGatewayError("invalid mission action board token") from exc
    try:
        normalized = kb._normalize_board_slug(board)
    except ValueError as exc:
        raise MissionGatewayError("invalid mission action board") from exc
    if normalized is None or normalized != board:
        raise MissionGatewayError("invalid mission action board")
    return normalized, parts[2]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def action_options_for_mission(mission: mdb.Mission) -> list[tuple[str, str, str]]:
    if mission.status == "awaiting_approval":
        return [
            ("approve", "Approve commit gate", "primary"),
            ("deny", "Deny commit", "danger"),
        ]
    if mission.status == "blocked":
        return [
            ("retry", "Retry safely", "primary"),
            ("reconcile", "Reconcile state", "secondary"),
        ]
    if mission.status == "waiting_for_user":
        return [
            ("resume", "Resume mission", "primary"),
            ("cancel", "Cancel mission", "danger"),
        ]
    if mission.status == "committing":
        return [
            ("commit", "Commit locally", "primary"),
            ("cancel", "Cancel mission", "danger"),
        ]
    return []


def add_subscription(
    conn,
    mission_id: str,
    identity: GatewayIdentity,
    *,
    notifier_profile: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
    reset_prompt: bool = False,
) -> None:
    identity = identity.validated()
    mdb.ensure_schema(conn)
    if mdb.get_mission(conn, mission_id) is None:
        raise KeyError(mission_id)
    now = int(time.time())
    metadata_json = _metadata_json(delivery_metadata)
    with kb.write_txn(conn):
        conn.execute(
            """
            INSERT INTO mission_gateway_subscriptions(
                mission_id, platform, chat_id, chat_type, thread_id,
                principal_id, scope_id, notifier_profile,
                delivery_metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mission_id, platform, chat_id, thread_id, principal_id)
            DO UPDATE SET
                chat_type=excluded.chat_type,
                scope_id=excluded.scope_id,
                notifier_profile=excluded.notifier_profile,
                delivery_metadata_json=excluded.delivery_metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                mission_id,
                identity.platform,
                identity.chat_id,
                identity.chat_type,
                identity.thread_id,
                identity.principal_id,
                identity.scope_id,
                str(notifier_profile or ""),
                metadata_json,
                now,
                now,
            ),
        )
        if reset_prompt:
            conn.execute(
                """
                UPDATE mission_gateway_subscriptions
                   SET prompt_claim_owner=NULL, prompt_claimed_at=NULL,
                       last_prompted_status=NULL, last_prompted_updated_at=NULL
                 WHERE mission_id=? AND platform=? AND chat_id=?
                   AND thread_id=? AND principal_id=?
                """,
                (
                    mission_id,
                    identity.platform,
                    identity.chat_id,
                    identity.thread_id,
                    identity.principal_id,
                ),
            )


def remove_subscription(conn, mission_id: str, identity: GatewayIdentity) -> bool:
    identity = identity.validated()
    mdb.ensure_schema(conn)
    with kb.write_txn(conn):
        cur = conn.execute(
            """DELETE FROM mission_gateway_subscriptions
                 WHERE mission_id=? AND platform=? AND chat_id=?
                   AND thread_id=? AND principal_id=?""",
            (
                mission_id,
                identity.platform,
                identity.chat_id,
                identity.thread_id,
                identity.principal_id,
            ),
        )
    return cur.rowcount == 1


def list_subscriptions(conn, mission_id: Optional[str] = None) -> list[dict[str, Any]]:
    mdb.ensure_schema(conn)
    if mission_id:
        rows = conn.execute(
            "SELECT * FROM mission_gateway_subscriptions WHERE mission_id=?",
            (mission_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM mission_gateway_subscriptions").fetchall()
    return [
        dict(row) | {"delivery_metadata": _metadata_value(row["delivery_metadata_json"])}
        for row in rows
    ]


def claim_prompt_candidates(conn, board: str, *, limit: int = 50) -> list[PromptCandidate]:
    """Claim actionable, not-yet-delivered mission prompts with stale recovery."""
    mdb.ensure_schema(conn)
    board = kb._normalize_board_slug(board) or kb.DEFAULT_BOARD
    now = int(time.time())
    stale_before = now - PROMPT_CLAIM_STALE_SECONDS
    owner = _claim_owner()
    candidates: list[PromptCandidate] = []
    rows = conn.execute(
        """
        SELECT s.*
          FROM mission_gateway_subscriptions s
          JOIN missions m ON m.id=s.mission_id
         WHERE m.status IN ('awaiting_approval','blocked','waiting_for_user','committing')
           AND (
                s.last_prompted_status IS NULL
                OR s.last_prompted_status != m.status
                OR COALESCE(s.last_prompted_updated_at, -1) != m.updated_at
           )
           AND (s.prompt_claimed_at IS NULL OR s.prompt_claimed_at < ?)
         ORDER BY m.updated_at, s.created_at
         LIMIT ?
        """,
        (stale_before, max(1, min(int(limit), 500))),
    ).fetchall()
    for row in rows:
        mission = mdb.get_mission(conn, row["mission_id"])
        if mission is None or not action_options_for_mission(mission):
            continue
        claim = f"{owner}:{len(candidates)}"
        with kb.write_txn(conn):
            cur = conn.execute(
                """
                UPDATE mission_gateway_subscriptions
                   SET prompt_claim_owner=?, prompt_claimed_at=?
                 WHERE mission_id=? AND platform=? AND chat_id=?
                   AND thread_id=? AND principal_id=?
                   AND (prompt_claimed_at IS NULL OR prompt_claimed_at < ?)
                   AND (
                        last_prompted_status IS NULL
                        OR last_prompted_status != ?
                        OR COALESCE(last_prompted_updated_at, -1) != ?
                   )
                   AND EXISTS (
                        SELECT 1 FROM missions m
                         WHERE m.id=mission_gateway_subscriptions.mission_id
                           AND m.status=? AND m.updated_at=?
                   )
                """,
                (
                    claim,
                    now,
                    row["mission_id"],
                    row["platform"],
                    row["chat_id"],
                    row["thread_id"],
                    row["principal_id"],
                    stale_before,
                    mission.status,
                    mission.updated_at,
                    mission.status,
                    mission.updated_at,
                ),
            )
        if cur.rowcount != 1:
            continue
        candidates.append(
            PromptCandidate(
                board=board,
                mission=mission,
                identity=GatewayIdentity(
                    platform=row["platform"],
                    chat_id=row["chat_id"],
                    chat_type=row["chat_type"],
                    thread_id=row["thread_id"],
                    principal_id=row["principal_id"],
                    scope_id=row["scope_id"],
                ),
                notifier_profile=str(row["notifier_profile"] or ""),
                delivery_metadata=_metadata_value(row["delivery_metadata_json"]),
                claim_owner=claim,
            )
        )
    return candidates


def complete_prompt(conn, candidate: PromptCandidate) -> bool:
    now = int(time.time())
    with kb.write_txn(conn):
        cur = conn.execute(
            """
            UPDATE mission_gateway_subscriptions
               SET prompt_claim_owner=NULL, prompt_claimed_at=NULL,
                   last_prompted_status=?, last_prompted_updated_at=?,
                   last_prompted_at=?, updated_at=?
             WHERE mission_id=? AND platform=? AND chat_id=?
               AND thread_id=? AND principal_id=? AND prompt_claim_owner=?
            """,
            (
                candidate.mission.status,
                candidate.mission.updated_at,
                now,
                now,
                candidate.mission.id,
                candidate.identity.platform,
                candidate.identity.chat_id,
                candidate.identity.thread_id,
                candidate.identity.principal_id,
                candidate.claim_owner,
            ),
        )
    return cur.rowcount == 1


def release_prompt(conn, candidate: PromptCandidate) -> bool:
    with kb.write_txn(conn):
        cur = conn.execute(
            """
            UPDATE mission_gateway_subscriptions
               SET prompt_claim_owner=NULL, prompt_claimed_at=NULL
             WHERE mission_id=? AND platform=? AND chat_id=?
               AND thread_id=? AND principal_id=? AND prompt_claim_owner=?
            """,
            (
                candidate.mission.id,
                candidate.identity.platform,
                candidate.identity.chat_id,
                candidate.identity.thread_id,
                candidate.identity.principal_id,
                candidate.claim_owner,
            ),
        )
    return cur.rowcount == 1


def issue_action_tokens(
    conn,
    board: str,
    mission: mdb.Mission,
    identity: GatewayIdentity,
    *,
    ttl_seconds: int = ACTION_TOKEN_TTL_SECONDS,
) -> list[MissionActionToken]:
    identity = identity.validated()
    options = action_options_for_mission(mission)
    if not options:
        return []
    board_segment = _board_segment(board)
    now = int(time.time())
    expires_at = now + max(30, min(int(ttl_seconds), 24 * 60 * 60))
    created: list[MissionActionToken] = []
    with kb.write_txn(conn):
        conn.execute(
            """
            UPDATE mission_gateway_actions
               SET status='superseded', completed_at=?
             WHERE mission_id=? AND platform=? AND chat_id=?
               AND thread_id=? AND principal_id=? AND status='pending'
            """,
            (
                now,
                mission.id,
                identity.platform,
                identity.chat_id,
                identity.thread_id,
                identity.principal_id,
            ),
        )
        for action, label, style in options:
            if action not in SUPPORTED_ACTIONS:
                raise MissionGatewayError(f"unsupported mission gateway action: {action}")
            secret = secrets.token_urlsafe(18)
            token = f"{TOKEN_PREFIX}.{board_segment}.{secret}"
            conn.execute(
                """
                INSERT INTO mission_gateway_actions(
                    token_hash, mission_id, action, platform, chat_id, chat_type,
                    thread_id, principal_id, scope_id, status, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    _hash_token(token),
                    mission.id,
                    action,
                    identity.platform,
                    identity.chat_id,
                    identity.chat_type,
                    identity.thread_id,
                    identity.principal_id,
                    identity.scope_id,
                    expires_at,
                    now,
                ),
            )
            created.append(MissionActionToken(action=action, label=label, style=style, token=token))
    return created


def revoke_action_tokens(conn, tokens: list[MissionActionToken], *, reason: str = "delivery_failed") -> int:
    if not tokens:
        return 0
    hashes = [_hash_token(token.token) for token in tokens]
    now = int(time.time())
    with kb.write_txn(conn):
        placeholders = ",".join("?" for _ in hashes)
        cur = conn.execute(
            f"""UPDATE mission_gateway_actions
                   SET status='revoked', completed_at=?, result_json=?
                 WHERE token_hash IN ({placeholders})
                   AND status='pending'""",
            (now, json.dumps({"reason": reason}, sort_keys=True), *hashes),
        )
    return int(cur.rowcount)


def _run_action(conn, mission_id: str, action: str):
    from hermes_cli import mission_service as svc

    mapping = {
        "approve": svc.approve_commit,
        "deny": svc.deny_mission,
        "retry": svc.retry_mission,
        "reconcile": svc.reconcile_mission,
        "resume": svc.resume_mission,
        "cancel": svc.cancel_mission,
        "commit": svc.commit_mission,
    }
    handler = mapping.get(action)
    if handler is None:
        raise MissionGatewayError(f"unsupported mission gateway action: {action}")
    return handler(conn, mission_id)


def resolve_action_token(token: str, identity: GatewayIdentity) -> dict[str, Any]:
    """Authorize and execute one durable mission action capability atomically."""
    board, _secret = _decode_token(token)
    identity = identity.validated()
    token_hash = _hash_token(token)
    now = int(time.time())
    with kb.connect_closing(board=board) as conn:
        mdb.ensure_schema(conn)
        expired = False
        with kb.write_txn(conn):
            row = conn.execute(
                "SELECT * FROM mission_gateway_actions WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                raise MissionGatewayError("mission action token is unknown")
            item = dict(row)
            if item["status"] != "pending":
                raise MissionGatewayError("mission action token was already used or revoked")
            if int(item["expires_at"]) < now:
                conn.execute(
                    "UPDATE mission_gateway_actions SET status='expired', completed_at=? WHERE token_hash=?",
                    (now, token_hash),
                )
                expired = True
            elif not identity.matches_row(item):
                raise MissionGatewayError("mission action token does not belong to this operator or conversation")
            else:
                cur = conn.execute(
                    """UPDATE mission_gateway_actions
                           SET status='claimed', claimed_at=?
                         WHERE token_hash=? AND status='pending'""",
                    (now, token_hash),
                )
                if cur.rowcount != 1:
                    raise MissionGatewayError("mission action token was claimed concurrently")
        if expired:
            raise MissionGatewayError("mission action token expired")
        try:
            mission = _run_action(conn, item["mission_id"], item["action"])
        except Exception as exc:
            finished = int(time.time())
            with kb.write_txn(conn):
                conn.execute(
                    """UPDATE mission_gateway_actions
                           SET status='failed', completed_at=?, result_json=?
                         WHERE token_hash=? AND status='claimed'""",
                    (
                        finished,
                        json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True),
                        token_hash,
                    ),
                )
            try:
                mdb.record_evidence(
                    conn,
                    item["mission_id"],
                    kind="gateway_authorization",
                    status="failed",
                    metadata={
                        "action": item["action"],
                        "platform": identity.platform,
                        "chat_id": identity.chat_id,
                        "principal_id": identity.principal_id,
                        "token_hash_prefix": token_hash[:12],
                        "error": str(exc),
                    },
                )
            except Exception:
                pass
            raise MissionGatewayError(str(exc)) from exc
        finished = int(time.time())
        result = {
            "board": board,
            "mission_id": mission.id,
            "action": item["action"],
            "status": mission.status,
            "phase": mission.phase,
        }
        with kb.write_txn(conn):
            conn.execute(
                """UPDATE mission_gateway_actions
                       SET status='succeeded', completed_at=?, result_json=?
                     WHERE token_hash=? AND status='claimed'""",
                (finished, json.dumps(result, sort_keys=True), token_hash),
            )
        mdb.record_evidence(
            conn,
            mission.id,
            kind="gateway_authorization",
            status="passed",
            metadata={
                "action": item["action"],
                "platform": identity.platform,
                "chat_id": identity.chat_id,
                "principal_id": identity.principal_id,
                "token_hash_prefix": token_hash[:12],
                "result_status": mission.status,
            },
            cwd=mission.worktree_path,
        )
        return result
