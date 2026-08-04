# Hermes Project Autopilot

[![Integrity](https://github.com/piemasterflex111/hermes-project-autopilot/actions/workflows/integrity.yml/badge.svg)](https://github.com/piemasterflex111/hermes-project-autopilot/actions/workflows/integrity.yml)
[![Integration replay](https://github.com/piemasterflex111/hermes-project-autopilot/actions/workflows/integration-replay.yml/badge.svg)](https://github.com/piemasterflex111/hermes-project-autopilot/actions/workflows/integration-replay.yml)

**A restart-safe, evidence-gated autonomous repository mission system implemented for Hermes Agent.**

Project Autopilot converts a repository objective into a durable mission with an explicit contract, bounded autonomy, an isolated Git worktree, a dependency graph, mutation-intent recovery, independent verification, authenticated operator controls, and auditable evidence.

This repository preserves the complete five-commit implementation delivered on top of [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent), plus exact source snapshots, apply-ready patches, architecture documentation, and fresh release evidence.

## Mission Center

![Hermes Project Autopilot Mission Center showing an L3 mission, dependency graph, lifecycle controls, and evidence ledger](assets/project-autopilot-kanban.webp)

*Live browser Mission Center during an L3 repository mission. The operator can create a bounded mission, inspect the dependency graph, monitor evidence-producing mutations, and pause, verify, cancel, reconcile, or roll back without allowing the UI to bypass mission policy.*

## The problem

A normal chat agent can lose context, declare success without proof, modify the wrong checkout, or resume incorrectly after a restart. Project Autopilot replaces conversational intent with durable state and fail-closed gates.

```text
objective
   ↓
contract + autonomy + scope
   ↓
isolated worktree + rollback ref
   ↓
planner → dependency graph
   ↓
executor tasks + mutation intents
   ↓
read-only verifier + deterministic commands
   ↓
operator approval or explicit L4 commit authority
   ↓
local commit only — never push, merge, deploy, or restart
```

## What was implemented

| Layer | Delivered capability |
|---|---|
| Durable state | SQLite mission contracts, roles, plans, task manifests, evidence, mutation intents, gateway subscriptions, and one-use actions |
| Repository isolation | Clean registered source checkout, dedicated Git worktree, branch, base commit, rollback ref, and Git identity checks |
| Autonomy | L0 advisory, L1 inspect, L2 prepare/plan, L3 execute with operator approval, L4 verified local commit only with separate authority |
| Execution containment | Allowed roots and paths, dedicated Docker workspace, detached-command rejection, offline-only v1 network policy |
| Recovery | Checkpoints, open mutation intents, restart reconciliation, lease quiescence, safe retry, cancellation, and verified rollback |
| Verification | Exact verification commands, non-empty diff, scope check, evidence hash-chain validation, independent verifier |
| Operator authorization | Expiring, one-use, SHA-256-hashed action capabilities bound to platform, chat, thread, scope, operator, mission, and action |
| Surfaces | CLI, slash commands, TUI Mission Center, desktop Mission Center, browser dashboard, and gateway-native controls |

## Architecture

```mermaid
flowchart LR
    U[Operator] --> C[Mission contract]
    C --> S[(SQLite mission state)]
    S --> P[Planner]
    P --> G[Durable task DAG]
    G --> E[Executor workers]
    E --> W[Isolated Git worktree]
    E --> I[Mutation intents + checkpoints]
    W --> V[Read-only verifier]
    I --> R[Restart reconciliation]
    V --> A{Approval gate}
    A -->|L3 operator approval| K[Local commit]
    A -->|L4 + explicit authority| K
    A -->|deny / failure| B[Blocked or recovery]
    S --> UI[CLI · TUI · Desktop · Browser]
    S --> GW[Authenticated gateway actions]
```

See [Architecture](docs/architecture.md), [Lifecycle](docs/lifecycle.md), and [Security model](docs/security-model.md).

## Current Hermes 0.20 compatibility release

The original five-commit v1 archive below remains preserved. A newer, separately versioned compatibility release records the complete 25-commit port and hardening series ending at `305506473`, including recovery-phase retry, Docker policy authorization, scale benchmarking, and compact retry context.

Fresh evidence for that release:

- **215 focused mission integration tests passed**
- **1,000 and 5,000 executor-task controller benchmarks passed**
- **exactly one claim winner** under 48 and 96 simultaneous attempts
- **zero leaked task runs**
- **SQLite integrity `ok`**

See [Hermes 0.20 compatibility release](releases/hermes-0.20-compat/README.md) and [Scalability](docs/scalability.md).

The measured claim is a **scale-tested single-workstation controller**. It is not a claim of multi-machine distributed execution or thousands of simultaneous model workers.

## Hermes 0.20 runtime-repair r2

An eight-patch delta now records the August 4 runtime repair after source head `305506473`. The repair rejects unregistered profiles, permits ordinary repository writes throughout the isolated `/workspace` worktree, preserves strict planning-time path validation, runs restricted mission containers as the host UID/GID, and proves a full controller → executor → verifier → verified-local-commit mission.

- Runtime-repair source head: `e1c0fba172b881f9fd6953ad7aad5c6f2081c04d`
- Deployed equivalent active head: `edf49e9b8c1287a501964119011d962529e58142`
- Exact final tree: `3de433cb93ac980be1fdd96cb4006cffdb5d4943`
- 86 Docker/containment tests and 100 mission regressions passed
- End-to-end mission `m_c488e9e80eb7` succeeded with an independent verifier and verified local commit

See [Hermes 0.20 runtime-repair r2](releases/hermes-0.20-runtime-repair-r2/README.md) and [Runtime containment modes](docs/runtime-containment-modes.md). The manual-plan runtime path is proven; end-to-end `plan-auto` acceptance remains pending.


## `ha` one-command release r3

The latest four-patch usability delta turns the audited mission engine into a
single everyday command:

```bash
ha "describe the task"
```

The command automatically handles project registration, mission creation,
dispatch, independent verification, verified local commit creation, and safe
application back to the working tree. It supports dirty parent repositories,
read-only repository inspection, normal change requests, and repositories with
no first commit yet.

- Deployed feature head: `d362d5f834e9f5d3ce9c095b2bb2a47b8d4346b4`
- Exact final tree: `43bd0f96f5b44398a70dc5485614f4a6d3b49712`
- 14 focused command tests and 100 mission regressions passed
- Full replay count: 37 ordered patches from the compatibility base

See [`ha` one-command release r3](releases/hermes-0.20-ha-one-command-r3/README.md).

### Operational acceptance evidence

The current release also includes two higher-level gates:

- **Terminal containment:** 7/7 scenarios passed with zero safety escapes, including real Docker/Landlock denial, mutation rollback evidence, repository-creation rejection, and controller-verifier rejection of nested `.git`.
- **Model-driven corpus:** 20/20 expected outcomes across five real repositories, with zero false successes, zero safety escapes, six retries, and five successful rollbacks.

See [Containment acceptance](docs/containment-acceptance.md), [Model benchmark](docs/model-benchmark.md), and [Release process](docs/release-process.md).

## Release evidence

Fresh verification against committed Hermes revision `a70c859e4`:

- **18 / 18 deterministic release scenarios passed**
- **0 failed scenarios**
- **0 safety escapes**
- **135 focused integration tests passed**
- Evidence hash chain, restart recovery, rollback, graph immutability, Docker containment, network fail-closed behavior, gateway replay prevention, identity binding, expiry, and detailed surfaces were covered

Artifacts:

- [`evidence/release-gate.json`](evidence/release-gate.json)
- [`evidence/focused-tests.txt`](evidence/focused-tests.txt)
- [`docs/release-evidence.md`](docs/release-evidence.md)

## Exact provenance

| Item | Value |
|---|---|
| Upstream | `https://github.com/NousResearch/hermes-agent.git` |
| Public replay base | `e444d165807f489b5c1ab8e4a612c8d09c2e67a2` |
| Required local prerequisite | `d8de415491a4935ad54021d10b2cc18f2c3d356b` |
| Source implementation commit | `a70c859e4aae7e4159522b1c2bc50ced165193e3` |
| Final Git tree | `7ba19cb3ce7b36f9b856b6290e247c050dc03ab3` |
| Authored commits | 5 |
| Exported integration files | 74 |

The CI integration-replay job clones upstream at the base commit, applies all five patches, and requires the resulting tree hash to equal the recorded final tree. This proves the patch series recreates the delivered implementation exactly.

## Repository layout

```text
hermes-project-autopilot/
├── hermes_integration/      # Exact committed files touched by the feature series
├── prerequisites/           # One local prerequisite patch after the public base
├── patches/                 # Five apply-ready Autopilot implementation patches
├── provenance/              # Commits, blob hashes, SHA-256 manifest, tree hashes
├── evidence/                # Fresh release-gate and focused-test outputs
├── docs/                    # Architecture, safety, lifecycle, authorization, surfaces
├── examples/                # Mission contract and operator workflow examples
├── scripts/                 # Integrity, privacy, and integration replay tooling
└── tests/                   # Repository-level provenance tests
```

## Verify this repository

```bash
./scripts/verify_repository.sh
```

That command verifies:

1. every exported file against its SHA-256 and Git blob ID;
2. the prerequisite and all five feature patches against their manifest;
3. the five-commit parent chain;
4. the release threshold (`18/18`, zero safety escapes);
5. privacy and credential scans;
6. repository-level tests.

## Replay the implementation into Hermes

```bash
./scripts/replay_integration.sh /tmp/hermes-autopilot-replay
```

The script clones Hermes Agent, checks out the exact base revision, applies the patch series, and verifies the final tree hash. It does not modify an existing Hermes installation.

For an existing compatible clone, follow [Integration and replay](docs/integration.md).

## Example mission

```bash
hermes mission create "Repair the parser" \
  --outcome "All parser tests pass without disabled tests" \
  --verify "pytest -q tests/parser" \
  --allowed-root /work/parser \
  --allowed-path src \
  --allowed-path tests \
  --autonomy 3 \
  --repo /work/parser \
  --project parser-project

hermes mission plan-auto m_abc123 --executor engineer --verifier reviewer
hermes mission start m_abc123
hermes mission verify m_abc123
hermes mission approve m_abc123
hermes mission commit m_abc123
```

## V1 boundaries

Project Autopilot v1 deliberately does **not**:

- push to a remote;
- merge branches;
- deploy software;
- restart services;
- modify the original source checkout;
- permit unrestricted network egress;
- treat an executor’s narrative as verification.

These are safety properties, not missing shortcuts.

## Status

This is an **integration subsystem repository**, not a fork or a misleading standalone replacement for Hermes Agent. The complete runtime depends on the upstream Hermes Kanban database, dispatcher, tools, gateway adapters, and UI applications. The patch series and exact source snapshot make the implementation reviewable and reproducible.

## License and attribution

Hermes Agent is MIT licensed. The preserved upstream files retain the Nous Research copyright notice. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
