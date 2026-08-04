# Hermes 0.20 compatibility release

This directory publishes the exact 25-commit Project Autopilot compatibility and hardening series applied on top of upstream Hermes commit `91937a6dc3ffbbe2f3be91a500f0ecf962c4cf53`.

It does **not** replace or rewrite the original v1 archive at the repository root. The original five-commit release remains immutable historical provenance. This release records the later port, recovery fixes, containment fixes, scale harness, and compact retry-context work.

## Verified identity

| Item | Value |
|---|---|
| Upstream base | `91937a6dc3ffbbe2f3be91a500f0ecf962c4cf53` |
| Source implementation commit | `30550647352814d934204d113dec6f742f9627b8` |
| Final Git tree | `561ad3ff0402f44382bbab150d15ffaff9136ed4` |
| Patch count | 25 |
| Focused integration tests | 215 passed |

## Scale evidence

| Workload | Result |
|---|---:|
| 100 bounded missions × 10 executor tasks | 1,000 executor tasks completed |
| 500 bounded missions × 10 executor tasks | 5,000 executor tasks completed |
| Claim contention | exactly one winner under 48 and 96 simultaneous attempts |
| Leaked task runs | 0 |
| SQLite integrity | `ok` |
| 1,000-task throughput | 178.8 task transitions/second |
| 5,000-task throughput | 103.04 task transitions/second |

These results prove strong **single-workstation controller scale**. They do not prove multi-machine coordination or thousands of simultaneous model workers. See [`../../docs/scalability.md`](../../docs/scalability.md).

## Replay

```bash
./scripts/replay_release.sh releases/hermes-0.20-compat /tmp/hermes-autopilot-020
```

The replay checks out the exact upstream base, applies all 25 patches, and requires the resulting tree to match the recorded final tree. Public patch author emails are sanitized, so the source commit identity is recorded separately from the replayed commit identity.

## Evidence and provenance

- [`release.json`](release.json)
- [`commits.json`](commits.json)
- [`patch-manifest.json`](patch-manifest.json)
- [`evidence/summary.json`](evidence/summary.json)
- [`evidence/scale-1000.json`](evidence/scale-1000.json)
- [`evidence/scale-5000.json`](evidence/scale-5000.json)
- [`evidence/focused-tests.txt`](evidence/focused-tests.txt)
