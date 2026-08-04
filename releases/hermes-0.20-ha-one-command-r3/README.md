# Hermes 0.20 `ha` one-command release r3

This release is a four-patch usability delta applied after
[`hermes-0.20-runtime-repair-r2`](../hermes-0.20-runtime-repair-r2/README.md).
It turns the proven Project Autopilot engine into an everyday command:

```bash
ha "describe the task"
```

## Verified identity

| Item | Value |
|---|---|
| Runtime-repair base tree | `3de433cb93ac980be1fdd96cb4006cffdb5d4943` |
| Deployed base commit | `edf49e9b8c1287a501964119011d962529e58142` |
| Deployed feature head | `d362d5f834e9f5d3ce9c095b2bb2a47b8d4346b4` |
| Final Git tree | `43bd0f96f5b44398a70dc5485614f4a6d3b49712` |
| Delta patch count | 4 |
| Total patches from the upstream compatibility base | 37 |

## User-facing behavior

`ha` detects the current Git repository and selected subfolder, registers the
project, creates the mission graph, dispatches the executor and independent
verifier, waits for completion, and returns a concise result.

### Change mode

```bash
ha "create docs/NOTES.md with a short project summary"
```

A successful change is committed in the isolated mission branch and applied
back to the working tree only when it does not overlap existing local edits.
Dirty parent repositories are handled through a clean shadow worktree.

### Inspect mode

```bash
ha "tell me what this repo does"
```

Inspection produces an independently verified answer without modifying the
source repository. The model receives a sanitized manifest of ordinary project
files rather than Git worktree metadata or host paths.

### Unborn repositories

A repository created with `git init` no longer needs a manual first commit.
Autopilot builds a temporary synthetic baseline, preserves the source's unborn
state for inspection, and can safely apply a requested verified change.

## Acceptance evidence

- 14 focused `ha` command tests passed;
- 100 mission regression tests passed;
- dirty-repository change completed without touching unrelated local work;
- unborn-repository inspection completed without creating a source commit;
- unborn-repository change completed without requiring a manual first commit;
- existing-repository inspection returned a verified answer without source
  modification or internal-path leakage.

## Replay

```bash
./scripts/replay_ha_one_command_release.sh /tmp/hermes-autopilot-ha-r3
```

The replay reconstructs the 25-patch compatibility release, applies the
8-patch runtime repair, applies this ordered 4-patch usability delta, and
requires the final tree to equal `43bd0f96f5b44398a70dc5485614f4a6d3b49712`.

## Evidence

- [`evidence/ha-feature-summary.json`](evidence/ha-feature-summary.json)
- [`evidence/focused-tests.txt`](evidence/focused-tests.txt)
