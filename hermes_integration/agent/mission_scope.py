"""Fail-closed mission policy binding for worker tool calls."""

from __future__ import annotations

import json
import os
import hashlib
import posixpath
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Optional


_ENV = "HERMES_MISSION_POLICY"
_CONTAINER_WORKTREE = PurePosixPath("/workspace")


def current_policy() -> Optional[dict[str, Any]]:
    raw = os.environ.get(_ENV, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PermissionError("malformed mission scope policy") from exc
    if not isinstance(value, dict) or not value.get("mission_id"):
        raise PermissionError("invalid mission scope policy")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _worktree_root(policy: dict[str, Any]) -> Path:
    root_raw = policy.get("worktree_path")
    if not root_raw:
        raise PermissionError("mission policy has no worktree path")
    return Path(str(root_raw)).expanduser().resolve(strict=False)


def mission_host_path(path: str | Path | PurePosixPath) -> Path:
    """Translate a mission-visible path to its host worktree equivalent.

    Mission tools execute inside Docker, where the approved linked worktree is
    mounted at ``/workspace``.  Validation, file metadata, and the evidence
    ledger run in the host process and must therefore inspect the real linked
    worktree path.  Accept both representations so the first file-tool call is
    safe even before a terminal command has established a session cwd.
    """
    policy = current_policy()
    if policy is None:
        return Path(path).expanduser().resolve(strict=False)

    root = _worktree_root(policy)
    raw = str(path)
    normalized = PurePosixPath(posixpath.normpath(raw))
    if normalized == _CONTAINER_WORKTREE:
        return root
    try:
        relative = normalized.relative_to(_CONTAINER_WORKTREE)
    except ValueError:
        relative = None
    if relative is not None:
        return (root / Path(relative.as_posix())).resolve(strict=False)

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def mission_container_path(path: str | Path | PurePosixPath) -> PurePosixPath:
    """Translate an approved host-worktree path to Docker's ``/workspace``.

    Paths outside the worktree are deliberately left as normalized absolute
    container paths.  Mutating callers subsequently pass them to
    :func:`require_write_path`, while host-side metadata callers pass them to
    :func:`mission_workspace_host_path`; both reject the escape.
    """
    policy = current_policy()
    raw = str(path)
    if policy is None:
        return PurePosixPath(posixpath.normpath(raw))

    normalized = PurePosixPath(posixpath.normpath(raw))
    if normalized == _CONTAINER_WORKTREE:
        return normalized
    try:
        normalized.relative_to(_CONTAINER_WORKTREE)
    except ValueError:
        pass
    else:
        return normalized

    root = _worktree_root(policy)
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        if _inside(resolved, root):
            relative = resolved.relative_to(root)
            return _CONTAINER_WORKTREE / PurePosixPath(relative.as_posix())
        return normalized

    # Relative paths are rooted at /workspace when no live terminal cwd exists.
    # File tools that do have a live /workspace/subdir cwd resolve against that
    # cwd before calling this helper.
    return PurePosixPath(posixpath.normpath(str(_CONTAINER_WORKTREE / normalized)))


def mission_workspace_host_path(path: str | Path | PurePosixPath) -> Path:
    """Map *path* to the host and require it to remain in the worktree."""
    policy = current_policy()
    host_path = mission_host_path(path)
    if policy is None:
        return host_path
    root = _worktree_root(policy)
    if not _inside(host_path, root):
        raise PermissionError(
            f"mission {policy['mission_id']} blocks paths outside {root}: {host_path}"
        )
    return host_path


def _require_live_executor_lease(policy: dict[str, Any]) -> None:
    """Revalidate the durable mission/task lease at every mutating tool edge."""
    if policy.get("mission_role") != "executor":
        raise PermissionError("only a mission executor may mutate the workspace")
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    run_raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    claim = os.environ.get("HERMES_KANBAN_CLAIM_LOCK", "").strip()
    db_raw = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not task_id or not run_raw or not claim or not db_raw:
        raise PermissionError("mission execution lease is incomplete")
    try:
        run_id = int(run_raw)
    except ValueError as exc:
        raise PermissionError("mission execution lease has an invalid run id") from exc

    try:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing(db_path=Path(db_raw).expanduser()) as conn:
            row = conn.execute(
                """SELECT t.status AS task_status, t.mission_id, t.mission_role,
                          t.current_run_id, t.claim_lock, m.status AS mission_status
                     FROM tasks t
                     JOIN missions m ON m.id=t.mission_id
                    WHERE t.id=?""",
                (task_id,),
            ).fetchone()
    except PermissionError:
        raise
    except Exception as exc:
        raise PermissionError("mission execution lease could not be validated") from exc
    if not row or not (
        row["mission_id"] == str(policy["mission_id"])
        and row["mission_role"] == "executor"
        and row["mission_status"] == "running"
        and row["task_status"] == "running"
        and int(row["current_run_id"] or -1) == run_id
        and str(row["claim_lock"] or "") == claim
    ):
        raise PermissionError("mission execution lease is no longer active")


def require_write_path(path: str | Path) -> None:
    policy = current_policy()
    if policy is None:
        return
    if policy.get("read_only"):
        raise PermissionError(
            f"mission {policy['mission_id']} role {policy.get('mission_role')} is read-only"
        )
    _require_live_executor_lease(policy)
    root = _worktree_root(policy)
    candidate = mission_workspace_host_path(path)
    if not _inside(candidate, root):
        raise PermissionError(
            f"mission {policy['mission_id']} blocks writes outside {root}: {candidate}"
        )
    allowed = [str(v).strip().strip("/") for v in policy.get("allowed_paths", [])]
    if allowed:
        rel = candidate.relative_to(root).as_posix()
        if not any(rel == item or rel.startswith(item + "/") for item in allowed):
            raise PermissionError(
                f"mission {policy['mission_id']} blocks out-of-scope path: {rel}"
            )


def terminal_host_cwd(cwd: str | Path, env_type: str) -> Path:
    """Map an approved terminal cwd back to the mission's host worktree."""
    policy = current_policy()
    if policy is None:
        return Path(cwd).expanduser().resolve(strict=False)
    root = _worktree_root(policy)
    if env_type != "docker":
        candidate = Path(cwd).expanduser().resolve(strict=False)
        if not _inside(candidate, root):
            raise PermissionError("mission terminal cwd is outside the approved worktree")
        return candidate
    mount_enabled = os.environ.get(
        "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    host_cwd_raw = os.environ.get("TERMINAL_CWD", "").strip()
    if not mount_enabled or not host_cwd_raw:
        raise PermissionError("mission Docker worktree mount is not enabled")
    host_cwd = Path(host_cwd_raw).expanduser().resolve(strict=False)
    if host_cwd != root:
        raise PermissionError("mission Docker mount does not match the approved worktree")
    candidate = Path(cwd)
    container_root = Path("/workspace")
    if not candidate.is_absolute() or not _inside(candidate, container_root):
        raise PermissionError("mission terminal cwd is outside the Docker worktree mount")
    return root / candidate.relative_to(container_root)


def require_terminal(
    cwd: str | Path,
    env_type: str,
    *,
    background: bool = False,
) -> None:
    policy = current_policy()
    if policy is None:
        return
    if policy.get("read_only"):
        raise PermissionError(
            f"mission {policy['mission_id']} role {policy.get('mission_role')} has no terminal capability"
        )
    _require_live_executor_lease(policy)
    if background:
        raise PermissionError("mission terminal background processes are not supported")
    allowed_backends = set(policy.get("allowed_terminal_backends") or ["docker"])
    if env_type != "docker" or env_type not in allowed_backends:
        raise PermissionError(
            f"mission {policy['mission_id']} requires the Docker terminal backend"
        )
    terminal_host_cwd(cwd, env_type)


def record_file_evidence(paths: list[str], *, task_id: Optional[str], action: str) -> Optional[int]:
    """Append hashes for a successful mission file mutation."""
    policy = current_policy()
    if policy is None:
        return None
    records = []
    for raw in paths:
        require_write_path(raw)
        path = mission_host_path(raw)
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            size = path.stat().st_size
        else:
            digest = None
            size = None
        records.append({"path": str(path), "sha256": digest, "bytes": size})
    from hermes_cli import kanban_db as kb
    from hermes_cli import missions_db as mdb
    db_raw = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not db_raw:
        raise PermissionError("mission evidence database is not pinned")
    run_raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    run_id = int(run_raw) if run_raw else None
    with kb.connect_closing(db_path=Path(db_raw).expanduser()) as conn:
        return mdb.record_evidence(
            conn, str(policy["mission_id"]), kind="file_mutation", status="passed",
            metadata={"action": action, "files": records}, task_id=task_id,
            run_id=run_id,
            cwd=str(policy.get("worktree_path") or ""),
        )
