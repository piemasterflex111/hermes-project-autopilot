# Hermes 0.20 runtime-repair r2

This release is an eight-patch delta applied after the immutable
[`hermes-0.20-compat`](../hermes-0.20-compat/README.md) release. It preserves
the prior 25-patch archive and adds the runtime corrections proven on August 4,
2026.

## Verified identity

| Item | Value |
|---|---|
| Base compatibility source head | `30550647352814d934204d113dec6f742f9627b8` |
| Base compatibility tree | `561ad3ff0402f44382bbab150d15ffaff9136ed4` |
| Runtime-repair source head | `e1c0fba172b881f9fd6953ad7aad5c6f2081c04d` |
| Deployed equivalent active head | `edf49e9b8c1287a501964119011d962529e58142` |
| Final Git tree | `3de433cb93ac980be1fdd96cb4006cffdb5d4943` |
| Delta patch count | 8 |
| Total patches from the upstream compatibility base | 33 |

The clean repair head and deployed active head have different commit identities
because the repair was cherry-picked into the active checkout. They resolve to
the same final Git tree.

## Repairs delivered

- reject unregistered executor and verifier profiles before task storage;
- permit ordinary terminal and file-tool writes throughout the isolated mission
  worktree;
- retain strict planning-time and manual-task validation against
  `allowed_paths`;
- force restricted mission containers to run as the invoking host UID/GID;
- preserve host Git inspection, controller verification, rollback, and commit;
- record a successful controller → executor → verifier → verified-local-commit
  mission.

## Containment semantics

`workspace-permissive` uses `/workspace` as the runtime write boundary. The
executor may create, modify, rename, and delete normal repository files beneath
that isolated Git worktree. Host paths outside the mount, extra mounts,
credential passthrough, Docker socket access, and network egress remain
unavailable.

Planning remains contract-bound: planner declarations and manual task bodies
that name paths outside `allowed_paths` are rejected before execution. `strict`
runtime mode remains available to direct policy callers and tests. New CLI
missions and legacy contracts without the field currently default to
`workspace-permissive` at runtime.

## Acceptance evidence

- 86 Docker, ownership, and containment tests passed;
- 100 mission regression tests passed;
- invalid assignees are rejected before task storage;
- normal terminal creation of a missing approved file succeeds;
- output remains owned and readable by the host user;
- controller, executor, and independent verifier all completed;
- mission `m_c488e9e80eb7` reached `succeeded`;
- verified local commit `d061df716a630ab09e2b7888a02fdf565ec70dee`
  contains `HERMES_AUTOPILOT_OK`.

The proven path used manual `task-add` plus `plan-finish`. End-to-end
`plan-auto` acceptance remains a separate gate.

## Replay

```bash
./scripts/replay_runtime_repair_release.sh /tmp/hermes-autopilot-runtime-r2
```

The replay first reconstructs the 25-patch compatibility release, then applies
this ordered eight-patch delta and requires the final tree to equal
`3de433cb93ac980be1fdd96cb4006cffdb5d4943`.

## Evidence

- [`evidence/runtime-repair-summary.json`](evidence/runtime-repair-summary.json)
- [`evidence/end-to-end-mission.json`](evidence/end-to-end-mission.json)
- [`evidence/ownership-proof.json`](evidence/ownership-proof.json)
- [`evidence/focused-tests.txt`](evidence/focused-tests.txt)
