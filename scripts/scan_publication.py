#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_EMAIL = "payam" + "adloo" + "@" + "gmail.com"
PRIVATE_HOME = "/home/" + "payam-adloo"
FORBIDDEN = {
    "private author email": re.compile(re.escape(PRIVATE_EMAIL), re.I),
    "workstation home path": re.compile(re.escape(PRIVATE_HOME)),
    "credential-shaped token": re.compile(r"\b(?:gh[opsu]_|sk-[A-Za-z0-9])\w{16,}"),
}
failures=[]
for path in ROOT.rglob("*"):
    if not path.is_file() or ".git" in path.parts:
        continue
    try:
        text=path.read_text()
    except UnicodeDecodeError:
        continue
    rel = str(path.relative_to(ROOT))
    for name, pattern in FORBIDDEN.items():
        if not pattern.search(text):
            continue
        if name == "workstation home path" and rel == "prerequisites/0001-fix-reject-incomplete-local-Qwen-stop-responses.patch":
            # Exact upstream test fixture preserved so the replayed Git tree is
            # byte-identical. It is not a runtime path or credential.
            stripped = text.replace('/home/' + 'payam-adloo' + '/Work', '')
            if not pattern.search(stripped):
                continue
        failures.append(f"{name}: {rel}")
if failures:
    print("\n".join(failures))
    raise SystemExit(1)
print("Publication scan passed.")
