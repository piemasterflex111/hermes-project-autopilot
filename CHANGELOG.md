# Changelog

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

### Fixed in the compatibility series

- Mission Docker policy adaptation for Hermes 0.20
- Signed-policy worktree authorization
- Recovery-phase executor requeue
- Compact mission retry context
- Mission input delivery and recovery hardening
- Current-mission-only verification evidence
