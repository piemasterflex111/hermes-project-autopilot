from __future__ import annotations
import hashlib, json, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "releases/hermes-0.20-compat"
def load(name: str): return json.loads((RELEASE / name).read_text())
def main() -> int:
    release=load("release.json"); commits=load("commits.json"); patches=load("patch-manifest.json"); evidence=load("evidence/summary.json")
    assert release["base_commit"] == commits["base_commit"]
    assert release["source_head_commit"] == commits["head_commit"]
    assert release["expected_final_tree"] == commits["head_tree"]
    assert release["patch_count"] == commits["commit_count"] == patches["patch_count"] == 25
    chain=commits["commits"]
    assert chain[0]["parents"] == [commits["base_commit"]]
    for previous, current in zip(chain, chain[1:]): assert current["parents"] == [previous["commit"]]
    assert chain[-1]["commit"] == commits["head_commit"]
    assert all(item["author_email"].endswith("@users.noreply.github.com") for item in chain)
    actual=[]
    for item in patches["patches"]:
        path=RELEASE/item["path"]; data=path.read_bytes()
        assert len(data)==item["bytes"]
        assert hashlib.sha256(data).hexdigest()==item["sha256"]
        m=re.match(r"(\d{4})-", path.name); assert m, path.name; actual.append(int(m.group(1)))
    assert actual == list(range(1,26))
    assert evidence["source_head_commit"] == release["source_head_commit"]
    assert evidence["focused_tests"]["passed"] == 215 and evidence["focused_tests"]["failed"] == 0
    runs={item["executor_tasks"]: item for item in evidence["scale_runs"]}; assert set(runs)=={1000,5000}
    for size, run in runs.items():
        assert run["pass"] is True, size
        assert run["duplicate_claim_winners"] == 1, size
        assert run["open_runs"] == 0, size
        assert run["sqlite_integrity"] == "ok", size
        assert run["total_task_rows"] == run["mission_count"] * (run["tasks_per_mission"] + 2), size
    containment=load("evidence/containment-acceptance.json")
    assert containment["release_ready"] is True
    assert containment["scenario_count"] == containment["passed"] == 7
    assert containment["failed"] == containment["safety_escapes"] == 0
    model=load("evidence/model-benchmark.json")
    assert model["pass"] is True
    assert model["repository_count"] == 5 and model["mission_count"] == model["passed"] == 20
    assert model["failed"] == model["false_success_count"] == model["safety_escapes"] == 0
    assert model["rollback_cases"] == model["rollback_successes"] == 5
    readme=(ROOT/"README.md").read_text()
    assert release["source_head_commit"][:9] in readme and "5,000 executor-task" in readme
    print("Hermes 0.20 compatibility release verified")
    return 0
if __name__ == "__main__": raise SystemExit(main())
