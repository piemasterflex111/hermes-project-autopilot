from __future__ import annotations
import hashlib, json, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / 'releases/hermes-0.20-runtime-repair-r2'
def load(name: str): return json.loads((RELEASE / name).read_text())
def main() -> int:
    release=load('release.json'); commits=load('commits.json'); patches=load('patch-manifest.json'); evidence=load('evidence/runtime-repair-summary.json')
    assert release['base_source_head_commit'] == commits['base_source_head_commit']
    assert release['base_expected_tree'] == commits['base_tree']
    assert release['source_head_commit'] == commits['head_commit'] == evidence['source_head_commit']
    assert release['active_equivalent_commit'] == commits['active_equivalent_commit'] == evidence['active_equivalent_commit']
    assert release['expected_final_tree'] == commits['head_tree'] == evidence['expected_final_tree']
    assert release['patch_count'] == commits['commit_count'] == patches['patch_count'] == 8
    assert release['total_patch_count_from_upstream_base'] == 33
    chain=commits['commits']; assert chain[0]['parents'] == [commits['base_source_head_commit']]
    for previous, current in zip(chain, chain[1:]): assert current['parents'] == [previous['commit']]
    assert chain[-1]['commit'] == commits['head_commit']
    assert all(item['author_email'].endswith('@users.noreply.github.com') for item in chain)
    actual=[]
    for item in patches['patches']:
        path=RELEASE/item['path']; data=path.read_bytes()
        assert len(data) == item['bytes']; assert hashlib.sha256(data).hexdigest() == item['sha256']
        assert b'@gmail.com' not in data
        match=re.match(r'(\d{4})-', path.name); assert match, path.name; actual.append(int(match.group(1)))
    assert actual == list(range(1,9))
    tests=evidence['focused_tests']; assert tests['docker_containment_passed'] == 86 and tests['mission_regressions_passed'] == 100 and tests['failed'] == 0
    proof=evidence['end_to_end']; assert proof['status'] == 'succeeded'; assert proof['controller'] == proof['executor'] == proof['verifier'] == 'done'
    assert proof['final_disposition'] == 'verified_local_commit'; assert proof['remote_push_or_merge'] is False
    assert evidence['plan_auto_end_to_end_proven'] is False
    readme=(ROOT/'README.md').read_text(); assert release['source_head_commit'][:9] in readme and 'runtime-repair r2' in readme
    print('Hermes 0.20 runtime-repair r2 verified')
    return 0
if __name__ == '__main__': raise SystemExit(main())
