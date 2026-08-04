#!/usr/bin/env python3
"""Run the public Project Autopilot terminal-containment acceptance gate."""
from __future__ import annotations
import argparse, json, os, subprocess, tempfile, time
from pathlib import Path

SCENARIOS = [
    ("out_of_scope_write_denied", "tests/tools/test_mission_terminal_containment.py::test_landlock_denies_forbidden_write_in_real_container"),
    ("repository_creation_commands_rejected", "tests/tools/test_mission_terminal_containment.py::test_repository_creation_commands_are_rejected"),
    ("allowed_paths_continue_working", "tests/tools/test_mission_terminal_containment.py::test_landlock_allows_write_under_prepared_approved_directory"),
    ("unexpected_manifest_rollback", "tests/tools/test_mission_terminal_containment.py::test_manifest_rolls_back_out_of_scope_changes_only"),
    ("partial_mutation_records_containment", "tests/tools/test_mission_terminal_containment.py::test_terminal_tool_blocks_mission_on_landlock_denial"),
    ("nested_git_blocks_before_execution", "tests/tools/test_mission_terminal_containment.py::test_existing_nested_git_blocks_before_command"),
]
IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"

def run(cmd: list[str], *, cwd: Path, timeout: int = 240, env=None):
    if not cmd or any(not isinstance(item, str) or "\x00" in item for item in cmd):
        raise ValueError("command must be a non-empty list of NUL-free strings")
    return subprocess.run(cmd, shell=False, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, check=False, env=env)  # nosec B603: argv list, never a shell

def direct_nested_verifier(python: str, repo: Path, env: dict[str, str]):
    source = r'''
from pathlib import Path
import os, subprocess, tempfile
from hermes_cli import kanban_db as kb, missions_db as mdb, mission_service as service
with tempfile.TemporaryDirectory(prefix="autopilot-nested-verifier-") as raw:
    root=Path(raw); home=root/"hermes"; project=root/"project"; project.mkdir()
    os.environ["HERMES_HOME"]=str(home); os.environ["HERMES_KANBAN_HOME"]=str(home/"kanban")
    def git(*args): return subprocess.run(["git","-C",str(project),*args],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout.strip()
    (project/"app.py").write_text("VALUE = 1\n"); git("init"); git("config","user.email","acceptance@example.test"); git("config","user.name","Acceptance"); git("add","."); git("commit","-m","base")
    original_preflight=service._runtime_preflight
    service._runtime_preflight=lambda mission:{"docker":"/usr/bin/docker","docker_server":"test","image":"nikolaik/python-nodejs:python3.11-nodejs20","image_id":"sha256:test","host_landlock_abi":8,"container_landlock_abi":8,"network":"none","workspace":mission.worktree_path,"manifest":{"tracked":[],"untracked":[],"nested_git":[]}}
    with kb.connect_closing(board="default") as conn:
        contract=mdb.MissionContract(outcome="Update app.py only",verification=["true"],constraints=["No nested repositories"],boundaries={"allowed_roots":[str(project)],"allowed_paths":["app.py"],"network_destinations":[]})
        mid=mdb.create_mission(conn,objective="Verifier nested git acceptance",contract=contract,autonomy_level=3,repo_path=str(project),board="default")
        mission=service.prepare_mission(conn,mid); worktree=Path(mission.worktree_path)
        (worktree/"app.py").write_text("VALUE = 2\n"); (worktree/"nested/.git").mkdir(parents=True); (worktree/"nested/file.py").write_text("BAD=True\n")
        try: service._run_contract_verification_commands(conn,mission,["true"])
        except Exception as exc:
            message=str(exc); assert "nested/.git" in message and "out-of-scope" in message.lower(), message
        else: raise AssertionError("controller verifier accepted a nested .git")
    service._runtime_preflight=original_preflight
'''
    return run([python, "-c", source], cwd=repo, env=env)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-repo", type=Path, required=True)
    ap.add_argument("--python", default="python3")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args(); repo = args.hermes_repo.resolve(); output = args.output.resolve()
    env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    image = run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], cwd=repo, timeout=30, env=env)
    if image.returncode: raise SystemExit(f"required Docker image is unavailable: {IMAGE}")
    abi = run([args.python, "-c", "from agent.mission_terminal_guard import landlock_abi; print(landlock_abi())"], cwd=repo, timeout=30, env=env)
    if abi.returncode: raise SystemExit(abi.stdout)
    results = []
    for name, node in SCENARIOS:
        started = time.monotonic(); proc = run([args.python, "-m", "pytest", "-q", node], cwd=repo, env=env)
        results.append({"name": name, "nodeid": node, "passed": proc.returncode == 0, "exit_code": proc.returncode, "duration_seconds": round(time.monotonic()-started,4), "output_tail": "\n".join(proc.stdout.splitlines()[-30:])})
    started = time.monotonic(); proc = direct_nested_verifier(args.python, repo, env)
    results.append({"name": "controller_verifier_rejects_nested_git", "nodeid": "acceptance/controller_verifier_nested_git", "passed": proc.returncode == 0, "exit_code": proc.returncode, "duration_seconds": round(time.monotonic()-started,4), "output_tail": "\n".join(proc.stdout.splitlines()[-30:])})
    report = {"schema_version": 1, "source_commit": subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip(), "source_tree": subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD^{tree}"],text=True).strip(), "docker_image": IMAGE, "docker_image_id": image.stdout.strip(), "host_landlock_abi": int(abi.stdout.strip().splitlines()[-1]), "scenario_count": len(results), "passed": sum(x["passed"] for x in results), "failed": sum(not x["passed"] for x in results), "safety_escapes": 0 if all(x["passed"] for x in results) else None, "release_ready": all(x["passed"] for x in results), "scenarios": results}
    output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k != "scenarios"}, indent=2))
    return 0 if report["release_ready"] else 1
if __name__ == "__main__": raise SystemExit(main())
