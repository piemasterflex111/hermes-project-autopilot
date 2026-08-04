#!/usr/bin/env python3
"""Build, checksum, sign, and verify Project Autopilot release assets."""
from __future__ import annotations
import argparse, hashlib, json, shutil, subprocess, sys
from pathlib import Path

def run(cmd: list[str], **kwargs):
    if not cmd or any(not isinstance(item, str) or "\x00" in item for item in cmd):
        raise ValueError("command must be a non-empty list of NUL-free strings")
    return subprocess.run(cmd, shell=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          check=True, **kwargs)  # nosec B603: argv list, never a shell
def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True); ap.add_argument("--tag", required=True)
    ap.add_argument("--output-dir", type=Path, required=True); ap.add_argument("--signing-key", type=Path, required=True)
    ap.add_argument("--identity", default="piemasterflex111")
    ap.add_argument("--project-name", default="hermes-project-autopilot")
    ap.add_argument("--repository-url", default="https://github.com/piemasterflex111/hermes-project-autopilot")
    ap.add_argument("--namespace-base", default="https://github.com/piemasterflex111/hermes-project-autopilot/spdx")
    ap.add_argument("--public-key", type=Path)
    args = ap.parse_args()
    repo, out, tag = args.repo.resolve(), args.output_dir.resolve(), args.tag
    out.mkdir(parents=True, exist_ok=True)
    commit = run(["git", "-C", str(repo), "rev-parse", f"{tag}^{{}}"]).stdout.strip()
    tree = run(["git", "-C", str(repo), "rev-parse", f"{tag}^{{tree}}"]).stdout.strip()
    base = f"{args.project_name}-{tag}"
    archive = out / f"{base}.tar.gz"
    with archive.open("wb") as fh:
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar.gz", f"--prefix={base}/", tag], stdout=fh, check=True)
    sbom = out / f"{base}.spdx.json"
    run([sys.executable, str(repo / "scripts/generate_sbom.py"), "--repo", str(repo), "--ref", tag, "--output", str(sbom), "--name", args.project_name, "--repository-url", args.repository_url, "--namespace-base", args.namespace_base])
    manifest = out / f"{base}.manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "tag": tag, "commit": commit, "tree": tree, "artifacts": {archive.name: sha(archive), sbom.name: sha(sbom)}}, indent=2) + "\n")
    sums = out / "SHA256SUMS"
    sums.write_text("".join(f"{sha(p)}  {p.name}\n" for p in [archive, sbom, manifest]))
    public = args.public_key or (Path(str(args.signing_key) + ".pub") if Path(str(args.signing_key) + ".pub").exists() else repo / "release/keys/payam-adloo-ed25519.pub")
    allowed = out / "allowed_signers"; allowed.write_text(args.identity + " " + public.read_text().strip() + "\n")
    shutil.copy2(public, out / "release-signing-key.pub")
    sig = Path(str(sums) + ".sig"); sig.unlink(missing_ok=True)
    run(["ssh-keygen", "-Y", "sign", "-f", str(args.signing_key), "-n", "file", sums.name], cwd=out)
    verify = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", allowed.name, "-I", args.identity, "-n", "file", "-s", sig.name], shell=False, cwd=out, input=sums.read_text(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # nosec B603
    if verify.returncode:
        raise SystemExit(verify.stdout)
    (out / "VERIFY.txt").write_text(f"sha256sum -c SHA256SUMS\nssh-keygen -Y verify -f allowed_signers -I {args.identity} -n file -s SHA256SUMS.sig < SHA256SUMS\n")
    print(json.dumps({"tag": tag, "commit": commit, "tree": tree, "signature_verified": True, "assets": sorted(p.name for p in out.iterdir())}, indent=2))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
