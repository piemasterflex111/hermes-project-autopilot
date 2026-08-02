"""CLI and slash-command adapter for the shared mission service."""

from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import mission_service as service
from hermes_cli import missions_db as mdb


def build_parser(parent_subparsers) -> argparse.ArgumentParser:
    parser = parent_subparsers.add_parser(
        "mission",
        help="Run restart-safe, evidence-gated engineering missions",
    )
    parser.add_argument("--board", default=None)
    sub = parser.add_subparsers(dest="mission_action")

    create = sub.add_parser("create", help="Create a draft mission contract")
    create.add_argument("objective")
    create.add_argument("--outcome", required=True)
    create.add_argument("--verify", action="append", required=True)
    create.add_argument("--constraint", action="append", default=[])
    create.add_argument("--allowed-root", action="append", required=True)
    create.add_argument("--allowed-path", action="append", default=[])
    create.add_argument("--network", action="append", default=[])
    create.add_argument("--stop-when", action="append", default=[])
    create.add_argument("--autonomy", type=int, choices=range(0, 5), required=True)
    create.add_argument("--risk", choices=("low", "medium", "high"), default="medium")
    create.add_argument("--repo", required=True)
    create.add_argument("--project", required=True, help="Registered Hermes project id or slug")
    create.add_argument("--allow-local-commit", action="store_true")
    create.add_argument("--evidence-bytes", type=int, default=100 * 1024 * 1024)
    create.add_argument("--json", action="store_true")

    listing = sub.add_parser("list", aliases=["ls"])
    listing.add_argument("--status", choices=sorted(mdb.MISSION_STATUSES))
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--json", action="store_true")

    for name in (
        "show", "inspect", "prepare", "start", "pause", "resume", "verify", "approve",
        "commit", "deny", "retry", "cancel", "rollback", "reconcile",
    ):
        p = sub.add_parser(name)
        p.add_argument("mission_id")
        p.add_argument("--json", action="store_true")

    task = sub.add_parser("task-add", help="Add a serialized executor task")
    task.add_argument("mission_id")
    task.add_argument("title")
    task.add_argument("--body", required=True)
    task.add_argument("--assignee", required=True)
    task.add_argument("--parent", action="append", default=[])
    task.add_argument("--json", action="store_true")

    auto_plan = sub.add_parser("plan-auto", help="Create a bounded task graph with an isolated planner")
    auto_plan.add_argument("mission_id")
    auto_plan.add_argument("--executor", required=True)
    auto_plan.add_argument("--verifier", required=True)
    auto_plan.add_argument("--json", action="store_true")

    finish = sub.add_parser("plan-finish", help="Seal the graph with an independent verifier")
    finish.add_argument("mission_id")
    finish.add_argument("--verifier", required=True)
    finish.add_argument("--json", action="store_true")

    evidence = sub.add_parser("evidence-add", help="Append a command result to the mission ledger")
    evidence.add_argument("mission_id")
    evidence.add_argument("--command", required=True)
    evidence.add_argument("--cwd", required=True)
    evidence.add_argument("--exit-code", type=int, required=True)
    evidence.add_argument("--stdout-file")
    evidence.add_argument("--stderr-file")
    evidence.add_argument("--task")
    evidence.add_argument("--tool-call")
    evidence.add_argument("--json", action="store_true")

    verdict = sub.add_parser("verdict", help="Submit the independent verifier's strict JSON verdict")
    verdict.add_argument("mission_id")
    verdict.add_argument("--file", required=True)
    verdict.add_argument("--json", action="store_true")

    return parser


def _mission_json(mission: mdb.Mission) -> dict:
    return asdict(mission) | {"contract": asdict(mission.contract)}


def _emit(value, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(value, mdb.Mission):
        print(f"{value.id}  {value.status:18s} L{value.autonomy_level}  {value.objective}")
        if value.blocked_reason:
            print(f"  blocked: {value.blocked_reason}")
        if value.worktree_path:
            print(f"  worktree: {value.worktree_path}")
        if value.verified_commit:
            print(f"  commit: {value.verified_commit}")
        return
    print(value)


def mission_command(args: argparse.Namespace) -> int:
    board = args.board or kb.get_current_board()
    action = args.mission_action
    if not action:
        print("usage: hermes mission {create,list,show,inspect,prepare,task-add,plan-finish,start,pause,resume,evidence-add,verdict,verify,approve,deny,retry,commit,cancel,rollback,reconcile}")
        return 2
    try:
        with kb.connect_closing(board=board) as conn:
            if action == "create":
                contract = mdb.MissionContract(
                    outcome=args.outcome,
                    verification=args.verify,
                    constraints=args.constraint,
                    boundaries={
                        "allowed_roots": [str(Path(v).expanduser().resolve()) for v in args.allowed_root],
                        "allowed_paths": args.allowed_path,
                        "network_destinations": args.network,
                    },
                    stop_when=args.stop_when,
                    allow_local_commit=args.allow_local_commit,
                )
                mission = service.create_mission(
                    conn, objective=args.objective, contract=contract,
                    autonomy_level=args.autonomy, repo_path=args.repo, board=board,
                    risk_level=args.risk, project_id=args.project,
                    budget={"evidence_bytes": args.evidence_bytes},
                )
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action in {"list", "ls"}:
                missions = mdb.list_missions(conn, status=args.status, limit=args.limit)
                if args.json:
                    _emit([_mission_json(m) for m in missions], as_json=True)
                else:
                    for mission in missions:
                        _emit(mission)
            elif action == "show":
                report = service.mission_report(conn, args.mission_id)
                _emit(report if args.json else json.dumps(report, indent=2, default=str), as_json=args.json)
            elif action == "prepare":
                mission = service.prepare_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "inspect":
                mission = service.inspect_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "task-add":
                tid = service.add_execution_task(
                    conn, args.mission_id, title=args.title, body=args.body,
                    assignee=args.assignee, parents=args.parent,
                )
                _emit({"task_id": tid} if args.json else tid, as_json=args.json)
            elif action == "plan-auto":
                mission = service.plan_with_model(
                    conn, args.mission_id, executor_assignee=args.executor,
                    verifier_assignee=args.verifier,
                )
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "plan-finish":
                mission = service.finish_plan(conn, args.mission_id, verifier_assignee=args.verifier)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "start":
                mission = service.start_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "pause":
                mission = service.pause_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "resume":
                mission = service.resume_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "verify":
                mission = service.verify_with_model(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "evidence-add":
                stdout = Path(args.stdout_file).read_text(errors="replace") if args.stdout_file else ""
                stderr = Path(args.stderr_file).read_text(errors="replace") if args.stderr_file else ""
                eid = service.record_command_result(
                    conn, args.mission_id, command=args.command, cwd=args.cwd,
                    exit_code=args.exit_code, stdout=stdout, stderr=stderr,
                    task_id=args.task, tool_call_id=args.tool_call,
                )
                _emit({"evidence_id": eid} if args.json else eid, as_json=args.json)
            elif action == "verdict":
                payload = json.loads(Path(args.file).read_text())
                mission = service.submit_verifier_verdict(conn, args.mission_id, payload)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "approve":
                mission = service.approve_commit(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "commit":
                mission = service.commit_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "deny":
                mission = service.deny_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "retry":
                mission = service.retry_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "cancel":
                mission = service.cancel_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "rollback":
                mission = service.rollback_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
            elif action == "reconcile":
                mission = service.reconcile_mission(conn, args.mission_id)
                _emit(_mission_json(mission) if args.json else mission, as_json=args.json)
        return 0
    except (ValueError, KeyError, OSError, service.MissionError, json.JSONDecodeError) as exc:
        print(f"mission: {exc}")
        return 1


def run_slash(text: str, *, board: Optional[str] = None) -> str:
    """Execute `/mission ...` through the exact CLI parser and capture output."""
    import contextlib
    import io

    class _Root:
        def __init__(self):
            self.parser = argparse.ArgumentParser(prog="/mission", add_help=False)
            self.sub = self.parser.add_subparsers(dest="root")

    root = _Root()
    parser = build_parser(root.sub)
    argv = ["mission", *shlex.split(text)]
    if board:
        argv[1:1] = ["--board", board]
    out = io.StringIO()
    try:
        args = root.parser.parse_args(argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            mission_command(args)
    except SystemExit:
        return "Invalid /mission command. Use `/mission list` or `/mission create --help`."
    return out.getvalue().strip()
