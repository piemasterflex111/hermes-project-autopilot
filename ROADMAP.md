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
- [x] Eight-patch Hermes 0.20 runtime-repair delta with exact replay tree
- [x] Host-UID mission execution and successful executor/verifier/local-commit proof

## Completed operational gates

- [x] Terminal allowed-path containment acceptance gate: 7/7 scenarios, zero escapes
- [x] Controller verifier rejects nested Git repositories
- [x] Signed release and SBOM generation process

## Next engineering milestones

- [ ] Destination-level egress enforcement for non-empty network allowlists
- [x] Model-driven multi-repository benchmark corpus: 5 repositories, 20 missions, zero false successes and zero safety escapes
- [x] Quantified single-workstation controller throughput, contention, persistence, and evidence growth
- [ ] Quantified model context consumption, human intervention rate, retry count, and end-to-end completion latency
- [x] Signed release bundle tooling and SPDX 2.3 software bill of materials
- [ ] Upstream pull-request decomposition and review plan
- [ ] Explicit CLI containment-mode selection and end-to-end `plan-auto` acceptance
- [ ] Optional remote operations behind separate cryptographic and human approval gates

Remote push, merge, deployment, and service restart must remain outside the v1 authority model.
