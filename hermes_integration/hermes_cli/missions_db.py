"""Durable mission records layered on the existing Kanban database.

Missions own an objective and completion contract; Kanban continues to own
task scheduling, claims, retries, and workspaces.  Keeping both records in the
same per-board SQLite database makes mission/task creation and recovery
transactional without introducing a second dispatcher.
"""

from __future__ import annotations

import hashlib
import gzip
import json
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb


MISSION_STATUSES = frozenset(
    {
        "draft", "planning", "ready", "running", "waiting_for_user",
        "blocked", "verifying", "awaiting_approval", "committing",
        "succeeded", "failed", "cancelled", "rolled_back",
    }
)
TERMINAL_MISSION_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "rolled_back"}
)
MISSION_ROLES = frozenset({"controller", "planner", "executor", "verifier"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"planning", "cancelled"}),
    "planning": frozenset({"ready", "blocked", "failed", "cancelled"}),
    "ready": frozenset({"running", "blocked", "cancelled"}),
    "running": frozenset({"waiting_for_user", "blocked", "verifying", "failed", "cancelled"}),
    "waiting_for_user": frozenset({"running", "blocked", "cancelled"}),
    "blocked": frozenset({"planning", "ready", "running", "rolled_back", "failed", "cancelled"}),
    "verifying": frozenset({"running", "blocked", "awaiting_approval", "committing", "failed", "cancelled"}),
    "awaiting_approval": frozenset({"committing", "blocked", "cancelled"}),
    "committing": frozenset({"succeeded", "blocked", "failed", "cancelled"}),
    "succeeded": frozenset({"rolled_back"}),
    "failed": frozenset({"rolled_back"}),
    "cancelled": frozenset({"rolled_back"}),
    "rolled_back": frozenset(),
}


@dataclass(frozen=True)
class MissionContract:
    outcome: str
    verification: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    boundaries: dict[str, Any] = field(default_factory=dict)
    stop_when: list[str] = field(default_factory=list)
    allow_local_commit: bool = False

    def validate(self) -> None:
        if not self.outcome.strip():
            raise ValueError("mission contract outcome is required")
        if not self.verification:
            raise ValueError("at least one verification criterion is required")
        if not isinstance(self.allow_local_commit, bool):
            raise ValueError("allow_local_commit must be a boolean")
        roots = self.boundaries.get("allowed_roots", [])
        if not isinstance(roots, list) or not roots:
            raise ValueError("boundaries.allowed_roots must contain at least one path")
        for raw in roots:
            if not Path(str(raw)).expanduser().is_absolute():
                raise ValueError("mission allowed roots must be absolute paths")
        allowed_paths = self.boundaries.get("allowed_paths", [])
        if not isinstance(allowed_paths, list):
            raise ValueError("boundaries.allowed_paths must be a list")
        for raw in allowed_paths:
            raw_value = str(raw).strip()
            value = raw_value.strip("/")
            if not value or Path(raw_value).is_absolute() or ".." in Path(value).parts:
                raise ValueError("mission allowed paths must be safe relative paths")
        destinations = self.boundaries.get("network_destinations", [])
        if not isinstance(destinations, list) or any(
            not isinstance(item, str) or not item.strip() for item in destinations
        ):
            raise ValueError("boundaries.network_destinations must be a list of strings")
        backends = self.boundaries.get("allowed_terminal_backends", ["docker"])
        if not isinstance(backends, list) or any(
            not isinstance(item, str) or not item.strip() for item in backends
        ):
            raise ValueError("boundaries.allowed_terminal_backends must be a list of strings")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "MissionContract":
        data = json.loads(raw)
        return cls(
            outcome=str(data.get("outcome") or ""),
            verification=[str(v) for v in data.get("verification", [])],
            constraints=[str(v) for v in data.get("constraints", [])],
            boundaries=dict(data.get("boundaries") or {}),
            stop_when=[str(v) for v in data.get("stop_when", [])],
            allow_local_commit=bool(data.get("allow_local_commit", False)),
        )


@dataclass(frozen=True)
class Mission:
    id: str
    objective: str
    contract: MissionContract
    autonomy_level: int
    risk_level: str
    status: str
    phase: str
    board: str
    project_id: Optional[str]
    root_task_id: Optional[str]
    repo_path: str
    worktree_path: Optional[str]
    branch_name: Optional[str]
    base_commit: Optional[str]
    rollback_ref: Optional[str]
    verified_commit: Optional[str]
    budget: dict[str, Any]
    deadline: Optional[int]
    blocked_reason: Optional[str]
    final_disposition: Optional[str]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Mission":
        return cls(
            id=row["id"], objective=row["objective"],
            contract=MissionContract.from_json(row["contract_json"]),
            autonomy_level=int(row["autonomy_level"]), risk_level=row["risk_level"],
            status=row["status"], phase=row["phase"], board=row["board"],
            project_id=row["project_id"], root_task_id=row["root_task_id"],
            repo_path=row["repo_path"], worktree_path=row["worktree_path"],
            branch_name=row["branch_name"], base_commit=row["base_commit"],
            rollback_ref=row["rollback_ref"], verified_commit=row["verified_commit"],
            budget=json.loads(row["budget_json"] or "{}"), deadline=row["deadline"],
            blocked_reason=row["blocked_reason"], final_disposition=row["final_disposition"],
            created_at=int(row["created_at"]), updated_at=int(row["updated_at"]),
        )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    autonomy_level INTEGER NOT NULL,
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    board TEXT NOT NULL,
    project_id TEXT,
    root_task_id TEXT,
    repo_path TEXT NOT NULL,
    worktree_path TEXT,
    branch_name TEXT,
    base_commit TEXT,
    rollback_ref TEXT,
    verified_commit TEXT,
    budget_json TEXT NOT NULL DEFAULT '{}',
    deadline INTEGER,
    blocked_reason TEXT,
    final_disposition TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL,
    task_id TEXT,
    run_id INTEGER,
    tool_call_id TEXT,
    kind TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    cwd TEXT,
    command TEXT,
    exit_code INTEGER,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    blob_path TEXT,
    blob_sha256 TEXT,
    blob_bytes INTEGER NOT NULL DEFAULT 0,
    previous_hash TEXT,
    record_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL,
    task_id TEXT,
    run_id INTEGER,
    claim_token TEXT,
    action TEXT NOT NULL,
    checkpoint_ref TEXT,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    resolved_at INTEGER
);

CREATE TABLE IF NOT EXISTS mission_plans (
    mission_id TEXT PRIMARY KEY,
    plan_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    executor_assignee TEXT NOT NULL,
    verifier_assignee TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_task_manifests (
    mission_id TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_task_manifest_cards (
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    PRIMARY KEY (mission_id, task_id)
);

CREATE TABLE IF NOT EXISTS mission_gateway_subscriptions (
    mission_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    principal_id TEXT NOT NULL,
    scope_id TEXT NOT NULL DEFAULT '',
    notifier_profile TEXT NOT NULL DEFAULT '',
    delivery_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    prompt_claim_owner TEXT,
    prompt_claimed_at INTEGER,
    last_prompted_status TEXT,
    last_prompted_updated_at INTEGER,
    last_prompted_at INTEGER,
    PRIMARY KEY (mission_id, platform, chat_id, thread_id, principal_id)
);

CREATE TABLE IF NOT EXISTS mission_gateway_actions (
    token_hash TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    action TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    principal_id TEXT NOT NULL,
    scope_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    claimed_at INTEGER,
    completed_at INTEGER,
    result_json TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_mission_evidence_mission ON mission_evidence(mission_id, id);
CREATE INDEX IF NOT EXISTS idx_mission_intents_open ON mission_intents(mission_id, status);
CREATE INDEX IF NOT EXISTS idx_mission_gateway_subs_pending
    ON mission_gateway_subscriptions(last_prompted_status, last_prompted_updated_at);
CREATE INDEX IF NOT EXISTS idx_mission_gateway_actions_pending
    ON mission_gateway_actions(status, expires_at);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    kb._add_column_if_missing(conn, "tasks", "mission_id", "mission_id TEXT")
    kb._add_column_if_missing(conn, "tasks", "mission_role", "mission_role TEXT")
    kb._add_column_if_missing(conn, "mission_intents", "run_id", "run_id INTEGER")
    kb._add_column_if_missing(conn, "mission_intents", "claim_token", "claim_token TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_mission ON tasks(mission_id, status)")
    conn.commit()


def _new_id() -> str:
    return "m_" + secrets.token_hex(6)


def create_mission(
    conn: sqlite3.Connection,
    *,
    objective: str,
    contract: MissionContract,
    autonomy_level: int,
    repo_path: str,
    board: str,
    risk_level: str = "medium",
    project_id: Optional[str] = None,
    budget: Optional[dict[str, Any]] = None,
    deadline: Optional[int] = None,
) -> str:
    ensure_schema(conn)
    objective = objective.strip()
    if not objective:
        raise ValueError("mission objective is required")
    contract.validate()
    if autonomy_level not in range(0, 5):
        raise ValueError("autonomy_level must be an explicit value from 0 through 4")
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_absolute():
        raise ValueError("repo_path must be absolute")
    roots = [Path(str(raw)).expanduser().resolve() for raw in contract.boundaries["allowed_roots"]]
    if not any(repo == root or repo.is_relative_to(root) for root in roots):
        raise ValueError("repo_path must be inside an allowed root")
    now = int(time.time())
    mission_id = _new_id()
    with kb.write_txn(conn):
        conn.execute(
            """
            INSERT INTO missions(
                id, objective, contract_json, autonomy_level, risk_level,
                status, phase, board, project_id, repo_path, budget_json,
                deadline, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'draft', 'contract', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mission_id, objective, contract.to_json(), autonomy_level,
                risk_level, board, project_id, str(repo),
                json.dumps(budget or {}, sort_keys=True), deadline, now, now,
            ),
        )
    return mission_id


def get_mission(conn: sqlite3.Connection, mission_id: str) -> Optional[Mission]:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
    return Mission.from_row(row) if row else None


def list_missions(
    conn: sqlite3.Connection, *, status: Optional[str] = None, limit: int = 100
) -> list[Mission]:
    ensure_schema(conn)
    if status is not None and status not in MISSION_STATUSES:
        raise ValueError(f"invalid mission status: {status}")
    sql = "SELECT * FROM missions"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    return [Mission.from_row(row) for row in conn.execute(sql, params)]


def transition_mission(
    conn: sqlite3.Connection,
    mission_id: str,
    target: str,
    *,
    phase: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    final_disposition: Optional[str] = None,
) -> Mission:
    ensure_schema(conn)
    if target not in MISSION_STATUSES:
        raise ValueError(f"invalid mission status: {target}")
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
        if row is None:
            raise KeyError(mission_id)
        current = row["status"]
        if target not in ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"illegal mission transition: {current} -> {target}")
        conn.execute(
            """UPDATE missions
               SET status = ?, phase = COALESCE(?, phase), blocked_reason = ?,
                   final_disposition = COALESCE(?, final_disposition), updated_at = ?
             WHERE id = ? AND status = ?""",
            (target, phase, blocked_reason, final_disposition, int(time.time()), mission_id, current),
        )
    mission = get_mission(conn, mission_id)
    assert mission is not None
    return mission


def link_task(conn: sqlite3.Connection, mission_id: str, task_id: str, role: str) -> None:
    """Bind a task once, or accept an exact idempotent re-bind.

    Mission/task ownership is immutable after the first successful link.  A
    retried planner may repeat the same link, but it must never steal a task
    from another mission or silently change its role within this mission.
    """
    ensure_schema(conn)
    if role not in MISSION_ROLES:
        raise ValueError(f"invalid mission role: {role}")
    with kb.write_txn(conn):
        cur = conn.execute(
            """UPDATE tasks SET mission_id = ?, mission_role = ?
                 WHERE id = ?
                   AND (
                        (mission_id IS NULL AND mission_role IS NULL)
                        OR (mission_id = ? AND mission_role = ?)
                   )""",
            (mission_id, role, task_id, mission_id, role),
        )
        if cur.rowcount == 1:
            return
        existing = conn.execute(
            "SELECT mission_id, mission_role FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if existing is None:
            raise KeyError(task_id)
        raise ValueError(
            f"task {task_id} is already linked to mission "
            f"{existing['mission_id']!r} with role {existing['mission_role']!r}"
        )


def get_plan(conn: sqlite3.Connection, mission_id: str) -> Optional[dict[str, Any]]:
    """Return the mission's immutable canonical planner proposal, if present."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM mission_plans WHERE mission_id=?", (mission_id,),
    ).fetchone()
    return dict(row) if row else None


def store_plan(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    plan_hash: str,
    payload_json: str,
    executor_assignee: str,
    verifier_assignee: str,
) -> dict[str, Any]:
    """Persist a canonical plan once, before any task cards are materialized.

    An exact replay is accepted. A different proposal or routing decision can
    never replace the durable contract after the first insert.
    """
    ensure_schema(conn)
    values = (
        plan_hash, payload_json, executor_assignee, verifier_assignee,
    )
    with kb.write_txn(conn):
        if conn.execute(
            "SELECT 1 FROM missions WHERE id=?", (mission_id,),
        ).fetchone() is None:
            raise KeyError(mission_id)
        conn.execute(
            """INSERT OR IGNORE INTO mission_plans(
                   mission_id, plan_hash, payload_json, executor_assignee,
                   verifier_assignee, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (mission_id, *values, int(time.time())),
        )
        row = conn.execute(
            "SELECT * FROM mission_plans WHERE mission_id=?", (mission_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("failed to persist mission plan")
        existing = (
            row["plan_hash"], row["payload_json"], row["executor_assignee"],
            row["verifier_assignee"],
        )
        if existing != values:
            raise ValueError("mission already has a different canonical plan")
        return dict(row)


def get_task_manifest(
    conn: sqlite3.Connection, mission_id: str,
) -> Optional[dict[str, Any]]:
    """Return the immutable materialized-card manifest, if accepted."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM mission_task_manifests WHERE mission_id=?",
        (mission_id,),
    ).fetchone()
    return dict(row) if row else None


def store_task_manifest(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    manifest_hash: str,
    payload_json: str,
) -> dict[str, Any]:
    """Persist the exact accepted task cards and dependency graph once.

    Exact retries are idempotent.  A later caller cannot redefine card IDs,
    execution specs, routing, workspaces, or edges after the mission has
    crossed its planning gate.
    """
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != manifest_hash:
        raise ValueError("mission task manifest hash does not match its payload")
    try:
        payload = json.loads(payload_json)
        cards = payload["cards"]
        bindings = [
            (mission_id, str(card["id"]), str(card["mission_role"]))
            for card in cards
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("mission task manifest has invalid card bindings") from exc
    if (
        not bindings
        or any(role not in MISSION_ROLES for _, _, role in bindings)
        or len({task_id for _, task_id, _ in bindings}) != len(bindings)
    ):
        raise ValueError("mission task manifest has invalid card bindings")
    ensure_schema(conn)
    with kb.write_txn(conn):
        if conn.execute(
            "SELECT 1 FROM missions WHERE id=?", (mission_id,),
        ).fetchone() is None:
            raise KeyError(mission_id)
        conn.execute(
            """INSERT OR IGNORE INTO mission_task_manifests(
                   mission_id, manifest_hash, payload_json, created_at
               ) VALUES (?, ?, ?, ?)""",
            (mission_id, manifest_hash, payload_json, int(time.time())),
        )
        row = conn.execute(
            "SELECT * FROM mission_task_manifests WHERE mission_id=?",
            (mission_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("failed to persist mission task manifest")
        if (
            row["manifest_hash"] != manifest_hash
            or row["payload_json"] != payload_json
        ):
            raise ValueError("mission already has a different task manifest")
        conn.executemany(
            """INSERT OR IGNORE INTO mission_task_manifest_cards(
                   mission_id, task_id, role
               ) VALUES (?, ?, ?)""",
            bindings,
        )
        stored = conn.execute(
            """SELECT task_id,role FROM mission_task_manifest_cards
                 WHERE mission_id=? ORDER BY task_id""",
            (mission_id,),
        ).fetchall()
        if [(r["task_id"], r["role"]) for r in stored] != sorted(
            (task_id, role) for _, task_id, role in bindings
        ):
            raise ValueError("mission task manifest card bindings disagree")
        return dict(row)


def set_workspace(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    worktree_path: str,
    branch_name: str,
    base_commit: str,
    rollback_ref: str,
) -> None:
    ensure_schema(conn)
    with kb.write_txn(conn):
        conn.execute(
            """UPDATE missions SET worktree_path=?, branch_name=?, base_commit=?,
               rollback_ref=?, updated_at=? WHERE id=?""",
            (worktree_path, branch_name, base_commit, rollback_ref, int(time.time()), mission_id),
        )


def begin_preparation(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    worktree_path: str,
    branch_name: str,
    base_commit: str,
    rollback_ref: str,
) -> None:
    """Atomically freeze the preparation manifest before external Git writes."""
    ensure_schema(conn)
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT status,phase FROM missions WHERE id=?", (mission_id,),
        ).fetchone()
        if row is None:
            raise KeyError(mission_id)
        recoverable = row["status"] in {"draft", "planning"} or (
            row["status"] == "blocked" and row["phase"] == "environment"
        )
        if not recoverable:
            raise ValueError(f"mission cannot prepare from {row['status']}")
        conn.execute(
            """UPDATE missions
                  SET status='planning', phase='environment', blocked_reason=NULL,
                      worktree_path=?, branch_name=?, base_commit=?, rollback_ref=?,
                      updated_at=?
                WHERE id=?""",
            (
                worktree_path, branch_name, base_commit, rollback_ref,
                int(time.time()), mission_id,
            ),
        )


def set_root_task(conn: sqlite3.Connection, mission_id: str, task_id: str) -> None:
    ensure_schema(conn)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE missions SET root_task_id=?, updated_at=? WHERE id=?",
            (task_id, int(time.time()), mission_id),
        )


def record_evidence(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    kind: str,
    status: str,
    metadata: Optional[dict[str, Any]] = None,
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
    tool_call_id: Optional[str] = None,
    cwd: Optional[str] = None,
    command: Optional[str] = None,
    exit_code: Optional[int] = None,
    started_at: Optional[int] = None,
    finished_at: Optional[int] = None,
    blob_path: Optional[str] = None,
    blob_sha256: Optional[str] = None,
    blob_bytes: int = 0,
) -> int:
    """Append one hash-chained evidence row; existing rows are never updated."""
    ensure_schema(conn)
    created_at = int(time.time())
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    with kb.write_txn(conn):
        prev = conn.execute(
            "SELECT record_hash FROM mission_evidence WHERE mission_id=? ORDER BY id DESC LIMIT 1",
            (mission_id,),
        ).fetchone()
        previous_hash = prev[0] if prev else None
        canonical = json.dumps(
            {
                "mission_id": mission_id, "task_id": task_id, "run_id": run_id,
                "tool_call_id": tool_call_id, "kind": kind, "created_at": created_at,
                "started_at": started_at, "finished_at": finished_at, "cwd": cwd,
                "command": command, "exit_code": exit_code, "status": status,
                "metadata_json": metadata_json, "blob_path": blob_path,
                "blob_sha256": blob_sha256, "blob_bytes": int(blob_bytes),
                "previous_hash": previous_hash,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        record_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        cur = conn.execute(
            """INSERT INTO mission_evidence(
                mission_id, task_id, run_id, tool_call_id, kind, created_at,
                started_at, finished_at, cwd, command, exit_code, status,
                metadata_json, blob_path, blob_sha256, blob_bytes,
                previous_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mission_id, task_id, run_id, tool_call_id, kind, created_at,
                started_at, finished_at, cwd, command, exit_code, status,
                metadata_json, blob_path, blob_sha256, int(blob_bytes),
                previous_hash, record_hash,
            ),
        )
        return int(cur.lastrowid)


def list_evidence(conn: sqlite3.Connection, mission_id: str) -> list[dict[str, Any]]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM mission_evidence WHERE mission_id=? ORDER BY id", (mission_id,)
    ).fetchall()
    return [dict(row) | {"metadata": json.loads(row["metadata_json"] or "{}")} for row in rows]


def verify_evidence_chain(conn: sqlite3.Connection, mission_id: str) -> bool:
    rows = list_evidence(conn, mission_id)
    previous: Optional[str] = None
    for row in rows:
        canonical = json.dumps(
            {
                "mission_id": row["mission_id"], "task_id": row["task_id"],
                "run_id": row["run_id"], "tool_call_id": row["tool_call_id"],
                "kind": row["kind"], "created_at": row["created_at"],
                "started_at": row["started_at"], "finished_at": row["finished_at"],
                "cwd": row["cwd"], "command": row["command"],
                "exit_code": row["exit_code"], "status": row["status"],
                "metadata_json": row["metadata_json"], "blob_path": row["blob_path"],
                "blob_sha256": row["blob_sha256"], "blob_bytes": row["blob_bytes"],
                "previous_hash": previous,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if row["previous_hash"] != previous or row["record_hash"] != expected:
            return False
        if row["blob_path"]:
            try:
                with gzip.open(row["blob_path"], "rb") as fh:
                    blob = fh.read()
            except (OSError, EOFError):
                return False
            if (
                not row["blob_sha256"]
                or hashlib.sha256(blob).hexdigest() != row["blob_sha256"]
                or len(blob) != int(row["blob_bytes"] or 0)
            ):
                return False
        previous = expected
    return True


def open_intents(conn: sqlite3.Connection, mission_id: str) -> list[dict[str, Any]]:
    ensure_schema(conn)
    return [
        dict(row) for row in conn.execute(
            "SELECT * FROM mission_intents WHERE mission_id=? AND status='open' ORDER BY id",
            (mission_id,),
        )
    ]


def begin_intent(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    task_id: str,
    action: str,
    run_id: Optional[int] = None,
    claim_token: Optional[str] = None,
    checkpoint_ref: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> int:
    """Open one mutation intent per exact mission task run and action.

    Worker spawn can be retried after an uncertain caller boundary.  Returning
    the original row for the same run/claim/action makes that retry harmless
    while a new Kanban run (and therefore a new run id or claim token) still
    receives its own journal record.
    """
    ensure_schema(conn)
    with kb.write_txn(conn):
        existing = conn.execute(
            """SELECT id FROM mission_intents
                 WHERE mission_id=? AND task_id=? AND run_id IS ?
                   AND claim_token IS ? AND action=? AND status='open'
                 ORDER BY id LIMIT 1""",
            (mission_id, task_id, run_id, claim_token, action),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO mission_intents(
                mission_id, task_id, run_id, claim_token, action, checkpoint_ref, status,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (
                mission_id, task_id, run_id, claim_token, action, checkpoint_ref,
                json.dumps(details or {}, sort_keys=True), int(time.time()),
            ),
        )
        return int(cur.lastrowid)


def resolve_task_intents(
    conn: sqlite3.Connection,
    mission_id: str,
    task_id: str,
    *,
    outcome: str,
    run_id: Optional[int] = None,
    claim_token: Optional[str] = None,
) -> int:
    ensure_schema(conn)
    with kb.write_txn(conn):
        clauses = ["mission_id=?", "task_id=?", "status='open'"]
        params: list[Any] = [mission_id, task_id]
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(int(run_id))
        else:
            clauses.append("run_id IS NULL")
        if claim_token is not None:
            clauses.append("claim_token=?")
            params.append(claim_token)
        cur = conn.execute(
            "UPDATE mission_intents SET status=?, resolved_at=? WHERE " + " AND ".join(clauses),
            (outcome, int(time.time()), *params),
        )
        return int(cur.rowcount)
