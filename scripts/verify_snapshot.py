#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git_blob_id(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def main() -> int:
    manifest = json.loads((ROOT / "provenance/source-manifest.json").read_text())
    failures: list[str] = []
    files = manifest["files"]
    if manifest["file_count"] != len(files):
        failures.append("source manifest file_count mismatch")
    for item in files:
        path = ROOT / "hermes_integration" / item["path"]
        if not path.is_file():
            failures.append(f"missing snapshot file: {item['path']}")
            continue
        data = path.read_bytes()
        if len(data) != item["bytes"]:
            failures.append(f"byte-count mismatch: {item['path']}")
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            failures.append(f"SHA-256 mismatch: {item['path']}")
        if git_blob_id(data) != item["git_blob"]:
            failures.append(f"Git blob mismatch: {item['path']}")

    patch_manifest = json.loads((ROOT / "provenance/patch-manifest.json").read_text())
    for item in patch_manifest["patches"]:
        path = ROOT / item["path"]
        if not path.is_file():
            failures.append(f"missing patch: {item['path']}")
            continue
        data = path.read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            failures.append(f"patch integrity mismatch: {item['path']}")

    if failures:
        print("\n".join(failures))
        return 1
    print(f"Verified {len(files)} source files and {len(patch_manifest['patches'])} patches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
