from __future__ import annotations
import hashlib, json, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "releases/hermes-0.20-mobile-shortcuts-r4"
def load(name: str): return json.loads((RELEASE / name).read_text())
def main() -> int:
    release=load("release.json")
    commits=load("commits.json")
    patches=load("patch-manifest.json")
    evidence=load("evidence/mobile-shortcuts-summary.json")
    assert release["base_expected_tree"] == commits["base_tree"] == evidence["base_expected_tree"]
    assert release["source_base_commit"] == commits["source_base_commit"] == evidence["source_base_commit"]
    assert release["source_head_commit"] == commits["source_head_commit"] == evidence["source_head_commit"]
    assert release["active_equivalent_commit"] == commits["active_equivalent_commit"] == evidence["active_equivalent_commit"]
    assert release["expected_final_tree"] == commits["head_tree"] == evidence["expected_final_tree"]
    assert release["patch_count"] == commits["commit_count"] == patches["patch_count"] == 3
    assert release["total_patch_count_from_upstream_base"] == 40
    chain=commits["commits"]
    assert chain[0]["parents"] == [commits["source_base_commit"]]
    for previous,current in zip(chain,chain[1:]): assert current["parents"] == [previous["commit"]]
    assert chain[-1]["commit"] == commits["source_head_commit"]
    assert all(item["author_email"].endswith("@users.noreply.github.com") for item in chain)
    numbers=[]
    for item in patches["patches"]:
        path=RELEASE/item["path"]
        data=path.read_bytes()
        assert len(data) == item["bytes"]
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert b"@gmail.com" not in data
        private_home=("/home/"+"payam-adloo").encode()
        assert private_home not in data
        match=re.match(r"(\d{4})-", path.name); assert match, path.name
        numbers.append(int(match.group(1)))
    assert numbers == [1,2,3]
    tests=evidence["focused_tests"]
    assert tests["mobile_shortcuts_passed"] == 30
    assert tests["mission_and_containment_passed"] == 128
    assert tests["failed"] == 0
    assert all(evidence["capabilities"].values())
    assert all(item["status"] == "succeeded" for item in evidence["acceptance"].values())
    assert all(evidence["preflight_rejections"].values())
    assert evidence["remote_side_effects"] is False
    assert evidence["source_repository_cleanup_performed"] is False
    readme=(ROOT/"README.md").read_text()
    assert "mobile-shortcuts release r4" in readme
    assert release["source_head_commit"][:9] in readme
    print("Hermes mobile-shortcuts release r4 verified")
    return 0
if __name__ == "__main__": raise SystemExit(main())
