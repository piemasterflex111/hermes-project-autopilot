"""Mission lifecycle service shared by CLI, slash, RPC, and UI adapters."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import missions_db as mdb


_SECRET_RE = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)"
    r"([^\s,'\"}]+)"
)


class MissionError(RuntimeError):
    pass


def _connection_db_path(conn) -> Path:
    """Return the file backing ``conn`` or fail closed for control actions."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(str(row[2])).resolve()
    raise MissionError("mission actions require a file-backed Kanban database")


def _mission_lock_seed(conn, mission_id: str) -> Path:
    """Return a stable, board-local path used to serialize one mission.

    Prefer the database actually backing ``conn`` instead of re-resolving the
    current board from process-global environment. This keeps explicit CLI,
    RPC, and dispatcher actions on the same cross-process lock even when more
    than one board is open.
    """
    db_path = _connection_db_path(conn)
    suffix = hashlib.sha256(mission_id.encode("utf-8")).hexdigest()[:24]
    return db_path.with_name(f"{db_path.name}.mission-{suffix}")


@contextlib.contextmanager
def _mission_action_lock(conn, mission_id: str):
    """Non-blocking, cross-process guard for verifier/control-plane actions."""
    with kb._dispatch_tick_lock(
        _mission_lock_seed(conn, mission_id), fail_open=False,
    ) as held:
        yield held


@contextlib.contextmanager
def _board_and_mission_action_lock(conn, mission_id: str):
    """Exclude dispatcher claim/spawn while changing mission execution state."""
    with kb._dispatch_tick_lock(_connection_db_path(conn), fail_open=False) as board_held:
        if not board_held:
            yield False
            return
        with _mission_action_lock(conn, mission_id) as mission_held:
            yield mission_held


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise MissionError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _git_ref(repo: Path, ref: str) -> Optional[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", ref],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60, check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def _run_git_bytes(
    repo: Path,
    *args: str,
    input_data: Optional[bytes] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> bytes:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], input=input_data, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60, check=False,
    )
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="replace").strip()
        raise MissionError(error or f"git {' '.join(args)} failed")
    return proc.stdout


def _git_common_dir(repo: Path) -> Path:
    value = _run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(value).resolve()


def _validate_worktree_identity(
    mission: mdb.Mission,
    *,
    expected_head: Optional[str] = None,
    require_clean: bool = False,
) -> dict[str, str]:
    """Prove that Git operations still target the mission's linked worktree."""
    if not (
        mission.worktree_path and mission.branch_name and mission.base_commit
        and mission.rollback_ref
    ):
        raise MissionError("mission worktree identity is incomplete")
    repo = Path(mission.repo_path).resolve()
    worktree = Path(mission.worktree_path).resolve()
    expected_path = repo.parent / ".hermes-worktrees" / repo.name / mission.id
    if worktree != expected_path.resolve() or worktree.is_relative_to(repo):
        raise MissionError("mission worktree path no longer matches its durable manifest")
    actual_root = Path(_run_git(worktree, "rev-parse", "--show-toplevel")).resolve()
    if actual_root != worktree:
        raise MissionError("mission worktree top-level identity mismatch")
    if _git_common_dir(worktree) != _git_common_dir(repo):
        raise MissionError("mission worktree belongs to a different Git repository")
    symbolic = _run_git(worktree, "symbolic-ref", "-q", "HEAD")
    expected_symbolic = f"refs/heads/{mission.branch_name}"
    if symbolic != expected_symbolic:
        raise MissionError("mission worktree branch identity mismatch")
    head = _run_git(worktree, "rev-parse", "HEAD")
    branch_head = _run_git(repo, "rev-parse", expected_symbolic)
    if branch_head != head:
        raise MissionError("mission branch and worktree HEAD disagree")
    rollback = _run_git(repo, "rev-parse", mission.rollback_ref)
    if rollback != mission.base_commit:
        raise MissionError("mission rollback ref no longer matches its immutable base")
    if expected_head is not None and head != expected_head:
        raise MissionError("mission worktree HEAD is outside the expected checkpoint")
    if require_clean and _run_git(worktree, "status", "--porcelain"):
        raise MissionError("mission worktree is not clean")
    return {"head": head, "branch_ref": expected_symbolic, "rollback": rollback}


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)


def _redact_value(value: Any) -> Any:
    """Recursively redact strings before durable evidence persistence."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    return value


def _mission_evidence_root(board: str, mission_id: str) -> Path:
    return kb.board_dir(board) / "mission-evidence" / mission_id


def store_evidence_blob(
    conn,
    mission: mdb.Mission,
    *,
    kind: str,
    status: str,
    content: str | bytes,
    metadata: Optional[dict[str, Any]] = None,
    **fields: Any,
) -> int:
    raw = content if isinstance(content, bytes) else _redact(content).encode("utf-8")
    metadata = _redact_value(metadata or {})
    evidence_limit = int(mission.budget.get("evidence_bytes", 100 * 1024 * 1024))
    prior = conn.execute(
        "SELECT COALESCE(SUM(blob_bytes), 0) FROM mission_evidence WHERE mission_id=?",
        (mission.id,),
    ).fetchone()[0]
    if int(prior) + len(raw) > evidence_limit:
        raise MissionError("mission evidence budget exceeded")
    digest = hashlib.sha256(raw).hexdigest()
    root = _mission_evidence_root(mission.board, mission.id)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{digest}.txt.gz"
    replace_target = not target.exists()
    if target.exists():
        try:
            with gzip.open(target, "rb") as fh:
                existing = fh.read()
            valid = hashlib.sha256(existing).hexdigest() == digest and len(existing) == len(raw)
        except (OSError, EOFError):
            valid = False
        if not valid:
            referenced = conn.execute(
                "SELECT 1 FROM mission_evidence WHERE blob_path=? LIMIT 1",
                (str(target),),
            ).fetchone()
            if referenced:
                raise MissionError("referenced evidence blob failed its integrity check")
            replace_target = True
    if replace_target:
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b", prefix=f".{digest}.", suffix=".tmp",
                dir=root, delete=False,
            ) as raw_fh:
                temp_path = Path(raw_fh.name)
                with gzip.GzipFile(fileobj=raw_fh, mode="wb") as compressed:
                    compressed.write(raw)
                raw_fh.flush()
                os.fsync(raw_fh.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, target)
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return mdb.record_evidence(
        conn,
        mission.id,
        kind=kind,
        status=status,
        metadata=metadata,
        blob_path=str(target),
        blob_sha256=digest,
        blob_bytes=len(raw),
        **fields,
    )


def create_mission(
    conn,
    *,
    objective: str,
    contract: mdb.MissionContract,
    autonomy_level: int,
    repo_path: str,
    board: str,
    project_id: str,
    risk_level: str = "medium",
    budget: Optional[dict[str, Any]] = None,
    deadline: Optional[int] = None,
) -> mdb.Mission:
    """Create a public mission only for a registered, clean Hermes project.

    The low-level database primitive remains available for migrations and
    focused tests. Every user-facing surface routes through this function.
    Execution-capable missions (levels 2-4) immediately create their isolated
    worktree, branch, rollback ref, and controller card.
    """
    ref = str(project_id or "").strip()
    if not ref:
        raise MissionError("a registered Hermes project is required")
    repo = Path(repo_path).expanduser().resolve()
    from hermes_cli import projects_db as pdb

    with pdb.connect_closing() as project_conn:
        project = pdb.get_project(project_conn, ref)
        if project is None or project.archived:
            raise MissionError(f"registered project {ref!r} does not exist or is archived")
        owner = pdb.project_for_path(project_conn, str(repo))
        if owner is None or owner.id != project.id:
            raise MissionError("repo_path is not owned by the selected registered project")
    try:
        top = Path(_run_git(repo, "rev-parse", "--show-toplevel")).resolve()
    except Exception as exc:
        raise MissionError("repo_path must be a Git repository root") from exc
    if top != repo:
        raise MissionError(f"repo_path must be the Git root ({top})")
    if _run_git(repo, "status", "--porcelain"):
        raise MissionError("source checkout must be clean before mission creation")
    if contract.allow_local_commit and autonomy_level != 4:
        raise MissionError("allow_local_commit is valid only for autonomy level 4")

    mission_id = mdb.create_mission(
        conn, objective=objective, contract=contract, autonomy_level=autonomy_level,
        repo_path=str(repo), board=board, risk_level=risk_level,
        project_id=project.id, budget=budget, deadline=deadline,
    )
    if autonomy_level >= 2:
        return prepare_mission(conn, mission_id)
    mission = mdb.get_mission(conn, mission_id)
    assert mission is not None
    return mission


def _prepare_mission_unlocked(conn, mission_id: str) -> mdb.Mission:
    """Create or adopt the isolated worktree, rollback ref, and root card."""
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    recovering = mission.status == "blocked" and mission.phase == "environment"
    if mission.status not in {"draft", "planning"} and not recovering:
        raise MissionError(f"mission must be draft, not {mission.status}")
    if mission.autonomy_level < 2:
        raise MissionError("autonomy level 2 or higher is required to prepare a worktree")
    repo = Path(mission.repo_path).resolve()
    top = Path(_run_git(repo, "rev-parse", "--show-toplevel")).resolve()
    if top != repo:
        raise MissionError(f"repo_path must be the Git root ({top})")
    source_head = _run_git(repo, "rev-parse", "HEAD")
    branch = f"hermes/mission-{mission.id[2:]}"
    rollback_ref = f"refs/hermes/rollback/{mission.id}"
    # Keep mission worktrees outside the source checkout. Repositories do not
    # consistently ignore `.worktrees/`; placing one inside an otherwise-clean
    # checkout would itself create unrelated source-tree state.
    worktree = repo.parent / ".hermes-worktrees" / repo.name / mission.id
    existing_rollback = _git_ref(repo, rollback_ref)
    base = mission.base_commit or existing_rollback or source_head
    if mission.base_commit and existing_rollback and mission.base_commit != existing_rollback:
        raise MissionError("stored base commit disagrees with the rollback ref")

    # Freeze the exact base/ref/path manifest in the same transaction as the
    # phase change. A crash after this point can adopt external Git artifacts
    # without consulting a potentially advanced source HEAD.
    mdb.begin_preparation(
        conn, mission.id, worktree_path=str(worktree), branch_name=branch,
        base_commit=base, rollback_ref=rollback_ref,
    )
    try:
        if existing_rollback is None:
            _run_git(repo, "-c", "core.hooksPath=/dev/null", "update-ref", rollback_ref, base)
        branch_ref = f"refs/heads/{branch}"
        branch_head = _git_ref(repo, branch_ref)
        if branch_head is not None and branch_head != base:
            raise MissionError("mission branch advanced before environment adoption")
        if worktree.exists():
            try:
                actual_root = Path(
                    _run_git(worktree, "rev-parse", "--show-toplevel")
                ).resolve()
            except Exception as exc:
                raise MissionError(
                    f"mission worktree path exists but is not adoptable: {worktree}"
                ) from exc
            if (
                actual_root != worktree.resolve()
                or _run_git(worktree, "rev-parse", "HEAD") != base
                or _run_git(worktree, "branch", "--show-current") != branch
            ):
                raise MissionError("existing mission worktree does not match its durable contract")
        elif branch_head is None:
            _run_git(
                repo, "-c", "core.hooksPath=/dev/null", "worktree", "add",
                "-b", branch, str(worktree), base,
            )
        else:
            _run_git(
                repo, "-c", "core.hooksPath=/dev/null", "worktree", "add",
                str(worktree), branch,
            )
        prepared = mdb.get_mission(conn, mission.id)
        assert prepared is not None
        _validate_worktree_identity(
            prepared, expected_head=base, require_clean=True,
        )
        root_task = kb.create_task(
            conn,
            title=f"Mission: {mission.objective[:70]}",
            body=(
                f"Objective: {mission.objective}\n\n"
                f"Completion contract:\n{mission.contract.to_json()}"
            ),
            assignee=None,
            created_by="mission-controller",
            workspace_kind="worktree",
            workspace_path=str(worktree),
            branch_name=branch,
            project_id=mission.project_id,
            initial_status="blocked",
            idempotency_key=f"mission:{mission.id}:controller",
        )
        mdb.link_task(conn, mission.id, root_task, "controller")
        mdb.set_root_task(conn, mission.id, root_task)
        refreshed = mdb.get_mission(conn, mission.id)
        assert refreshed is not None
        environment_recorded = any(
            row["kind"] == "environment"
            and row["status"] == "passed"
            and row["metadata"].get("base_commit") == base
            for row in mdb.list_evidence(conn, mission.id)
        )
        if not environment_recorded:
            store_evidence_blob(
                conn,
                refreshed,
                kind="environment",
                status="passed",
                content=json.dumps(
                    {
                        "repo": str(repo),
                        "source_status": _run_git(repo, "status", "--porcelain"),
                        "base_commit": base, "worktree": str(worktree), "branch": branch,
                        "rollback_ref": rollback_ref,
                    },
                    sort_keys=True,
                ),
                metadata={"base_commit": base, "rollback_ref": rollback_ref},
                cwd=str(repo),
            )
        return refreshed
    except Exception as exc:
        current = mdb.get_mission(conn, mission.id)
        if current and current.status == "planning":
            mdb.transition_mission(
                conn, mission.id, "blocked", phase="environment",
                blocked_reason=f"environment preparation failed: {exc}",
            )
        raise


def prepare_mission(conn, mission_id: str) -> mdb.Mission:
    """Idempotently prepare one mission under its cross-process lock."""
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission preparation is already running")
        return _prepare_mission_unlocked(conn, mission_id)


def inspect_mission(conn, mission_id: str) -> mdb.Mission:
    """Perform the level-1 read-only repository inspection without mutation."""
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.autonomy_level < 1:
        raise MissionError("autonomy level 1 or higher is required to inspect the environment")
    if mission.status != "draft":
        raise MissionError("read-only inspection is available while the mission is a draft")
    repo = Path(mission.repo_path).resolve()
    top = Path(_run_git(repo, "rev-parse", "--show-toplevel")).resolve()
    if top != repo:
        raise MissionError(f"repo_path must be the Git root ({top})")
    mdb.record_evidence(
        conn, mission_id, kind="inspection", status="passed",
        metadata={
            "repo": str(repo),
            "head": _run_git(repo, "rev-parse", "HEAD"),
            "branch": _run_git(repo, "branch", "--show-current"),
            "dirty": bool(_run_git(repo, "status", "--porcelain")),
        },
        cwd=str(repo),
    )
    return mdb.get_mission(conn, mission_id)  # type: ignore[return-value]


def add_execution_task(
    conn,
    mission_id: str,
    *,
    title: str,
    body: str,
    assignee: str,
    parents: Optional[list[str]] = None,
) -> str:
    """Add one serialized executor task to a prepared mission plan."""
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission planning is already running")
        return _add_execution_task_unlocked(
            conn, mission_id, title=title, body=body, assignee=assignee,
            parents=parents,
        )


def _add_execution_task_unlocked(
    conn,
    mission_id: str,
    *,
    title: str,
    body: str,
    assignee: str,
    parents: Optional[list[str]] = None,
) -> str:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status != "planning" or not mission.worktree_path:
        raise MissionError("execution tasks can only be added while planning")
    if mdb.get_plan(conn, mission_id) is not None:
        raise MissionError("a durable automatic plan already owns this mission graph")
    existing = conn.execute(
        "SELECT id FROM tasks WHERE mission_id=? AND mission_role='executor' ORDER BY created_at, id",
        (mission_id,),
    ).fetchall()
    dependencies = list(parents or [])
    if not dependencies and existing:
        dependencies = [existing[-1][0]]
    elif not dependencies and mission.root_task_id:
        # The blocked controller task is the mission's start latch.  The
        # first executor cannot become ready until start_mission completes it.
        dependencies = [mission.root_task_id]
    if not dependencies:
        raise MissionError("every manual executor must depend on the mission start graph")
    placeholders = ",".join("?" for _ in dependencies)
    dependency_rows = conn.execute(
        f"SELECT id,mission_id FROM tasks WHERE id IN ({placeholders})",
        tuple(dependencies),
    ).fetchall()
    if (
        len(dependency_rows) != len(set(dependencies))
        or any(row["mission_id"] != mission_id for row in dependency_rows)
    ):
        raise MissionError("manual mission dependencies must be cards in the same mission")
    with kb._trusted_mission_controller_mutation():
        task_id = kb.create_task(
            conn, title=title, body=body, assignee=assignee,
            created_by="mission-planner", parents=dependencies,
            workspace_kind="worktree", workspace_path=mission.worktree_path,
            branch_name=mission.branch_name, project_id=mission.project_id,
            goal_mode=True,
        )
    mdb.link_task(conn, mission_id, task_id, "executor")
    return task_id


_PLAN_VERIFIER_TITLE = "Independently verify mission acceptance criteria"
_PLAN_VERIFIER_BODY = (
    "Read the mission contract, Git diff, and evidence. Do not trust executor "
    "claims. This task is read-only: report missing evidence or violations; "
    "the mission controller alone may commit."
)


def _canonical_assignee(value: str, *, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissionError(f"{role} assignee is required")
    assignee = kb._canonical_assignee(value.strip())
    if not assignee:
        raise MissionError(f"{role} assignee is required")
    return assignee


def _canonicalize_plan_payload(payload: Any) -> tuple[str, list[dict[str, str]]]:
    if not isinstance(payload, dict) or set(payload) != {"tasks"}:
        raise MissionError("planner payload must contain only the tasks field")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12:
        raise MissionError("planner must return between 1 and 12 tasks")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(tasks, start=1):
        if not isinstance(item, dict):
            raise MissionError(f"planner task {index} is not an object")
        if set(item) != {"title", "body"}:
            raise MissionError(
                f"planner task {index} must contain only title and body"
            )
        title_value = item.get("title")
        body_value = item.get("body")
        if not isinstance(title_value, str) or not isinstance(body_value, str):
            raise MissionError(f"planner task {index} lacks title or body")
        title = title_value.strip()
        body = body_value.strip()
        if not title or not body:
            raise MissionError(f"planner task {index} lacks title or body")
        normalized.append({"title": title, "body": body})
    canonical = json.dumps(
        {"tasks": normalized}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return canonical, normalized


def _load_canonical_plan(row: dict[str, Any]) -> tuple[list[dict[str, str]], str, str]:
    payload_json = str(row["payload_json"])
    plan_hash = str(row["plan_hash"])
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != plan_hash:
        raise MissionError("durable mission plan failed its integrity check")
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise MissionError("durable mission plan contains malformed JSON") from exc
    canonical, tasks = _canonicalize_plan_payload(payload)
    if canonical != payload_json:
        raise MissionError("durable mission plan is not canonical")
    executor_assignee = _canonical_assignee(
        str(row["executor_assignee"]), role="executor",
    )
    verifier_assignee = _canonical_assignee(
        str(row["verifier_assignee"]), role="verifier",
    )
    if (
        executor_assignee != row["executor_assignee"]
        or verifier_assignee != row["verifier_assignee"]
    ):
        raise MissionError("durable mission plan assignees are not canonical")
    return tasks, executor_assignee, verifier_assignee


def _plan_task_key(mission_id: str, plan_hash: str, role: str, ordinal: int = 0) -> str:
    return f"mission:{mission_id}:plan:{plan_hash}:{role}:{ordinal:02d}"


def _validate_planned_card(
    conn,
    mission: mdb.Mission,
    task_id: str,
    *,
    role: str,
    title: str,
    body: str,
    assignee: str,
    idempotency_key: str,
    parents: list[str],
    allow_unlinked: bool = False,
) -> None:
    task = kb.get_task(conn, task_id)
    if task is None:
        raise MissionError(f"planned {role} card disappeared: {task_id}")
    expected = {
        "title": title,
        "body": body,
        "assignee": assignee,
        "created_by": "mission-planner",
        "workspace_kind": "worktree",
        "workspace_path": mission.worktree_path,
        "branch_name": mission.branch_name,
        "project_id": mission.project_id,
        "idempotency_key": idempotency_key,
        "goal_mode": True,
    }
    mismatches = [
        field for field, value in expected.items()
        if getattr(task, field) != value
    ]
    if task.status == "archived":
        mismatches.append("status")
    if mission.status == "planning" and task.status != "todo":
        mismatches.append("status")
    if allow_unlinked:
        if (task.mission_id, task.mission_role) not in {
            (None, None), (mission.id, role),
        }:
            mismatches.extend(("mission_id", "mission_role"))
    else:
        if task.mission_id != mission.id:
            mismatches.append("mission_id")
        if task.mission_role != role:
            mismatches.append("mission_role")
    if mismatches:
        raise MissionError(
            f"adopted {role} card {task_id} does not match its canonical spec: "
            + ", ".join(sorted(set(mismatches)))
        )
    if kb.parent_ids(conn, task_id) != sorted(parents):
        raise MissionError(f"adopted {role} card {task_id} has unexpected dependencies")


_TASK_MANIFEST_FIELDS = (
    "id", "title", "body", "assignee", "priority", "created_by",
    "workspace_kind", "workspace_path", "branch_name", "project_id",
    "tenant", "idempotency_key", "max_runtime_seconds", "skills",
    "max_retries", "model_override", "provider_override", "goal_mode",
    "goal_max_turns", "session_id", "workflow_template_id",
    "current_step_key", "mission_id", "mission_role",
)


def _task_manifest_payload(
    conn, mission: mdb.Mission,
) -> tuple[str, dict[str, Any]]:
    """Serialize every immutable card field and every mission DAG edge."""
    rows = conn.execute(
        """SELECT * FROM tasks
             WHERE mission_id=?
             ORDER BY CASE mission_role
                        WHEN 'controller' THEN 0
                        WHEN 'executor' THEN 1
                        WHEN 'verifier' THEN 2
                        ELSE 3 END,
                      created_at,id""",
        (mission.id,),
    ).fetchall()
    if not rows:
        raise MissionError("mission has no materialized task cards")
    cards = [
        {field: row[field] for field in _TASK_MANIFEST_FIELDS}
        for row in rows
    ]
    ids = {str(card["id"]) for card in cards}
    controllers = [
        card for card in cards if card["mission_role"] == "controller"
    ]
    executors = [card for card in cards if card["mission_role"] == "executor"]
    verifiers = [card for card in cards if card["mission_role"] == "verifier"]
    if (
        len(controllers) != 1
        or controllers[0]["id"] != mission.root_task_id
        or not executors
        or len(verifiers) != 1
        or any(
            card["mission_role"] not in {"controller", "executor", "verifier"}
            for card in cards
        )
    ):
        raise MissionError(
            "mission task graph must contain its controller, at least one "
            "executor, and exactly one verifier"
        )
    placeholders = ",".join("?" for _ in ids)
    edge_rows = conn.execute(
        f"""SELECT parent_id,child_id FROM task_links
              WHERE parent_id IN ({placeholders})
                 OR child_id IN ({placeholders})
              ORDER BY parent_id,child_id""",
        (*sorted(ids), *sorted(ids)),
    ).fetchall()
    edges = [[str(row["parent_id"]), str(row["child_id"])] for row in edge_rows]
    outside = sorted(
        endpoint
        for edge in edges
        for endpoint in edge
        if endpoint not in ids
    )
    if outside:
        raise MissionError(
            "mission task graph contains edges to non-mission cards: "
            + ", ".join(sorted(set(outside)))
        )
    payload = {
        "mission_id": mission.id,
        "root_task_id": mission.root_task_id,
        "cards": cards,
        "edges": edges,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return canonical, payload


def _load_task_manifest(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    payload_json = str(row["payload_json"])
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if digest != row["manifest_hash"]:
        raise MissionError("durable mission task manifest failed its integrity check")
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise MissionError("durable mission task manifest contains malformed JSON") from exc
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    if canonical != payload_json:
        raise MissionError("durable mission task manifest is not canonical")
    return payload_json, payload


def _accept_or_validate_task_manifest(conn, mission: mdb.Mission) -> dict[str, Any]:
    """Seal a planning result once, then require byte-exact graph identity."""
    row = mdb.get_task_manifest(conn, mission.id)
    if row is None and mission.status != "planning":
        reason = (
            "mission predates immutable task manifests; execution is "
            "blocked because its cards cannot be authenticated safely. "
            "Preserve or roll back the isolated worktree, then recreate "
            "the mission."
        )
        if mission.status in {
            "ready", "running", "waiting_for_user", "verifying",
            "awaiting_approval", "committing",
        }:
            mdb.transition_mission(
                conn,
                mission.id,
                "blocked",
                phase="migration",
                blocked_reason=reason,
            )
        raise MissionError(reason)
    current_json, current = _task_manifest_payload(conn, mission)
    if row is None:
        try:
            row = mdb.store_task_manifest(
                conn,
                mission.id,
                manifest_hash=hashlib.sha256(current_json.encode("utf-8")).hexdigest(),
                payload_json=current_json,
            )
        except ValueError as exc:
            raise MissionError(str(exc)) from exc
    accepted_json, accepted = _load_task_manifest(row)
    if accepted.get("mission_id") != mission.id:
        raise MissionError("mission task manifest belongs to a different mission")
    if accepted_json != current_json:
        raise MissionError(
            "mission task cards or dependency graph no longer match the "
            "immutable accepted manifest"
        )
    expected_bindings = sorted(
        (str(card["id"]), str(card["mission_role"]))
        for card in current["cards"]
    )
    stored_bindings = [
        (str(row["task_id"]), str(row["role"]))
        for row in conn.execute(
            """SELECT task_id,role FROM mission_task_manifest_cards
                 WHERE mission_id=? ORDER BY task_id""",
            (mission.id,),
        ).fetchall()
    ]
    if stored_bindings != expected_bindings:
        raise MissionError("mission task manifest bindings failed their integrity check")
    return current


def validate_mission_task_graph(
    conn,
    mission_id: str,
    *,
    boundary: str,
    expected_task_id: Optional[str] = None,
    expected_workspace: Optional[str] = None,
) -> dict[str, Any]:
    """Revalidate the accepted graph at every execution trust boundary."""
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    manifest = _accept_or_validate_task_manifest(conn, mission)
    cards = {str(card["id"]): card for card in manifest["cards"]}
    if expected_task_id is not None:
        card = cards.get(expected_task_id)
        if card is None or card.get("mission_role") != "executor":
            raise MissionError(
                f"task {expected_task_id} is not an accepted mission executor"
            )
        if card.get("workspace_path") != mission.worktree_path:
            raise MissionError("mission executor workspace differs from its mission worktree")
        if expected_workspace != mission.worktree_path:
            raise MissionError("spawned executor workspace differs from its accepted workspace")
        try:
            accepted_path = Path(str(mission.worktree_path)).resolve(strict=True)
            supplied_path = Path(str(expected_workspace)).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MissionError("mission executor workspace cannot be resolved") from exc
        if supplied_path != accepted_path:
            raise MissionError("spawned executor workspace resolves outside its accepted workspace")
    if boundary == "start":
        status_rows = conn.execute(
            "SELECT id,mission_role,status FROM tasks WHERE mission_id=?",
            (mission_id,),
        ).fetchall()
        invalid = [
            str(row["id"])
            for row in status_rows
            if (
                row["mission_role"] == "controller"
                and row["status"] not in {"blocked", "ready"}
            ) or (
                row["mission_role"] in {"executor", "verifier"}
                and row["status"] not in {"todo", "ready"}
            )
        ]
        if invalid:
            raise MissionError(
                "mission cards advanced before the explicit start gate: "
                + ", ".join(invalid)
            )
    return manifest


def _create_or_adopt_planned_card(
    conn,
    mission: mdb.Mission,
    *,
    role: str,
    title: str,
    body: str,
    assignee: str,
    idempotency_key: str,
    parents: list[str],
) -> str:
    matches = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key=? ORDER BY created_at,id",
        (idempotency_key,),
    ).fetchall()
    if len(matches) > 1:
        raise MissionError(f"duplicate planned-card idempotency key: {idempotency_key}")
    if matches:
        task_id = matches[0]["id"]
    else:
        with kb._trusted_mission_controller_mutation():
            task_id = kb.create_task(
                conn,
                title=title,
                body=body,
                assignee=assignee,
                created_by="mission-planner",
                parents=parents,
                workspace_kind="worktree",
                workspace_path=mission.worktree_path,
                branch_name=mission.branch_name,
                # An empty value bypasses create_task's mutable board-level project
                # inheritance while normalizing back to NULL. The mission record is
                # the durable routing contract for automatic cards.
                project_id=mission.project_id if mission.project_id is not None else "",
                goal_mode=True,
                idempotency_key=idempotency_key,
            )
    _validate_planned_card(
        conn, mission, task_id, role=role, title=title, body=body,
        assignee=assignee, idempotency_key=idempotency_key, parents=parents,
        allow_unlinked=True,
    )
    try:
        mdb.link_task(conn, mission.id, task_id, role)
    except (KeyError, ValueError) as exc:
        raise MissionError(
            f"cannot adopt {role} card {task_id} for mission {mission.id}"
        ) from exc
    _validate_planned_card(
        conn, mission, task_id, role=role, title=title, body=body,
        assignee=assignee, idempotency_key=idempotency_key, parents=parents,
    )
    return task_id


def _record_plan_evidence_once(
    conn, mission: mdb.Mission, *, plan_hash: str, task_count: int,
) -> None:
    exists = any(
        row["kind"] == "plan"
        and row["status"] == "passed"
        and row["metadata"].get("plan_hash") == plan_hash
        for row in mdb.list_evidence(conn, mission.id)
    )
    if not exists:
        mdb.record_evidence(
            conn, mission.id, kind="plan", status="passed",
            metadata={
                "task_count": task_count,
                "plan_hash": plan_hash,
                "planner_output_hash": plan_hash,
            },
            cwd=mission.worktree_path,
        )


def _materialize_canonical_plan(
    conn, mission: mdb.Mission, plan_row: dict[str, Any],
) -> mdb.Mission:
    tasks, executor_assignee, verifier_assignee = _load_canonical_plan(plan_row)
    plan_hash = str(plan_row["plan_hash"])
    executor_ids: list[str] = []
    for ordinal, item in enumerate(tasks, start=1):
        parents = (
            [executor_ids[-1]] if executor_ids
            else ([mission.root_task_id] if mission.root_task_id else [])
        )
        if not parents:
            raise MissionError("mission plan has no controller start latch")
        task_id = _create_or_adopt_planned_card(
            conn, mission,
            role="executor",
            title=item["title"],
            body=item["body"],
            assignee=executor_assignee,
            idempotency_key=_plan_task_key(
                mission.id, plan_hash, "executor", ordinal,
            ),
            parents=parents,
        )
        executor_ids.append(task_id)

    verifier_id = _create_or_adopt_planned_card(
        conn, mission,
        role="verifier",
        title=_PLAN_VERIFIER_TITLE,
        body=_PLAN_VERIFIER_BODY,
        assignee=verifier_assignee,
        idempotency_key=_plan_task_key(mission.id, plan_hash, "verifier", 1),
        parents=executor_ids,
    )
    linked_executors = conn.execute(
        """SELECT id FROM tasks
             WHERE mission_id=? AND mission_role='executor'""",
        (mission.id,),
    ).fetchall()
    if {row["id"] for row in linked_executors} != set(executor_ids):
        raise MissionError("mission contains executor cards outside its canonical plan")
    linked_verifiers = conn.execute(
        """SELECT id FROM tasks
             WHERE mission_id=? AND mission_role='verifier'""",
        (mission.id,),
    ).fetchall()
    if [row["id"] for row in linked_verifiers] != [verifier_id]:
        raise MissionError("mission must contain exactly one canonical verifier card")
    _accept_or_validate_task_manifest(conn, mission)
    _record_plan_evidence_once(
        conn, mission, plan_hash=plan_hash, task_count=len(tasks),
    )
    refreshed = mdb.get_mission(conn, mission.id)
    assert refreshed is not None
    if refreshed.status == "planning":
        return mdb.transition_mission(
            conn, mission.id, "ready", phase="execution",
        )
    return refreshed


def plan_with_model(
    conn,
    mission_id: str,
    *,
    executor_assignee: str,
    verifier_assignee: str,
) -> mdb.Mission:
    """Create or resume a bounded serialized DAG from a durable proposal."""
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission planning is already running")
        mission = mdb.get_mission(conn, mission_id)
        if mission is None:
            raise KeyError(mission_id)
        plan_row = mdb.get_plan(conn, mission_id)
        if plan_row is None:
            if mission.status != "planning":
                raise MissionError("mission must be prepared before automatic planning")
            existing = conn.execute(
                """SELECT id FROM tasks
                     WHERE mission_id=? AND mission_role IN ('executor','verifier')
                     LIMIT 1""",
                (mission_id,),
            ).fetchone()
            if existing:
                raise MissionError(
                    "automatic planning cannot adopt pre-existing manual mission cards"
                )
            canonical_executor = _canonical_assignee(
                executor_assignee, role="executor",
            )
            canonical_verifier = _canonical_assignee(
                verifier_assignee, role="verifier",
            )
            from agent.auxiliary_client import call_llm

            system = (
                "You are a mission planner. Return JSON only: "
                '{"tasks":[{"title":"...","body":"..."}]}. '
                "Create 1-12 bounded implementation tasks in execution order. Every body "
                "must name its expected output and verification. Do not claim to execute work."
            )
            user = json.dumps(
                {
                    "objective": mission.objective,
                    "contract": asdict(mission.contract),
                    "repo_path": mission.repo_path,
                    "worktree_path": mission.worktree_path,
                },
                ensure_ascii=False,
            )
            response = call_llm(
                task="mission_planner",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
                max_tokens=6000,
                timeout=120,
            )
            raw = (response.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = re.sub(
                    r"^```(?:json)?\s*|\s*```$", "", raw,
                    flags=re.IGNORECASE,
                )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MissionError("planner returned malformed JSON") from exc
            canonical, _ = _canonicalize_plan_payload(payload)
            plan_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if conn.execute(
                """SELECT 1 FROM tasks
                     WHERE mission_id=? AND mission_role IN ('executor','verifier')
                     LIMIT 1""",
                (mission_id,),
            ).fetchone():
                raise MissionError(
                    "mission cards changed while automatic planning was running"
                )
            try:
                plan_row = mdb.store_plan(
                    conn,
                    mission_id,
                    plan_hash=plan_hash,
                    payload_json=canonical,
                    executor_assignee=canonical_executor,
                    verifier_assignee=canonical_verifier,
                )
            except ValueError as exc:
                raise MissionError(str(exc)) from exc
        return _materialize_canonical_plan(conn, mission, plan_row)


def finish_plan(conn, mission_id: str, *, verifier_assignee: str) -> mdb.Mission:
    """Finish a manual plan once; exact retries adopt the same verifier card."""
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission planning is already running")
        mission = mdb.get_mission(conn, mission_id)
        if mission is None:
            raise KeyError(mission_id)
        plan_row = mdb.get_plan(conn, mission_id)
        if plan_row is not None:
            return _materialize_canonical_plan(conn, mission, plan_row)
        executor_rows = conn.execute(
            """SELECT id FROM tasks
                 WHERE mission_id=? AND mission_role='executor'
                 ORDER BY created_at, id""",
            (mission_id,),
        ).fetchall()
        if not executor_rows:
            raise MissionError("a mission plan requires at least one executor task")
        verifier_rows = conn.execute(
            """SELECT id FROM tasks
                 WHERE mission_id=? AND mission_role='verifier'""",
            (mission_id,),
        ).fetchall()
        if len(verifier_rows) > 1:
            raise MissionError("mission must contain exactly one verifier card")
        canonical_verifier = _canonical_assignee(
            verifier_assignee, role="verifier",
        )
        parents = [row["id"] for row in executor_rows]
        verifier_key = f"mission:{mission_id}:manual:verifier"
        if verifier_rows:
            verifier = verifier_rows[0]["id"]
            _validate_planned_card(
                conn, mission, verifier,
                role="verifier",
                title=_PLAN_VERIFIER_TITLE,
                body=_PLAN_VERIFIER_BODY,
                assignee=canonical_verifier,
                idempotency_key=verifier_key,
                parents=parents,
            )
        else:
            if mission.status != "planning":
                raise MissionError("mission must be planning before adding its verifier")
            verifier = _create_or_adopt_planned_card(
                conn, mission,
                role="verifier",
                title=_PLAN_VERIFIER_TITLE,
                body=_PLAN_VERIFIER_BODY,
                assignee=canonical_verifier,
                idempotency_key=verifier_key,
                parents=parents,
            )
        _accept_or_validate_task_manifest(conn, mission)
        refreshed = mdb.get_mission(conn, mission_id)
        assert refreshed is not None
        if refreshed.status == "planning":
            return mdb.transition_mission(
                conn, mission_id, "ready", phase="execution",
            )
        return refreshed


def _start_mission_unlocked(conn, mission_id: str) -> mdb.Mission:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status not in {"ready", "blocked"}:
        raise MissionError(f"cannot start mission from {mission.status}")
    if mission.autonomy_level < 3:
        raise MissionError("autonomy level 3 or higher is required to execute a mission")
    if mdb.open_intents(conn, mission_id):
        raise MissionError("mission has unresolved mutation intents; reconcile before starting")
    boundaries = mission.contract.boundaries
    destinations = boundaries.get("network_destinations", [])
    if destinations:
        raise MissionError(
            "Project Autopilot v1 cannot enforce destination allowlists; "
            "network_destinations must be empty"
        )
    terminal_backends = set(boundaries.get("allowed_terminal_backends") or ["docker"])
    if terminal_backends != {"docker"}:
        raise MissionError(
            "Project Autopilot v1 supports only allowed_terminal_backends=['docker']"
        )
    validate_mission_task_graph(conn, mission_id, boundary="start")
    _validate_worktree_identity(
        mission, expected_head=mission.base_commit, require_clean=True,
    )
    if not mission.root_task_id:
        raise MissionError("mission has no controller task")
    root = kb.get_task(conn, mission.root_task_id)
    if root and root.status != "done":
        if root.status != "ready":
            with kb._trusted_mission_controller_mutation():
                promoted, reason = kb.promote_task(
                    conn, mission.root_task_id, actor="mission-controller",
                    reason="explicit mission start", force=True,
                )
            if not promoted:
                raise MissionError(reason or "could not release mission controller")
        if not kb.complete_task(
            conn, mission.root_task_id,
            result="Mission contract approved for bounded execution.",
            summary="Explicit start gate released by mission controller.",
        ):
            raise MissionError("could not complete mission controller start gate")
    return mdb.transition_mission(conn, mission_id, "running", phase="execution")


def start_mission(conn, mission_id: str) -> mdb.Mission:
    with _board_and_mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission start is busy; retry on the next dispatcher tick")
        return _start_mission_unlocked(conn, mission_id)


def _pause_mission_unlocked(conn, mission_id: str) -> mdb.Mission:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status == "running":
        # Revoke the durable lease before touching host processes or Docker.
        # If the controller dies during quiescence, restart sees a safe
        # waiting/pausing mission rather than a mutation-capable running one.
        mission = mdb.transition_mission(
            conn,
            mission_id,
            "waiting_for_user",
            phase="pausing",
            blocked_reason="pause in progress",
        )
    elif not (
        mission.status == "waiting_for_user" and mission.phase == "pausing"
    ):
        raise MissionError("only a running mission can be paused")
    return _finish_mission_pause_unlocked(conn, mission_id)


def _finish_mission_pause_unlocked(conn, mission_id: str) -> mdb.Mission:
    """Complete a durable, restart-safe pause after lease revocation."""
    _quiesce_mission_executors(
        conn,
        mission_id,
        resolve_outcome="paused",
        mark_paused=True,
    )
    with kb.write_txn(conn):
        cur = conn.execute(
            """UPDATE missions
                  SET phase='paused',blocked_reason='paused by operator',updated_at=?
                WHERE id=? AND status='waiting_for_user'
                  AND phase IN ('pausing','paused')""",
            (int(time.time()), mission_id),
        )
        if cur.rowcount != 1:
            raise MissionError("mission left the pause state during quiescence")
    paused = mdb.get_mission(conn, mission_id)
    assert paused is not None
    return paused


def pause_mission(conn, mission_id: str) -> mdb.Mission:
    with _board_and_mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission pause is busy; retry on the next dispatcher tick")
        return _pause_mission_unlocked(conn, mission_id)


def _resume_mission_unlocked(conn, mission_id: str) -> mdb.Mission:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status != "waiting_for_user":
        raise MissionError("mission is not paused")
    if mission.phase == "pausing":
        mission = _finish_mission_pause_unlocked(conn, mission_id)
    if mission.phase != "paused":
        raise MissionError(f"mission is waiting in non-pause phase {mission.phase!r}")
    if mdb.open_intents(conn, mission_id):
        raise MissionError("mission has unresolved mutation intents; reconcile before resuming")
    validate_mission_task_graph(conn, mission_id, boundary="resume")
    _resume_controller_paused_executors(conn, mission_id)
    return mdb.transition_mission(conn, mission_id, "running", phase="execution")


def resume_mission(conn, mission_id: str) -> mdb.Mission:
    with _board_and_mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission resume is busy; retry on the next dispatcher tick")
        return _resume_mission_unlocked(conn, mission_id)


def _resume_controller_paused_executors(conn, mission_id: str) -> None:
    """Requeue only executor cards quiesced by the mission pause control.

    The paired task events are the durable ownership marker. A genuine worker
    block has no ``mission_paused`` event and is therefore never unblocked by
    a broad mission resume. Accepting ``ready``/``todo`` makes recovery
    idempotent if the process stopped after requeueing a card but before
    recording its matching ``mission_resumed`` event.
    """
    rows = conn.execute(
        """SELECT t.id,t.status,
                  (SELECT e.id FROM task_events e
                    WHERE e.task_id=t.id AND e.kind='mission_paused'
                    ORDER BY e.id DESC LIMIT 1) AS paused_event_id,
                  (SELECT e.run_id FROM task_events e
                    WHERE e.task_id=t.id AND e.kind='mission_paused'
                    ORDER BY e.id DESC LIMIT 1) AS paused_run_id,
                  (SELECT e.id FROM task_events e
                    WHERE e.task_id=t.id AND e.kind='mission_resumed'
                    ORDER BY e.id DESC LIMIT 1) AS resumed_event_id
             FROM tasks t
            WHERE t.mission_id=? AND t.mission_role='executor'
            ORDER BY t.id""",
        (mission_id,),
    ).fetchall()
    pending = [
        row for row in rows
        if row["paused_event_id"] is not None
        and int(row["paused_event_id"]) > int(row["resumed_event_id"] or 0)
    ]
    for row in pending:
        task_id = str(row["id"])
        status = str(row["status"])
        if status == "blocked":
            with kb._trusted_mission_controller_mutation():
                if not kb.unblock_task(conn, task_id):
                    raise MissionError(f"could not resume paused executor task {task_id}")
        elif status not in {"ready", "todo"}:
            raise MissionError(
                f"paused executor task {task_id} changed to unexpected state {status!r}"
            )
        with kb.write_txn(conn):
            # Operator pauses are control-plane events, not recurring worker
            # blockers. Clear the loop-breaker state so repeated pause/resume
            # cycles can never route a healthy executor to triage.
            conn.execute(
                """UPDATE tasks SET block_kind=NULL,block_recurrences=0
                     WHERE id=? AND status IN ('ready','todo')""",
                (task_id,),
            )
            kb._append_event(
                conn,
                task_id,
                "mission_resumed",
                {"mission_id": mission_id},
                run_id=row["paused_run_id"],
            )
            conn.execute(
                """UPDATE mission_intents SET status='resumed'
                     WHERE mission_id=? AND task_id=? AND status='paused'""",
                (mission_id, task_id),
            )


def _remove_mission_containers(conn, mission_id: str, task_ids: list[str]) -> None:
    """Force-remove Docker sandboxes dedicated to live mission executors."""
    if not task_ids:
        return
    from tools.environments.docker import (
        _mission_db_fingerprint,
        _sanitize_label_value,
        find_docker,
    )

    docker = find_docker()
    if not docker:
        raise MissionError("cannot prove executor quiescence: Docker is unavailable")
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if not mission.worktree_path:
        raise MissionError("cannot prove executor quiescence without a mission worktree")
    expected_worktree = Path(mission.worktree_path).resolve(strict=False)
    expected_common = {
        "hermes-agent": "1",
        "hermes-mission-id": _sanitize_label_value(mission_id),
        "hermes-board": _sanitize_label_value(mission.board),
        "hermes-board-db": _mission_db_fingerprint(str(_connection_db_path(conn))),
    }
    for task_id in task_ids:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", task_id):
            raise MissionError(f"unsafe task id in Docker label: {task_id!r}")
        # Probe by the two labels shared by both the initial Autopilot release
        # and current containers. Querying only the newer mission/board labels
        # would make an orphan created during an upgrade invisible and falsely
        # report quiescence.
        filters = [
            "--filter", "label=hermes-agent=1",
            "--filter", f"label=hermes-task-id={task_id}",
        ]
        valid_run_labels = {
            _sanitize_label_value(str(row[0]))
            for row in conn.execute(
                "SELECT id FROM task_runs WHERE task_id=?", (task_id,),
            ).fetchall()
        }
        probe = subprocess.run(
            [docker, "ps", "-aq", *filters], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False,
        )
        if probe.returncode != 0:
            raise MissionError(
                "cannot prove executor quiescence: "
                + (probe.stderr.strip() or "Docker container lookup failed")
            )
        container_ids = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        for container_id in container_ids:
            if not re.fullmatch(r"[A-Fa-f0-9]{12,64}", container_id):
                raise MissionError("Docker returned an unsafe mission container id")
            inspected = subprocess.run(
                [docker, "inspect", "--format", "{{json .Config.Labels}}", container_id],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15, check=False,
            )
            try:
                labels = json.loads(inspected.stdout) if inspected.returncode == 0 else None
            except json.JSONDecodeError:
                labels = None
            mounted = subprocess.run(
                [docker, "inspect", "--format", "{{json .Mounts}}", container_id],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15, check=False,
            )
            try:
                mounts = json.loads(mounted.stdout) if mounted.returncode == 0 else None
            except json.JSONDecodeError:
                mounts = None
            expected = expected_common | {
                "hermes-task-id": _sanitize_label_value(task_id),
            }
            binding_keys = {
                "hermes-mission-id", "hermes-board", "hermes-board-db",
                "hermes-run-id",
            }
            has_current_binding = isinstance(labels, dict) and binding_keys.issubset(labels)
            is_legacy_binding = isinstance(labels, dict) and not any(
                key in labels for key in binding_keys
            )
            mount_matches = isinstance(mounts, list) and any(
                isinstance(item, dict)
                and item.get("Destination") == "/workspace"
                and item.get("Type") == "bind"
                and bool(str(item.get("Source") or "").strip())
                and Path(str(item.get("Source") or "")).resolve(strict=False)
                == expected_worktree
                for item in mounts
            )
            current_matches = (
                has_current_binding
                and all(labels.get(key) == value for key, value in expected.items())
                and labels.get("hermes-run-id") in valid_run_labels
            )
            legacy_matches = (
                is_legacy_binding
                and labels.get("hermes-agent") == "1"
                and labels.get("hermes-task-id") == _sanitize_label_value(task_id)
                and bool(valid_run_labels)
            )
            if not mount_matches or not (current_matches or legacy_matches):
                raise MissionError(
                    f"refusing to remove container with mismatched mission binding: {container_id}"
                )
        if container_ids:
            removed = subprocess.run(
                [docker, "rm", "-f", *container_ids], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=60, check=False,
            )
            if removed.returncode != 0:
                raise MissionError(
                    "could not stop mission executor containers: "
                    + (removed.stderr.strip() or "docker rm failed")
                )
        verify = subprocess.run(
            [docker, "ps", "-aq", *filters], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False,
        )
        if verify.returncode != 0 or verify.stdout.strip():
            raise MissionError("mission executor container survived revocation")


def _pause_running_executor(conn, mission_id: str, row) -> None:
    """Atomically close one active run and mark its card as controller-paused."""
    task_id = str(row["id"])
    run_id = row["current_run_id"]
    reason = "mission paused by operator"
    with kb.write_txn(conn):
        if run_id is None:
            cur = conn.execute(
                """UPDATE tasks
                      SET status='blocked',claim_lock=NULL,claim_expires=NULL,
                          worker_pid=NULL,block_kind=NULL,block_recurrences=0
                    WHERE id=? AND status='running'""",
                (task_id,),
            )
        else:
            cur = conn.execute(
                """UPDATE tasks
                      SET status='blocked',claim_lock=NULL,claim_expires=NULL,
                          worker_pid=NULL,block_kind=NULL,block_recurrences=0
                    WHERE id=? AND status='running' AND current_run_id=?""",
                (task_id, int(run_id)),
            )
        if cur.rowcount != 1:
            raise MissionError(f"could not pause executor task {task_id}")
        closed_run_id = kb._end_run(
            conn,
            task_id,
            outcome="paused",
            status="blocked",
            summary=reason,
        )
        kb._append_event(
            conn,
            task_id,
            "mission_paused",
            {"mission_id": mission_id, "reason": reason},
            run_id=closed_run_id,
        )
        if closed_run_id is not None:
            conn.execute(
                """UPDATE mission_intents SET status='paused',resolved_at=?
                     WHERE mission_id=? AND task_id=? AND run_id=?
                       AND status='open'""",
                (int(time.time()), mission_id, task_id, int(closed_run_id)),
            )


def _quiesce_mission_executors(
    conn,
    mission_id: str,
    *,
    resolve_outcome: Optional[str] = "cancelled",
    mark_paused: bool = False,
) -> None:
    """Stop claimed executors, close their runs, and resolve open intents."""
    rows = conn.execute(
        """SELECT t.id,t.status,t.worker_pid,t.claim_lock,t.current_run_id,
                  EXISTS(SELECT 1 FROM task_runs r WHERE r.task_id=t.id) AS was_run
             FROM tasks t
            WHERE t.mission_id=? AND t.mission_role='executor'
            ORDER BY t.id""",
        (mission_id,),
    ).fetchall()
    host_prefix = f"{kb._claimer_id().split(':', 1)[0]}:"
    remote = [
        row["id"] for row in rows
        if row["claim_lock"] and not str(row["claim_lock"]).startswith(host_prefix)
    ]
    if remote:
        raise MissionError(
            "cannot prove executor quiescence for non-local claims: " + ", ".join(remote)
        )
    for row in rows:
        if row["status"] != "running" and not row["claim_lock"] and not row["worker_pid"]:
            continue
        termination = kb._terminate_reclaimed_worker(row["worker_pid"], row["claim_lock"])
        if kb._worker_survived_termination(termination):
            raise MissionError(f"executor worker survived cancellation: {row['id']}")
    # Persistent task containers can survive after the host worker has marked
    # its card done. Enumerate every mission executor label, not just rows that
    # still look live in SQLite.
    _remove_mission_containers(
        conn, mission_id, [str(row["id"]) for row in rows if row["was_run"]],
    )
    for row in rows:
        current = kb.get_task(conn, row["id"])
        if current is not None and current.status == "running":
            if mark_paused:
                _pause_running_executor(conn, mission_id, row)
            else:
                if not kb.block_task(
                    conn, row["id"], reason="mission execution revoked by operator",
                    kind="needs_input", expected_run_id=row["current_run_id"],
                ):
                    raise MissionError(f"could not revoke executor task {row['id']}")
            if (
                not mark_paused
                and resolve_outcome is not None
                and row["current_run_id"] is not None
            ):
                # block_task closes the exact run atomically as `blocked`; the
                # enclosing lifecycle operation then refines that terminal
                # intent outcome to cancelled/rolled_back for audit clarity.
                with kb.write_txn(conn):
                    conn.execute(
                        """UPDATE mission_intents SET status=?
                             WHERE mission_id=? AND task_id=? AND run_id=?
                               AND status='blocked'""",
                        (
                            resolve_outcome, mission_id, row["id"],
                            int(row["current_run_id"]),
                        ),
                    )
    leftover = conn.execute(
        """SELECT id FROM tasks
            WHERE mission_id=? AND mission_role='executor'
              AND (status='running' OR claim_lock IS NOT NULL OR worker_pid IS NOT NULL)""",
        (mission_id,),
    ).fetchall()
    if leftover:
        raise MissionError("mission executors did not reach a quiescent state")
    if resolve_outcome is not None:
        for intent in list(mdb.open_intents(conn, mission_id)):
            mdb.resolve_task_intents(
                conn, mission_id, intent["task_id"], outcome=resolve_outcome,
                run_id=intent.get("run_id"), claim_token=intent.get("claim_token"),
            )


def _cancel_mission_unlocked(conn, mission_id: str) -> mdb.Mission:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status in mdb.TERMINAL_MISSION_STATUSES:
        raise MissionError(f"cannot cancel terminal mission {mission.status}")
    previous_status = mission.status
    # Revoke the live lease before signaling workers. If quiescence cannot be
    # proven, the mission remains safely paused instead of resuming mutation.
    if mission.status == "running":
        mission = mdb.transition_mission(
            conn, mission_id, "waiting_for_user", phase="cancelling",
            blocked_reason="cancellation in progress",
        )
    _quiesce_mission_executors(conn, mission_id)
    mdb.record_evidence(
        conn, mission_id, kind="lifecycle", status="passed",
        metadata={"action": "cancel", "previous_status": previous_status},
        cwd=mission.worktree_path,
    )
    return mdb.transition_mission(
        conn, mission_id, "cancelled", phase="cancelled",
        final_disposition="cancelled_by_operator",
    )


def cancel_mission(conn, mission_id: str) -> mdb.Mission:
    with _board_and_mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission cancellation is busy; retry on the next dispatcher tick")
        return _cancel_mission_unlocked(conn, mission_id)


def record_command_result(
    conn,
    mission_id: str,
    *,
    command: str,
    cwd: str,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
    tool_call_id: Optional[str] = None,
    started_at: Optional[int] = None,
    finished_at: Optional[int] = None,
) -> int:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    resolved_cwd = Path(cwd).resolve()
    if not mission.worktree_path or not resolved_cwd.is_relative_to(Path(mission.worktree_path).resolve()):
        raise MissionError("command cwd is outside the mission worktree")
    snapshot = _workspace_snapshot(mission)
    command_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
    safe_command = _redact(command)
    return store_evidence_blob(
        conn,
        mission,
        kind="command",
        status="passed" if exit_code == 0 else "failed",
        content=f"STDOUT\n{stdout}\n\nSTDERR\n{stderr}",
        metadata={
            "argv": _redact_value(shlex.split(command, posix=True) if command else []),
            "command_sha256": command_hash,
            "workspace_snapshot": snapshot,
        },
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        cwd=str(resolved_cwd),
        command=safe_command,
        exit_code=int(exit_code),
        started_at=started_at,
        finished_at=finished_at,
    )


def _base_tree_entries(mission: mdb.Mission) -> dict[str, dict[str, str]]:
    if not mission.worktree_path or not mission.base_commit:
        raise MissionError("mission worktree is not prepared")
    root = Path(mission.worktree_path)
    raw = _run_git_bytes(
        root, "ls-tree", "-r", "-z", "--full-tree", mission.base_commit,
    )
    entries: dict[str, dict[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split(" ", 2)
        entries[os.fsdecode(raw_path)] = {"mode": mode, "type": kind, "oid": oid}
    return entries


def _candidate_workspace_paths(mission: mdb.Mission) -> set[str]:
    """List base-tracked and non-ignored untracked paths via a trusted index."""
    assert mission.worktree_path and mission.base_commit
    root = Path(mission.worktree_path)
    with tempfile.TemporaryDirectory(prefix="hermes-mission-index-") as directory:
        index_path = str(Path(directory) / "index")
        env = {"GIT_INDEX_FILE": index_path}
        _run_git_bytes(root, "read-tree", mission.base_commit, extra_env=env)
        raw = _run_git_bytes(
            root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
            extra_env=env,
        )
    return {os.fsdecode(value) for value in raw.split(b"\0") if value}


def _current_workspace_entry(
    root: Path,
    rel: str,
    base: Optional[dict[str, str]],
) -> Optional[dict[str, Any]]:
    path = root / rel
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink():
        target = os.readlink(path)
        oid = _run_git_bytes(
            root, "hash-object", "--stdin", input_data=os.fsencode(target),
        ).decode("ascii").strip()
        return {"kind": "symlink", "mode": "120000", "oid": oid, "target": target}
    if path.is_file():
        mode = "100755" if stat_result.st_mode & 0o111 else "100644"
        oid = _run_git(root, "hash-object", "--no-filters", "--", rel)
        raw = path.read_bytes()
        return {
            "kind": "file", "mode": mode, "oid": oid, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if base and base.get("mode") == "160000" and path.is_dir():
        try:
            oid = _run_git(path, "rev-parse", "HEAD")
            dirty = bool(_run_git(path, "status", "--porcelain"))
        except MissionError:
            return {"kind": "unsupported", "mode": "160000", "oid": "", "unsupported": True}
        return {
            "kind": "gitlink", "mode": "160000", "oid": oid,
            "unsupported": dirty,
        }
    return {"kind": "unsupported", "mode": "", "oid": "", "unsupported": True}


def _workspace_snapshot(mission: mdb.Mission) -> dict[str, Any]:
    """Seal bytes, modes, links, deletions, and HEAD without trusting Git's index."""
    identity = _validate_worktree_identity(mission)
    assert mission.worktree_path and mission.base_commit
    root = Path(mission.worktree_path)
    base_entries = _base_tree_entries(mission)
    candidates = set(base_entries) | _candidate_workspace_paths(mission)
    files: list[dict[str, Any]] = []
    for rel in sorted(candidates, key=os.fsencode):
        base = base_entries.get(rel)
        current = _current_workspace_entry(root, rel, base)
        if current is None:
            if base is not None:
                files.append({
                    "path": rel, "kind": "deleted",
                    "base_mode": base["mode"], "base_oid": base["oid"],
                })
            continue
        unchanged = bool(
            base
            and not current.get("unsupported")
            and current.get("mode") == base.get("mode")
            and current.get("oid") == base.get("oid")
        )
        if unchanged:
            continue
        files.append(
            {"path": rel, **current}
            | ({"base_mode": base["mode"], "base_oid": base["oid"]} if base else {})
        )
    sealed_content = {"base_commit": mission.base_commit, "files": files}
    content_digest = hashlib.sha256(
        json.dumps(sealed_content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        **sealed_content,
        "head": identity["head"],
        "content_digest": content_digest,
    }
    payload["digest"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def _changed_paths(mission: mdb.Mission) -> list[str]:
    return [str(item["path"]) for item in _workspace_snapshot(mission)["files"]]


def _write_snapshot_tree(mission: mdb.Mission, snapshot: dict[str, Any]) -> str:
    """Materialize exactly the sealed filesystem state into a temporary index."""
    assert mission.worktree_path and mission.base_commit
    root = Path(mission.worktree_path)
    with tempfile.TemporaryDirectory(prefix="hermes-mission-commit-") as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        _run_git_bytes(root, "read-tree", mission.base_commit, extra_env=env)
        for item in snapshot.get("files", []):
            rel = str(item["path"])
            if item.get("kind") == "deleted":
                _run_git_bytes(
                    root, "update-index", "--force-remove", "--", rel,
                    extra_env=env,
                )
                continue
            if item.get("unsupported") or item.get("kind") not in {"file", "symlink", "gitlink"}:
                raise MissionError(f"unsupported filesystem entry in mission seal: {rel}")
            if item["kind"] == "file":
                oid = _run_git(
                    root, "hash-object", "-w", "--no-filters", "--", rel,
                )
            elif item["kind"] == "symlink":
                oid = _run_git_bytes(
                    root, "hash-object", "-w", "--stdin",
                    input_data=os.fsencode(os.readlink(root / rel)),
                ).decode("ascii").strip()
            else:
                oid = str(item["oid"])
            if oid != item.get("oid"):
                raise MissionError(f"workspace entry changed while committing: {rel}")
            _run_git_bytes(
                root, "update-index", "--add", "--cacheinfo",
                str(item["mode"]), oid, rel, extra_env=env,
            )
        return _run_git_bytes(root, "write-tree", extra_env=env).decode("ascii").strip()


def _latest_verification_seal(conn, mission_id: str) -> Optional[dict[str, Any]]:
    for row in reversed(mdb.list_evidence(conn, mission_id)):
        if row["kind"] == "verification_seal" and row["status"] == "passed":
            value = row["metadata"].get("workspace_snapshot")
            return value if isinstance(value, dict) else None
    return None


def _require_no_open_intents(conn, mission_id: str, *, action: str) -> None:
    intents = mdb.open_intents(conn, mission_id)
    if intents:
        raise MissionError(
            f"cannot {action}: mission has {len(intents)} unresolved mutation intent(s)"
        )


def deterministic_verification(
    conn,
    mission_id: str,
    *,
    require_base_head: bool = True,
) -> dict[str, Any]:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    validate_mission_task_graph(conn, mission_id, boundary="verification")
    evidence = mdb.list_evidence(conn, mission_id)
    chain_ok = mdb.verify_evidence_chain(conn, mission_id)
    current_snapshot = _workspace_snapshot(mission)
    changed = [str(item["path"]) for item in current_snapshot["files"]]
    allowed_paths = [str(v).strip().strip("/") for v in mission.contract.boundaries.get("allowed_paths", [])]
    violations = []
    if allowed_paths:
        violations = [
            path for path in changed
            if not any(path == allowed or path.startswith(allowed + "/") for allowed in allowed_paths)
        ]
    current_content_digest = current_snapshot["content_digest"]
    passed_command_hashes = {
        row["metadata"].get("command_sha256")
        or (hashlib.sha256(str(row["command"]).encode("utf-8")).hexdigest() if row["command"] else None)
        for row in evidence
        if row["kind"] == "command" and row["status"] == "passed" and row["exit_code"] == 0
        and (
            row["metadata"].get("workspace_snapshot", {}).get("content_digest")
            or row["metadata"].get("workspace_snapshot", {}).get("digest")
        ) == current_content_digest
    }
    missing = [
        criterion for criterion in mission.contract.verification
        if hashlib.sha256(criterion.encode("utf-8")).hexdigest() not in passed_command_hashes
    ]
    open_intents = mdb.open_intents(conn, mission_id)
    head_unchanged = current_snapshot["head"] == mission.base_commit
    unsupported = [
        str(item["path"]) for item in current_snapshot["files"] if item.get("unsupported")
    ]
    report = {
        "pass": bool(
            chain_ok and not violations and not missing and bool(changed)
            and not open_intents and not unsupported
            and (head_unchanged or not require_base_head)
        ),
        "evidence_chain_valid": chain_ok,
        "changed_paths": changed,
        "scope_violations": violations,
        "missing_verification": missing,
        "open_intent_count": len(open_intents),
        "unsupported_paths": unsupported,
        "head_unchanged": head_unchanged,
        "workspace_snapshot": current_snapshot,
    }
    mdb.record_evidence(
        conn, mission_id, kind="deterministic_verifier",
        status="passed" if report["pass"] else "failed", metadata=report,
        cwd=mission.worktree_path,
    )
    return report


def _submit_verifier_verdict_unlocked(
    conn,
    mission_id: str,
    verdict: dict[str, Any],
) -> mdb.Mission:
    """Accept a strict independent-verifier result and advance fail-closed."""
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status not in {"running", "verifying"}:
        raise MissionError(f"verdict requires verifying state, not {mission.status}")
    validate_mission_task_graph(conn, mission_id, boundary="verification")
    required = {"verdict", "requirements_checked", "requirements_failed", "evidence_missing", "recommended_action"}
    coherent = bool(
        isinstance(verdict, dict)
        and set(verdict) == required
        and verdict.get("verdict") in {"pass", "fail", "blocked"}
        and isinstance(verdict.get("requirements_checked"), int)
        and not isinstance(verdict.get("requirements_checked"), bool)
        and verdict.get("requirements_checked", -1) >= 0
        and isinstance(verdict.get("requirements_failed"), list)
        and all(isinstance(item, str) for item in verdict.get("requirements_failed", []))
        and isinstance(verdict.get("evidence_missing"), list)
        and all(isinstance(item, str) for item in verdict.get("evidence_missing", []))
        and verdict.get("recommended_action") in {"commit", "replan"}
    )
    if not coherent:
        raise MissionError("malformed verifier verdict")
    executor_states = [
        row[0] for row in conn.execute(
            "SELECT status FROM tasks WHERE mission_id=? AND mission_role='executor'",
            (mission_id,),
        )
    ]
    if not executor_states or any(state != "done" for state in executor_states):
        raise MissionError("all executor tasks must be done before a verifier verdict")
    _quiesce_mission_executors(conn, mission_id, resolve_outcome=None)
    _require_no_open_intents(conn, mission_id, action="accept a verifier verdict")
    if mission.status == "running":
        mission = mdb.transition_mission(conn, mission_id, "verifying", phase="verification")
    deterministic = deterministic_verification(conn, mission_id)
    semantic_pass = bool(
        verdict["verdict"] == "pass"
        and verdict["recommended_action"] == "commit"
        and not verdict["requirements_failed"]
        and not verdict["evidence_missing"]
        and verdict["requirements_checked"] > 0
    )
    passed = semantic_pass and deterministic["pass"]
    mdb.record_evidence(
        conn, mission_id, kind="independent_verifier",
        status="passed" if passed else "failed", metadata=verdict,
        cwd=mission.worktree_path,
    )
    if not passed:
        return mdb.transition_mission(
            conn, mission_id, "blocked", phase="verification",
            blocked_reason="verification failed or evidence is incomplete",
        )
    mdb.record_evidence(
        conn, mission_id, kind="verification_seal", status="passed",
        metadata={"workspace_snapshot": _workspace_snapshot(mission)},
        cwd=mission.worktree_path,
    )
    if mission.autonomy_level == 3:
        return mdb.transition_mission(conn, mission_id, "awaiting_approval", phase="commit")
    if mission.autonomy_level == 4 and mission.contract.allow_local_commit:
        return mdb.transition_mission(conn, mission_id, "committing", phase="commit")
    if mission.autonomy_level == 4:
        return mdb.transition_mission(
            conn, mission_id, "blocked", phase="commit",
            blocked_reason="autonomy level 4 lacks explicit local-commit authority",
        )
    return mdb.transition_mission(
        conn, mission_id, "blocked", phase="commit",
        blocked_reason=f"autonomy level {mission.autonomy_level} does not permit local commits",
    )


def submit_verifier_verdict(
    conn,
    mission_id: str,
    verdict: dict[str, Any],
) -> mdb.Mission:
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("another verifier or control action is already running")
        return _submit_verifier_verdict_unlocked(conn, mission_id, verdict)


def _verify_with_model_unlocked(conn, mission_id: str) -> mdb.Mission:
    """Run the independent verifier with no executor rationale in context."""
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    validate_mission_task_graph(conn, mission_id, boundary="verification")
    executor_states = [
        row[0] for row in conn.execute(
            "SELECT status FROM tasks WHERE mission_id=? AND mission_role='executor'",
            (mission_id,),
        )
    ]
    if not executor_states or any(state != "done" for state in executor_states):
        raise MissionError("all executor tasks must be done before independent verification")
    _quiesce_mission_executors(conn, mission_id, resolve_outcome=None)
    _require_no_open_intents(conn, mission_id, action="verify")
    if mission.status == "running":
        mission = mdb.transition_mission(conn, mission_id, "verifying", phase="verification")
    deterministic = deterministic_verification(conn, mission_id)
    diff = _run_git(Path(mission.worktree_path), "diff", "--no-ext-diff", mission.base_commit, "--")
    evidence = [
        {
            "id": row["id"], "kind": row["kind"], "status": row["status"],
            "command": row["command"], "exit_code": row["exit_code"],
            "metadata": row["metadata"], "blob_sha256": row["blob_sha256"],
        }
        for row in mdb.list_evidence(conn, mission_id)
    ]
    from agent.auxiliary_client import call_llm

    system = (
        "You are an independent verifier. You did not execute this work. Check the "
        "contract, diff, and evidence strictly. Reply with one JSON object containing "
        "exactly: verdict ('pass'|'fail'|'blocked'), requirements_checked (integer), "
        "requirements_failed (array), evidence_missing (array), recommended_action "
        "('commit' or 'replan'). Never accept executor claims without evidence."
    )
    payload = {
        "contract": asdict(mission.contract),
        "deterministic_checks": deterministic,
        "diff": diff,
        "evidence": evidence,
    }
    response = call_llm(
        task="mission_verifier",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0,
        max_tokens=2000,
        timeout=120,
    )
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MissionError("verifier returned malformed JSON; mission remains unverified") from exc
    return _submit_verifier_verdict_unlocked(conn, mission_id, verdict)


def verify_with_model(conn, mission_id: str) -> mdb.Mission:
    """Run one serialized independent-verifier action."""
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("another verifier or control action is already running")
        return _verify_with_model_unlocked(conn, mission_id)


def approve_commit(conn, mission_id: str) -> mdb.Mission:
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("another verifier or control action is already running")
        mission = mdb.get_mission(conn, mission_id)
        if mission is None:
            raise KeyError(mission_id)
        if mission.status != "awaiting_approval" or mission.autonomy_level != 3:
            raise MissionError("mission is not awaiting a supervised local commit")
        _require_no_open_intents(conn, mission_id, action="approve commit")
        return mdb.transition_mission(conn, mission_id, "committing", phase="commit")


def deny_mission(conn, mission_id: str) -> mdb.Mission:
    """Reject a supervised commit without discarding the isolated worktree."""
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("another verifier or control action is already running")
        mission = mdb.get_mission(conn, mission_id)
        if mission is None:
            raise KeyError(mission_id)
        if mission.status != "awaiting_approval":
            raise MissionError("mission is not awaiting commit approval")
        _require_no_open_intents(conn, mission_id, action="deny commit")
        mdb.record_evidence(
            conn, mission_id, kind="approval", status="failed",
            metadata={"action": "deny", "previous_status": mission.status},
            cwd=mission.worktree_path,
        )
        return mdb.transition_mission(
            conn, mission_id, "blocked", phase="commit",
            blocked_reason="local commit denied by operator",
        )


def _requeue_blocked_execution_tasks(conn, mission_id: str) -> list[str]:
    """Requeue executor cards as part of an explicit mission retry.

    Generic Kanban clients cannot unblock mission-linked cards because those
    cards materialize the accepted execution contract. The mission controller
    owns this narrow recovery path. ``kanban_db.unblock_task`` preserves the
    block recurrence counter, so an unchanged failure still escalates to
    triage instead of creating an infinite retry loop.
    """
    rows = conn.execute(
        """SELECT id,status FROM tasks
             WHERE mission_id=? AND mission_role='executor'
               AND status IN ('blocked','triage')
             ORDER BY id""",
        (mission_id,),
    ).fetchall()
    triage = [str(row["id"]) for row in rows if row["status"] == "triage"]
    if triage:
        raise MissionError(
            "executor task requires explicit triage before retry: " + ", ".join(triage)
        )
    blocked = [str(row["id"]) for row in rows if row["status"] == "blocked"]
    if not blocked:
        return []
    with kb._trusted_mission_controller_mutation():
        for task_id in blocked:
            if not kb.unblock_task(conn, task_id):
                raise MissionError(f"could not requeue blocked executor task {task_id}")
    return blocked


def retry_mission(conn, mission_id: str) -> mdb.Mission:
    """Restart a safely blocked mission from its last bounded phase."""
    with _board_and_mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission retry is busy; retry on the next dispatcher tick")
        mission = reconcile_mission(conn, mission_id)
        if mission.status != "blocked":
            raise MissionError(f"only a blocked mission can be retried, not {mission.status}")
        if mission.deadline is not None and int(time.time()) > int(mission.deadline):
            raise MissionError("mission deadline is still exceeded")
        if mission.blocked_reason and "recovery_ambiguous" in mission.blocked_reason:
            raise MissionError("ambiguous recovery must be resolved or rolled back before retry")
        _require_no_open_intents(conn, mission_id, action="retry")
        if mission.phase == "environment":
            return _prepare_mission_unlocked(conn, mission_id)
        if mission.worktree_path:
            _validate_worktree_identity(mission)
        target = "planning" if mission.phase == "planning" else "running"
        retried_tasks: list[str] = []
        if target == "running":
            validate_mission_task_graph(conn, mission_id, boundary="retry")
            if mission.phase == "execution":
                retried_tasks = _requeue_blocked_execution_tasks(conn, mission_id)
        mdb.record_evidence(
            conn, mission_id, kind="lifecycle", status="passed",
            metadata={
                "action": "retry", "previous_phase": mission.phase,
                "previous_blocker": mission.blocked_reason, "target": target,
                "retried_tasks": retried_tasks,
            },
            cwd=mission.worktree_path,
        )
        return mdb.transition_mission(
            conn, mission_id, target,
            phase=("planning" if target == "planning" else "execution"),
        )


def _commit_mission_unlocked(conn, mission_id: str) -> mdb.Mission:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status != "committing" or not mission.worktree_path:
        raise MissionError("mission has not passed the commit gate")
    _quiesce_mission_executors(conn, mission_id, resolve_outcome=None)
    _require_no_open_intents(conn, mission_id, action="commit")
    identity = _validate_worktree_identity(mission)
    seal = _latest_verification_seal(conn, mission_id)
    if (
        seal is None
        or seal.get("head") != mission.base_commit
        or not seal.get("content_digest")
    ):
        return mdb.transition_mission(
            conn, mission_id, "blocked", phase="commit",
            blocked_reason="independent verification seal is missing or invalid",
        )
    worktree = Path(mission.worktree_path)
    marker = f"mission({mission.id}):"
    objective_subject = mission.objective.splitlines()[0].strip()[:60]
    expected_subject = f"{marker} {objective_subject}"
    head_before = identity["head"]
    if head_before != mission.base_commit:
        # Recovery seam: Git may have durably created our commit before the
        # process could persist verified_commit / evidence / final status.
        # Adoption is allowed only when the independently sealed base state is
        # exactly the tree carried by one controller-shaped child commit.
        subject = _run_git(worktree, "log", "-1", "--format=%s")
        commit_count = _run_git(worktree, "rev-list", "--count", f"{mission.base_commit}..HEAD")
        parent = _run_git(worktree, "rev-parse", "HEAD^") if commit_count == "1" else ""
        current_snapshot = _workspace_snapshot(mission)
        expected_tree = _write_snapshot_tree(mission, current_snapshot)
        actual_tree = _run_git(worktree, "rev-parse", "HEAD^{tree}")
        if (
            subject != expected_subject
            or commit_count != "1"
            or parent != mission.base_commit
            or seal.get("content_digest") != current_snapshot.get("content_digest")
            or actual_tree != expected_tree
        ):
            return mdb.transition_mission(
                conn, mission_id, "blocked", phase="commit",
                blocked_reason="worktree HEAD advanced outside the mission commit gate",
            )
        report = deterministic_verification(
            conn, mission_id, require_base_head=False,
        )
        if not report["pass"]:
            return mdb.transition_mission(
                conn, mission_id, "blocked", phase="commit",
                blocked_reason="pre-commit verification became stale",
            )
        commit = head_before
        _run_git(worktree, "-c", "core.hooksPath=/dev/null", "reset", "--mixed", commit)
    else:
        # Re-run the deterministic half immediately before committing so
        # evidence cannot become stale between verifier approval and mutation.
        report = deterministic_verification(conn, mission_id)
        if not report["pass"]:
            return mdb.transition_mission(
                conn, mission_id, "blocked", phase="commit",
                blocked_reason="pre-commit verification became stale",
            )
        current_snapshot = report["workspace_snapshot"]
        if (
            seal.get("digest") != current_snapshot.get("digest")
            or seal.get("content_digest") != current_snapshot.get("content_digest")
        ):
            return mdb.transition_mission(
                conn, mission_id, "blocked", phase="commit",
                blocked_reason="worktree changed after independent verification",
            )
        tree = _write_snapshot_tree(mission, current_snapshot)
        # Re-seal after object creation and re-prove Git identity immediately
        # before the branch CAS. No worker container remains alive here.
        final_snapshot = _workspace_snapshot(mission)
        _validate_worktree_identity(mission, expected_head=mission.base_commit)
        if final_snapshot.get("digest") != current_snapshot.get("digest"):
            return mdb.transition_mission(
                conn, mission_id, "blocked", phase="commit",
                blocked_reason="worktree changed while creating the commit tree",
            )
        commit = _run_git(
            worktree, "-c", "core.hooksPath=/dev/null", "commit-tree", tree,
            "-p", mission.base_commit, "-m", expected_subject,
        )
        _run_git(
            worktree, "-c", "core.hooksPath=/dev/null", "update-ref",
            f"refs/heads/{mission.branch_name}",
            commit, mission.base_commit,
        )
        # Align the worktree's mutable index with the controller-created tree.
        # This invokes neither commit hooks nor repository-supplied hooks.
        _run_git(worktree, "-c", "core.hooksPath=/dev/null", "reset", "--mixed", commit)
        if _run_git(worktree, "rev-parse", f"{commit}^{{tree}}") != tree:
            raise MissionError("controller-created commit tree does not match the mission seal")
    if _run_git(worktree, "status", "--porcelain"):
        raise MissionError("mission worktree is not clean after commit")
    _validate_worktree_identity(mission, expected_head=commit, require_clean=True)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE missions SET verified_commit=?, updated_at=? WHERE id=?",
            (commit, int(time.time()), mission_id),
        )
    existing_commit_evidence = any(
        row["kind"] == "commit" and row["metadata"].get("commit") == commit
        for row in mdb.list_evidence(conn, mission_id)
    )
    if not existing_commit_evidence:
        mdb.record_evidence(
            conn, mission_id, kind="commit", status="passed",
            metadata={
                "commit": commit,
                "tree": _run_git(worktree, "rev-parse", f"{commit}^{{tree}}"),
                "rollback_ref": mission.rollback_ref,
            },
            cwd=str(worktree),
        )
    return mdb.transition_mission(
        conn, mission_id, "succeeded", phase="complete",
        final_disposition="verified_local_commit",
    )


def commit_mission(conn, mission_id: str) -> mdb.Mission:
    """Perform an explicitly requested, serialized local commit."""
    with _mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("another verifier or control action is already running")
        return _commit_mission_unlocked(conn, mission_id)


def _rollback_mission_unlocked(conn, mission_id: str) -> mdb.Mission:
    """Restore the isolated mission worktree to its immutable rollback ref."""
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status == "rolled_back":
        return mission
    if mission.status not in {"succeeded", "failed", "cancelled", "blocked"}:
        raise MissionError(f"cannot roll back mission from {mission.status}")
    if not mission.worktree_path or not mission.rollback_ref:
        raise MissionError("mission has no prepared rollback point")
    # A blocked/cancelled mission can still carry an executor claim from an
    # interrupted control action. Never reset files until that owner is gone.
    _quiesce_mission_executors(conn, mission_id, resolve_outcome=None)
    worktree = Path(mission.worktree_path).resolve()
    _validate_worktree_identity(mission)
    target = _run_git(worktree, "rev-parse", mission.rollback_ref)
    if target != mission.base_commit:
        raise MissionError("rollback ref was moved after mission preparation")
    before = _run_git(worktree, "rev-parse", "HEAD")
    _run_git(worktree, "-c", "core.hooksPath=/dev/null", "reset", "--hard", target)
    # The worktree belongs exclusively to this mission, so removing its
    # untracked outputs is bounded and required for a complete rollback.
    _run_git(worktree, "-c", "core.hooksPath=/dev/null", "clean", "-fdx")
    after = _run_git(worktree, "rev-parse", "HEAD")
    if after != target or _run_git(worktree, "status", "--porcelain"):
        raise MissionError("rollback verification failed")
    _validate_worktree_identity(mission, expected_head=target, require_clean=True)
    if not any(
        row["kind"] == "rollback"
        and row["status"] == "passed"
        and row["metadata"].get("after") == after
        for row in mdb.list_evidence(conn, mission_id)
    ):
        mdb.record_evidence(
            conn, mission_id, kind="rollback", status="passed",
            metadata={"before": before, "after": after, "rollback_ref": mission.rollback_ref},
            cwd=str(worktree),
        )
    for intent in list(mdb.open_intents(conn, mission_id)):
        mdb.resolve_task_intents(
            conn, mission_id, intent["task_id"], outcome="rolled_back",
            run_id=intent.get("run_id"), claim_token=intent.get("claim_token"),
        )
    return mdb.transition_mission(
        conn, mission_id, "rolled_back", phase="complete",
        final_disposition="restored_to_rollback_ref",
    )


def rollback_mission(conn, mission_id: str) -> mdb.Mission:
    """Perform an explicitly requested, serialized rollback."""
    with _board_and_mission_action_lock(conn, mission_id) as held:
        if not held:
            raise MissionError("mission rollback is busy; retry on the next dispatcher tick")
        return _rollback_mission_unlocked(conn, mission_id)


def mission_report(conn, mission_id: str) -> dict[str, Any]:
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    tasks = [
        dict(row) for row in conn.execute(
            "SELECT id,title,status,mission_role,current_run_id FROM tasks WHERE mission_id=? ORDER BY created_at,id",
            (mission_id,),
        )
    ]
    links = [
        dict(row) for row in conn.execute(
            """SELECT l.parent_id, l.child_id
                 FROM task_links l
                 JOIN tasks parent ON parent.id = l.parent_id
                 JOIN tasks child ON child.id = l.child_id
                WHERE parent.mission_id=? AND child.mission_id=?
                ORDER BY parent.created_at, parent.id, child.created_at, child.id""",
            (mission_id, mission_id),
        )
    ]
    return {
        "mission": asdict(mission) | {"contract": asdict(mission.contract)},
        "tasks": tasks,
        "links": links,
        "evidence": mdb.list_evidence(conn, mission_id),
        "evidence_chain_valid": mdb.verify_evidence_chain(conn, mission_id),
        "open_intents": mdb.open_intents(conn, mission_id),
    }


def _current_boot_id() -> Optional[str]:
    """Best-effort host boot identity used to reject pre-reboot PID reuse."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _ambiguous_open_intents(conn, mission_id: str) -> list[dict[str, Any]]:
    """Return mutation intents that cannot be proven to have a live owner."""
    ambiguous: list[dict[str, Any]] = []
    now = int(time.time())
    for intent in mdb.open_intents(conn, mission_id):
        row = conn.execute(
            """SELECT status, worker_pid, claim_lock, current_run_id,
                      last_heartbeat_at FROM tasks WHERE id=?""",
            (intent.get("task_id"),),
        ).fetchone()
        host_prefix = f"{kb._claimer_id().split(':', 1)[0]}:"
        heartbeat_fresh = bool(
            row
            and (
                row["last_heartbeat_at"] is None
                or now - int(row["last_heartbeat_at"])
                <= kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
            )
        )
        try:
            details = json.loads(intent.get("details_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        intent_boot = details.get("boot_id") if isinstance(details, dict) else None
        current_boot = _current_boot_id()
        try:
            run_matches = bool(
                row
                and intent.get("run_id") is not None
                and row["current_run_id"] is not None
                and int(row["current_run_id"]) == int(intent["run_id"])
            )
        except (TypeError, ValueError):
            run_matches = False
        claim_matches = bool(
            row
            and intent.get("claim_token")
            and row["claim_lock"]
            and str(row["claim_lock"]) == str(intent["claim_token"])
        )
        boot_matches = not intent_boot or not current_boot or intent_boot == current_boot
        healthy = bool(
            row
            and row["status"] == "running"
            and row["worker_pid"]
            and str(row["claim_lock"] or "").startswith(host_prefix)
            and run_matches
            and claim_matches
            and boot_matches
            and heartbeat_fresh
            and kb._pid_alive(int(row["worker_pid"]))
        )
        if not healthy:
            ambiguous.append(intent)
    return ambiguous


def _mission_needs_quiescence(conn, mission_id: str) -> bool:
    """Return whether a non-running mission still owns executable state."""
    live = conn.execute(
        """SELECT 1 FROM tasks
            WHERE mission_id=? AND mission_role='executor'
              AND (status='running' OR claim_lock IS NOT NULL
                   OR worker_pid IS NOT NULL OR current_run_id IS NOT NULL)
            LIMIT 1""",
        (mission_id,),
    ).fetchone()
    return live is not None or bool(mdb.open_intents(conn, mission_id))


def _mission_run_generation(conn, mission_id: str) -> int:
    row = conn.execute(
        """SELECT COALESCE(MAX(r.id),0)
             FROM task_runs r JOIN tasks t ON t.id=r.task_id
            WHERE t.mission_id=? AND t.mission_role='executor'""",
        (mission_id,),
    ).fetchone()
    return int(row[0] or 0)


def _quiescence_binding(conn, mission: mdb.Mission) -> dict[str, Any]:
    """Describe the exact blocked execution generation a proof covers."""
    return {
        "mission_status": mission.status,
        "phase": mission.phase,
        "blocked_reason": mission.blocked_reason,
        "mission_updated_at": mission.updated_at,
        "max_executor_run_id": _mission_run_generation(conn, mission.id),
    }


def _blocked_quiescence_proven(conn, mission: mdb.Mission) -> bool:
    binding = _quiescence_binding(conn, mission)
    for row in reversed(mdb.list_evidence(conn, mission.id)):
        if row["kind"] == "quiescence" and row["status"] == "passed":
            return row["metadata"] == binding
    return False


def _quiesce_blocked_mission(conn, mission: mdb.Mission) -> None:
    """Revoke a blocked mission once and persist a generation-bound proof."""
    _quiesce_mission_executors(
        conn, mission.id, resolve_outcome="blocked",
    )
    refreshed = mdb.get_mission(conn, mission.id)
    if refreshed is None or refreshed.status != "blocked":
        raise MissionError("mission left blocked state during executor quiescence")
    binding = _quiescence_binding(conn, refreshed)
    if not _blocked_quiescence_proven(conn, refreshed):
        mdb.record_evidence(
            conn,
            mission.id,
            kind="quiescence",
            status="passed",
            metadata=binding,
            cwd=refreshed.worktree_path,
        )


def reconcile_active_missions(conn) -> list[str]:
    """Block ambiguous mutations before the dispatcher can reclaim/spawn.

    A live worker's open intent is expected steady state. An intent whose task
    is no longer backed by a live, unexpired worker is ambiguous after a crash
    or reboot and must stop the mission before Kanban makes the task ready.
    """
    mdb.ensure_schema(conn)
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id FROM missions WHERE status IN "
        "('running','waiting_for_user','blocked','verifying','awaiting_approval','committing') "
        "ORDER BY updated_at, id"
    ).fetchall()
    for row in rows:
        mission = mdb.get_mission(conn, row["id"])
        if mission is None:
            continue
        if mission.status == "waiting_for_user" and mission.phase == "pausing":
            try:
                _finish_mission_pause_unlocked(conn, mission.id)
            except Exception:
                # The waiting state already revoked new tool leases. Leave the
                # durable pausing marker intact so the next dispatcher tick or
                # explicit resume retries host/container quiescence.
                continue
            reconciled.append(mission.id)
            continue
        if mission.status == "waiting_for_user" and mission.phase == "cancelling":
            try:
                _cancel_mission_unlocked(conn, mission.id)
            except Exception:
                # Cancellation already revoked the durable lease. Preserve
                # the phase so cleanup and the terminal transition retry.
                continue
            reconciled.append(mission.id)
            continue
        if mission.status == "blocked":
            if (
                not _mission_needs_quiescence(conn, mission.id)
                and _blocked_quiescence_proven(conn, mission)
            ):
                continue
            try:
                _quiesce_blocked_mission(conn, mission)
            except Exception:
                # Fail closed: blocked status denies new mission tool leases;
                # retain executable-state markers so reconciliation retries.
                continue
            reconciled.append(mission.id)
            continue
        ambiguous = _ambiguous_open_intents(conn, mission.id)
        if not ambiguous:
            continue
        mdb.record_evidence(
            conn, mission.id, kind="recovery", status="failed",
            metadata={
                "reason": "unresolved mutation intent has no live owner",
                "intent_ids": [item["id"] for item in ambiguous],
            },
            cwd=mission.worktree_path,
        )
        mdb.transition_mission(
            conn, mission.id, "blocked", phase="recovery",
            blocked_reason="recovery_ambiguous: unresolved mutation intent",
        )
        blocked = mdb.get_mission(conn, mission.id)
        assert blocked is not None
        try:
            _quiesce_blocked_mission(conn, blocked)
        except Exception:
            # The mission remains durably blocked and will be retried by the
            # blocked branch on the next dispatcher tick, even if crash
            # detection clears the task's live DB fields in the meantime.
            pass
        reconciled.append(mission.id)
    return reconciled


def _settle_verifier_task(conn, mission_id: str, *, passed: bool, reason: str) -> None:
    rows = conn.execute(
        "SELECT id FROM tasks WHERE mission_id=? AND mission_role='verifier' "
        "ORDER BY created_at,id",
        (mission_id,),
    ).fetchall()
    if len(rows) != 1:
        raise MissionError(
            f"mission requires exactly one verifier task, found {len(rows)}"
        )
    task_id = rows[0]["id"]
    task = kb.get_task(conn, task_id)
    if task is None or task.status == "done":
        return
    if task.status not in {"ready", "running"}:
        with kb._trusted_mission_controller_mutation():
            promoted, _ = kb.promote_task(
                conn, task_id, actor="mission-controller",
                reason="logical verifier is ready", force=True,
            )
        if not promoted:
            return
    if passed:
        kb.complete_task(
            conn, task_id, result=reason,
            summary="Independent mission verifier completed.",
        )
    else:
        kb.block_task(conn, task_id, reason=reason, kind="needs_input")


def _controller_error_count(conn, mission_id: str, phase: str) -> int:
    count = 0
    for row in mdb.list_evidence(conn, mission_id):
        if row["kind"] == "controller_error" and row["metadata"].get("phase") == phase:
            count += 1
    return count


def controller_tick(conn, *, max_advances: int = 1) -> list[dict[str, str]]:
    """Advance mission state under a per-mission control-plane lock.

    The tick is deliberately serialized and bounded to one expensive advance
    by default. It never prepares or starts a mission—that remains the explicit
    autonomy gate. Level 4 closes the loop through independent verification
    and an authorized local commit; Level 3 always requires separate explicit
    approval and commit actions.
    """
    mdb.ensure_schema(conn)
    actions: list[dict[str, str]] = []
    rows = conn.execute(
        "SELECT id FROM missions WHERE status IN ('running','verifying','committing') "
        "ORDER BY CASE status WHEN 'committing' THEN 0 WHEN 'verifying' THEN 1 ELSE 2 END, "
        "updated_at, id"
    ).fetchall()
    try:
        limit = max(1, int(max_advances))
    except (TypeError, ValueError):
        limit = 1
    expensive = 0
    for row in rows:
        if expensive >= limit:
            break
        with _mission_action_lock(conn, row["id"]) as held:
            if not held:
                actions.append({"mission_id": row["id"], "action": "busy"})
                continue
            # Re-read after the lock: a CLI/RPC action may have advanced this
            # record since the candidate query above.
            mission = mdb.get_mission(conn, row["id"])
            if mission is None or mission.status not in {"running", "verifying", "committing"}:
                continue
            if mission.deadline is not None and int(time.time()) > int(mission.deadline):
                mdb.transition_mission(
                    conn, mission.id, "blocked", phase="budget",
                    blocked_reason="mission deadline exceeded",
                )
                actions.append({"mission_id": mission.id, "action": "deadline_blocked"})
                continue
            if mission.status == "running":
                executor_rows = conn.execute(
                    "SELECT id,status FROM tasks WHERE mission_id=? AND mission_role='executor'",
                    (mission.id,),
                ).fetchall()
                if not executor_rows:
                    continue
                blocked = [
                    item["id"] for item in executor_rows
                    if item["status"] in {"blocked", "triage"}
                ]
                if blocked:
                    mdb.transition_mission(
                        conn, mission.id, "blocked", phase="execution",
                        blocked_reason=f"executor task requires attention: {', '.join(blocked)}",
                    )
                    actions.append({"mission_id": mission.id, "action": "executor_blocked"})
                    continue
                if any(item["status"] != "done" for item in executor_rows):
                    continue
            if mission.status in {"running", "verifying"}:
                expensive += 1
                try:
                    advanced = _verify_with_model_unlocked(conn, mission.id)
                    passed = advanced.status in {"awaiting_approval", "committing", "succeeded"}
                    _settle_verifier_task(
                        conn, mission.id, passed=passed,
                        reason=("Acceptance criteria passed." if passed else
                                advanced.blocked_reason or "Verification failed."),
                    )
                    actions.append({"mission_id": mission.id, "action": advanced.status})
                    mission = advanced
                except Exception as exc:
                    current = mdb.get_mission(conn, mission.id)
                    mdb.record_evidence(
                        conn, mission.id, kind="controller_error", status="failed",
                        metadata={"phase": "verification", "error": str(exc)},
                        cwd=mission.worktree_path,
                    )
                    retries = _controller_error_count(conn, mission.id, "verification")
                    try:
                        maximum = max(0, int(mission.budget.get("max_verifier_retries", 2)))
                    except (TypeError, ValueError):
                        maximum = 2
                    if current and current.status == "verifying" and retries > maximum:
                        mdb.transition_mission(
                            conn, mission.id, "blocked", phase="verification",
                            blocked_reason=(
                                f"automatic verifier failed after {retries} attempts: {exc}"
                            ),
                        )
                        _settle_verifier_task(
                            conn, mission.id, passed=False,
                            reason="Automatic verifier exhausted its retry budget.",
                        )
                        actions.append({
                            "mission_id": mission.id, "action": "verification_blocked",
                        })
                    else:
                        actions.append({
                            "mission_id": mission.id, "action": "verification_retry",
                        })
                    continue
            if mission.status == "committing" and mission.autonomy_level == 4:
                if expensive == 0:
                    expensive += 1
                try:
                    # Recovery seam: verification may have persisted the
                    # committing transition before its logical verifier card.
                    _settle_verifier_task(
                        conn, mission.id, passed=True,
                        reason="Acceptance criteria passed.",
                    )
                    completed = _commit_mission_unlocked(conn, mission.id)
                    actions.append({"mission_id": mission.id, "action": completed.status})
                except Exception as exc:
                    current = mdb.get_mission(conn, mission.id)
                    mdb.record_evidence(
                        conn, mission.id, kind="controller_error", status="failed",
                        metadata={"phase": "commit", "error": str(exc)},
                        cwd=mission.worktree_path,
                    )
                    retries = _controller_error_count(conn, mission.id, "commit")
                    try:
                        maximum = max(0, int(mission.budget.get("max_commit_retries", 2)))
                    except (TypeError, ValueError):
                        maximum = 2
                    if current and current.status == "committing" and retries > maximum:
                        mdb.transition_mission(
                            conn, mission.id, "blocked", phase="commit",
                            blocked_reason=(
                                f"automatic local commit failed after {retries} attempts: {exc}"
                            ),
                        )
                        actions.append({
                            "mission_id": mission.id, "action": "commit_blocked",
                        })
                    else:
                        actions.append({
                            "mission_id": mission.id, "action": "commit_retry",
                        })
    return actions


def reconcile_mission(conn, mission_id: str) -> mdb.Mission:
    """Fail closed when restart recovery finds an unresolved mutation."""
    mission = mdb.get_mission(conn, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    if mission.status in mdb.TERMINAL_MISSION_STATUSES:
        return mission
    if mission.status == "waiting_for_user" and mission.phase == "pausing":
        return _finish_mission_pause_unlocked(conn, mission_id)
    if mission.status == "waiting_for_user" and mission.phase == "cancelling":
        return _cancel_mission_unlocked(conn, mission_id)
    if mission.status == "blocked" and (
        _mission_needs_quiescence(conn, mission_id)
        or not _blocked_quiescence_proven(conn, mission)
    ):
        _quiesce_blocked_mission(conn, mission)
        refreshed = mdb.get_mission(conn, mission_id)
        assert refreshed is not None
        return refreshed
    if _ambiguous_open_intents(conn, mission_id):
        if mission.status in {
            "running", "waiting_for_user", "verifying", "awaiting_approval", "committing",
        }:
            blocked = mdb.transition_mission(
                conn, mission_id, "blocked", phase="recovery",
                blocked_reason="recovery_ambiguous: unresolved mutation intent",
            )
            _quiesce_blocked_mission(conn, blocked)
            return blocked
    return mission


def begin_task_intent(task: kb.Task, *, board: Optional[str] = None) -> Optional[int]:
    """Checkpoint and journal a mission executor immediately before spawn."""
    if not task.mission_id or task.mission_role != "executor" or not task.workspace_path:
        return None
    if task.current_run_id is None or not task.claim_lock:
        raise MissionError("mission executor has no attributable Kanban run/claim")
    resolved_board = board or os.environ.get("HERMES_KANBAN_BOARD") or kb.get_current_board()
    with kb.connect_closing(board=resolved_board) as conn:
        validate_mission_task_graph(
            conn,
            task.mission_id,
            boundary="spawn",
            expected_task_id=task.id,
            expected_workspace=task.workspace_path,
        )
        current = kb.get_task(conn, task.id)
        if (
            current is None
            or current.current_run_id != task.current_run_id
            or current.claim_lock != task.claim_lock
            or current.status != "running"
        ):
            raise MissionError("mission executor claim changed before checkpoint")

        from tools.checkpoint_manager import CheckpointManager

        manager = CheckpointManager(enabled=True)
        manager.new_turn()
        checkpointed = manager.ensure_checkpoint(
            task.workspace_path,
            reason=f"mission {task.mission_id} task {task.id}",
        )
        checkpoints = (
            manager.list_checkpoints(task.workspace_path) if checkpointed else []
        )
        checkpoint_ref = checkpoints[0].get("hash") if checkpoints else None
        # Recheck after the external checkpoint operation so no card/edge or
        # workspace substitution can land in the gap before the intent row.
        validate_mission_task_graph(
            conn,
            task.mission_id,
            boundary="spawn",
            expected_task_id=task.id,
            expected_workspace=task.workspace_path,
        )
        current = kb.get_task(conn, task.id)
        if (
            current is None
            or current.current_run_id != task.current_run_id
            or current.claim_lock != task.claim_lock
            or current.status != "running"
        ):
            raise MissionError("mission executor claim changed before intent creation")
        return mdb.begin_intent(
            conn, task.mission_id, task_id=task.id, action="execute_task",
            run_id=task.current_run_id, claim_token=task.claim_lock,
            checkpoint_ref=checkpoint_ref,
            details={
                "workspace": task.workspace_path,
                "role": task.mission_role,
                "boot_id": _current_boot_id(),
            },
        )


def resolve_task_intent(
    task: kb.Task,
    *,
    outcome: str,
    run_id: Optional[int] = None,
    claim_token: Optional[str] = None,
    board: Optional[str] = None,
) -> int:
    if not task.mission_id:
        return 0
    resolved_board = board or os.environ.get("HERMES_KANBAN_BOARD") or kb.get_current_board()
    with kb.connect_closing(board=resolved_board) as conn:
        return mdb.resolve_task_intents(
            conn, task.mission_id, task.id, outcome=outcome,
            run_id=run_id if run_id is not None else task.current_run_id,
            claim_token=claim_token,
        )
