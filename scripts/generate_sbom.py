#!/usr/bin/env python3
"""Generate a deterministic SPDX 2.3 JSON SBOM for one Git ref."""
from __future__ import annotations
import argparse, datetime, hashlib, json, subprocess
from pathlib import Path

def git(repo: Path, *args: str, binary: bool = False):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=not binary)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--name", default="hermes-project-autopilot")
    ap.add_argument("--repository-url", default="https://github.com/piemasterflex111/hermes-project-autopilot")
    ap.add_argument("--namespace-base", default="https://github.com/piemasterflex111/hermes-project-autopilot/spdx")
    args = ap.parse_args()
    repo = args.repo.resolve()
    commit = git(repo, "rev-parse", args.ref).strip()
    tree = git(repo, "rev-parse", f"{args.ref}^{{tree}}").strip()
    paths = [p for p in git(repo, "ls-tree", "-r", "--name-only", args.ref).splitlines() if p]
    files, relationships = [], []
    verification = hashlib.sha256()
    for index, path in enumerate(paths, 1):
        raw = git(repo, "show", f"{args.ref}:{path}", binary=True)
        checksum = hashlib.sha256(raw).hexdigest()
        verification.update(path.encode() + b"\0" + checksum.encode() + b"\n")
        spdx = f"SPDXRef-File-{index}"
        files.append({"SPDXID": spdx, "fileName": path, "checksums": [{"algorithm": "SHA256", "checksumValue": checksum}], "licenseConcluded": "NOASSERTION", "copyrightText": "NOASSERTION"})
        relationships.append({"spdxElementId": "SPDXRef-Package", "relationshipType": "CONTAINS", "relatedSpdxElement": spdx})
    name = args.name
    committed = git(repo, "show", "-s", "--format=%cI", args.ref).strip()
    created = datetime.datetime.fromisoformat(committed).astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    doc = {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{name}-{args.ref}", "documentNamespace": f"{args.namespace_base.rstrip('/')}/{commit}",
        "creationInfo": {"created": created, "creators": ["Tool: scripts/generate_sbom.py"]},
        "packages": [{"SPDXID": "SPDXRef-Package", "name": name, "versionInfo": args.ref,
            "downloadLocation": args.repository_url, "filesAnalyzed": True,
            "packageVerificationCode": {"packageVerificationCodeValue": verification.hexdigest()},
            "licenseConcluded": "NOASSERTION", "licenseDeclared": "MIT", "copyrightText": "NOASSERTION",
            "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": f"pkg:github/piemasterflex111/{name}@{commit}"}] }],
        "files": files,
        "relationships": [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": "SPDXRef-Package"}, *relationships],
        "annotations": [{"annotationDate": created, "annotationType": "OTHER", "annotator": "Tool: scripts/generate_sbom.py", "comment": f"Git commit {commit}; Git tree {tree}"}],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
