# Changelog

## 2026-08-04 — mobile-shortcuts release r4

- add `hai FILE`, unquoted `ham ...`, and interactive/piped `hapaste`;
- snapshot modified and untracked named inputs into disposable shadows;
- archive independently verified inspection answers;
- recognize common read-only phrases and reject vague tasks before model work;
- ignore dates and URLs during planner path extraction;
- add missing-file suggestions and protected-worktree rejection;
- ground next-action output and remove internal workspace terminology;
- isolate inspection from pre-existing tracked Git links;
- publish three apply-ready patches, exact 40-patch replay, 30 focused mobile tests, and 128 mission/containment tests.

## 2026-08-04 — `ha` one-command release r3

- add the `ha "describe the task"` everyday entrypoint;
- automate repository detection, project registration, mission construction, dispatch, verification, and safe result application;
- support dirty parent repositories through clean shadow worktrees;
- distinguish read-only inspect mode from change mode;
- support repositories with no first commit yet;
- publish four apply-ready patches, exact final-tree replay, 14 focused command tests, and 100 mission regressions.

## 2026-08-04 — Hermes 0.20 runtime-repair r2

- publish an eight-patch delta after compatibility source head `305506473`;
- reject unregistered mission executor and verifier profiles before storage;
- introduce workspace-permissive runtime execution while retaining strict planning-time path validation;
- force restricted mission containers to run as the host UID/GID;
- record 86 Docker/containment tests, 100 mission regressions, exact 33-patch replay, and a successful independent-verifier local-commit mission.

## v1.0.0 — Project Autopilot implementation archive

### Added

- Restart-safe mission contracts and SQLite persistence
- Registered-project and clean-checkout creation gate
- Isolated Git worktrees, rollback refs, and Git identity validation
- Durable planner/executor/verifier task graph
- Mutation intents, checkpoints, reconciliation, cancellation, and rollback
- L0–L4 autonomy with separate L4 local-commit authority
- Fail-closed deterministic and model verification
- Native TUI Mission Center
- Detailed desktop and browser mission surfaces
- Identity-bound gateway subscriptions and one-use action tokens
- Safe retry requeue behavior for blocked executor tasks
- 18-scenario release gate and repository provenance replay


## v1.1.0 — Hermes 0.20 compatibility and scale evidence

### Added

- Exact 25-commit compatibility and hardening patch series based on upstream commit `91937a6dc`
- Generic versioned-release replay script
- Machine-readable 1,000- and 5,000-executor-task benchmark evidence
- Focused 215-test compatibility evidence
- Repository consistency validation for commit chains, patch hashes, and scale invariants
- Explicit scalability boundaries distinguishing controller scale from distributed model-worker scale

- Seven-scenario Docker/Landlock terminal-containment acceptance report
- Five-repository, twenty-mission model-driven benchmark with zero false successes and zero safety escapes
- Exact executor token, latency, retry, intervention, and rollback measurements
- Reproducible benchmark and containment runners
- SPDX 2.3 SBOM generator and SSH-signed release-bundle tooling
- Public release signing key and documented verification process

### Fixed in the compatibility series

- Mission Docker policy adaptation for Hermes 0.20
- Signed-policy worktree authorization
- Recovery-phase executor requeue
- Compact mission retry context
- Mission input delivery and recovery hardening
- Current-mission-only verification evidence
