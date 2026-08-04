from __future__ import annotations
import json, subprocess, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class ReleaseToolTests(unittest.TestCase):
    def test_annotated_tag_is_dereferenced_for_sbom_and_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); repo = root / "repo"; repo.mkdir()
            def run(*args, **kwargs):
                return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, **kwargs)
            run("git", "-C", str(repo), "init")
            run("git", "-C", str(repo), "config", "user.name", "Release Test")
            run("git", "-C", str(repo), "config", "user.email", "release@example.test")
            (repo / "README.md").write_text("test\n")
            scripts = repo / "scripts"; scripts.mkdir()
            for name in ("generate_sbom.py", "build_release_bundle.py"):
                (scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
            run("git", "-C", str(repo), "add", ".")
            run("git", "-C", str(repo), "commit", "-m", "base")
            commit = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
            run("git", "-C", str(repo), "tag", "-a", "v-test", "-m", "annotated")
            key = root / "key"
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
            out = root / "dist"
            run("python3", str(ROOT / "scripts" / "build_release_bundle.py"), "--repo", str(repo), "--tag", "v-test", "--output-dir", str(out), "--signing-key", str(key), "--public-key", str(key) + ".pub")
            manifest = json.loads((out / "hermes-project-autopilot-v-test.manifest.json").read_text())
            sbom = json.loads((out / "hermes-project-autopilot-v-test.spdx.json").read_text())
            self.assertEqual(commit, manifest["commit"])
            self.assertIn(commit, sbom["documentNamespace"])

if __name__ == "__main__": unittest.main()
