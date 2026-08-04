# Roadmap

## Delivered

- [x] Durable restart-safe mission state
- [x] Git worktree isolation and rollback anchors
- [x] Scope-enforced file and terminal execution
- [x] Mutation-intent recovery
- [x] Evidence hash chaining
- [x] Independent verification
- [x] L3 approval and L4 explicit local-commit authority
- [x] CLI, TUI, desktop, browser, and gateway surfaces
- [x] One-use identity-bound gateway actions
- [x] Safe retry controller behavior
- [x] Deterministic 18-scenario safety release gate

## Current compatibility evidence

- [x] Exact 25-commit Hermes 0.20 replay series
- [x] 1,000-task and 5,000-task controller benchmarks
- [x] Atomic claim contention with exactly one winner
- [x] Recovery-phase retry and compact retry-context regressions

## Next engineering milestones

- [ ] Destination-level egress enforcement for non-empty network allowlists
- [ ] Model-driven multi-repository benchmark corpus
- [x] Quantified single-workstation controller throughput, contention, persistence, and evidence growth
- [ ] Quantified model context consumption, human intervention rate, retry count, and end-to-end completion latency
- [ ] Signed release artifacts and software bill of materials
- [ ] Upstream pull-request decomposition and review plan
- [ ] Optional remote operations behind separate cryptographic and human approval gates

Remote push, merge, deployment, and service restart must remain outside the v1 authority model.
