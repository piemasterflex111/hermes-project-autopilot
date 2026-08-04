#!/usr/bin/env python3
"""Generate a deterministic SPDX 2.3 JSON SBOM for one Git ref."""
from __future__ import annotations
import argparse, hashlib, json, subprocess
from pathlib import Path

def git(repo: Path, *args: str, binary: bool = False):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=not binary)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--output", type=Path, required=True)
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
    name = "hermes-project-autopilot"
    doc = {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{name}-{args.ref}", "documentNamespace": f"https://github.com/piemasterflex111/{name}/spdx/{commit}",
        "creationInfo": {"created": "2026-08-04T00:00:00Z", "creators": ["Tool: scripts/generate_sbom.py"]},
        "packages": [{"SPDXID": "SPDXRef-Package", "name": name, "versionInfo": args.ref,
            "downloadLocation": "https://github.com/piemasterflex111/hermes-project-autopilot", "filesAnalyzed": True,
            "packageVerificationCode": {"packageVerificationCodeValue": verification.hexdigest()},
            "licenseConcluded": "NOASSERTION", "licenseDeclared": "MIT", "copyrightText": "NOASSERTION",
            "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": f"pkg:github/piemasterflex111/{name}@{commit}"}] }],
        "files": files,
        "relationships": [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": "SPDXRef-Package"}, *relationships],
        "annotations": [{"annotationDate": "2026-08-04T00:00:00Z", "annotationType": "OTHER", "annotator": "Tool: scripts/generate_sbom.py", "comment": f"Git commit {commit}; Git tree {tree}"}],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
