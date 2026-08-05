# Hermes 0.20 mobile-shortcuts release r4

This release is a three-patch mobile usability delta applied after
[`ha` one-command release r3](../hermes-0.20-ha-one-command-r3/README.md).
It makes the audited Autopilot workflow practical from a phone terminal.

## Everyday commands

```bash
hai FILE
ham TASK WITHOUT QUOTES
hapaste
```

- `hai FILE` inspects one named file, prints a grounded answer, and archives the
  independently verified result.
- `ham ...` executes a change request without requiring quotation marks.
- `hapaste` accepts interactive pasted text terminated by `.done`, or ordinary
  standard input from a pipe, and removes the temporary input afterward.

## Verified identity

| Item | Value |
|---|---|
| r3 base tree | `43bd0f96f5b44398a70dc5485614f4a6d3b49712` |
| Source base commit | `d362d5f834e9f5d3ce9c095b2bb2a47b8d4346b4` |
| Source feature head | `b5053677363998180d9a6f8dd5fbe2c1b12bc0cf` |
| Deployed equivalent head | `d8e82a0c3a8f4e2c907e9232ebffabf398db5b14` |
| Exact final Git tree | `f3ee17996dd69de2045a68e507438db1825c18e3` |
| Delta patch count | 3 |
| Total patches from the compatibility base | 40 |

The source and deployed heads have different commit identities because the
release was cherry-picked into the active Hermes checkout. They resolve to the
same final Git tree.

## Additional behavior

- `ha` accepts unquoted multiword objectives;
- `read`, `open`, `look at`, `do you see`, and `check` select inspect mode;
- named modified or untracked inputs are copied into a disposable shadow rather
  than committed to the source repository;
- missing files fail before mission creation and offer a close-name suggestion;
- dates and URLs in prose are not interpreted as repository paths;
- vague tasks are rejected without a Python traceback;
- commands refuse to start inside Hermes shadow or mission-worktree paths;
- focused answers omit internal workspace terminology and remove unsupported
  high-impact actions from next-action sections;
- existing tracked Git links are removed only from the disposable inspection
  baseline, allowing safe inspection without changing the original repository.

## Acceptance evidence

- 30 focused mobile-command tests passed;
- 128 mission and containment tests passed;
- named-file inspection succeeded in a real dirty repository containing three
  pre-existing tracked Git links, with source status unchanged;
- `hapaste` succeeded from piped input and removed the temporary source file;
- unquoted `ham` applied a verified change;
- unquoted `ha explain README.md` returned and archived a verified answer;
- typo suggestion, vague-task rejection, protected-worktree rejection, and
  date/URL prose acceptance all passed.

No user repository cleanup is included in this release, and no remote push,
merge, deployment, or other repository side effect is delegated to Autopilot.

## Replay

```bash
./scripts/replay_mobile_shortcuts_release.sh /tmp/hermes-autopilot-mobile-r4
```

The replay reconstructs the previous 37-patch release, applies this ordered
three-patch delta, and requires the final tree to equal
`f3ee17996dd69de2045a68e507438db1825c18e3`.

## Evidence

- [`evidence/mobile-shortcuts-summary.json`](evidence/mobile-shortcuts-summary.json)
- [`evidence/focused-tests.txt`](evidence/focused-tests.txt)
