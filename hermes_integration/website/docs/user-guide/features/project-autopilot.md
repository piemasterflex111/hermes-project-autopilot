---
sidebar_position: 13
title: Project Autopilot missions
---

# Project Autopilot missions

Project Autopilot turns a repository objective into a durable, evidence-gated mission. It extends the Kanban task graph and dispatcher rather than creating a second scheduler.

A mission stores its objective, required outcome, verification commands, allowed roots and paths, autonomy level, task graph, isolated worktree, rollback ref, evidence chain, and final disposition. A restart can therefore resume from verified state instead of reconstructing intent from chat history.

## Safety contract

- Autonomy is required explicitly for every mission. L0 is advisory, L1 may inspect, L2 may prepare and plan, L3 may execute with an approval before local commit, and L4 may make a verified local commit only when the contract separately grants local-commit authority.
- Version 1 never pushes, merges, deploys, restarts services, or changes the source checkout.
- Creation requires a registered Hermes project and a clean Git source checkout. Levels 2–4 immediately create a dedicated Git worktree, branch, controller card, and rollback ref outside the source repository.
- Executor file writes are restricted to declared paths. Version 1 terminal commands run only in a dedicated, non-persistent Docker sandbox with the mission worktree mounted at `/workspace`; detached background commands are rejected.
- Version 1 supports offline execution (`network_destinations: []`). A non-empty destination allowlist is rejected at the start gate until destination-level egress enforcement is available.
- The verifier is read-only and receives the contract, diff, and evidence—not the executor's justification.
- Exact verification commands, a valid evidence hash chain, a non-empty diff, and path-scope checks must all pass immediately before commit.

## Typical workflow

```bash
hermes mission create "Repair the parser" \
  --outcome "All parser tests pass without disabled tests" \
  --verify "scripts/run_tests.sh tests/parser -q" \
  --allowed-root /work/parser \
  --allowed-path src --allowed-path tests \
  --autonomy 3 --repo /work/parser --project parser-project

# Levels 2–4 are prepared automatically during creation.
hermes mission plan-auto m_abc123 --executor engineer --verifier reviewer
hermes mission start m_abc123
hermes mission show m_abc123
hermes mission verify m_abc123
hermes mission approve m_abc123
hermes mission commit m_abc123
```

Use `pause`, `resume`, `deny`, `retry`, `cancel`, `reconcile`, and `rollback` for lifecycle and recovery. Pause is cooperative at the next mutating tool boundary; cancel synchronously revokes the durable lease, stops the executor and its task-labeled sandbox, and becomes terminal only after quiescence is proven. Rollback resets and cleans only the mission-owned worktree after the same quiescence checks, then verifies it matches the preserved rollback ref.

At Level 3, approval and commit remain separate operator actions: `approve` opens the commit gate, but neither the dispatcher nor the mission controller performs the commit. Level 4 may verify and create the local commit automatically only when `allow_local_commit` was explicitly granted at creation. Neither level pushes or merges.

The same workflow is available through `/mission …`, the native Project Autopilot section of the Kanban dashboard and desktop app, and the shared mission RPCs. In the terminal interface, run `/missions` to open the native Mission Center. It supports mission creation, list and detail views, durable task-graph edges, evidence inspection, approval, denial, retry, reconciliation, commit, cancellation, and rollback without implementing policy in the client.

## Evidence and recovery

Foreground terminal results and successful file mutations are appended to a hash-chained ledger. Large command bodies are compressed, redacted, SHA-256 hashed, and stored under the Hermes mission evidence directory. The SQLite row retains the hash, size, command, exit code, task, and timestamps.

Before an executor starts, Hermes creates a filesystem checkpoint and a mutation intent. A clean task completion resolves the intent. On restart, `reconcile` leaves an open intent alone only when the exact recorded boot, process, run, and claim owner is still live. An orphaned or mismatched open intent blocks the mission because its filesystem state is ambiguous and must be inspected rather than guessed.

## Release gate

Run the deterministic safety evaluation before labeling Project Autopilot v1 release-ready:

```bash
./scripts/evaluate_project_autopilot.py --output /tmp/project-autopilot-evaluation.json
```

The command returns success only when every acceptance scenario passes and the report records zero safety escapes. It measures mission completion, false-success prevention, scope containment, source-checkout preservation, project registration, explicit commit authority, operator intervention, restart recovery, rollback, Git identity integrity, graph immutability, sandbox containment, and network-scope enforcement.

## Gateway action routing

Running a gateway `/mission` command for one mission automatically subscribes the exact requesting platform, chat, thread, and operator to that mission. Use `/mission watch <mission-id>` to subscribe explicitly and `/mission unwatch <mission-id>` to remove the route.

When the mission reaches `awaiting_approval`, `blocked`, `waiting_for_user`, or `committing`, the gateway sends the actions that are valid for that state. Relay-based Telegram, Discord, Slack, Matrix, and Feishu connectors, WhatsApp Cloud, and QQ render native buttons. Other adapters receive a text fallback containing the same one-use action capabilities.

Each action capability:

- is random and opaque;
- is stored only as a SHA-256 hash in the board database;
- is bound to the board, mission, action, platform, chat, thread, workspace or guild scope, and exact operator identity;
- expires after 15 minutes;
- can be claimed once through an atomic database update;
- rejects replay, stale, cross-user, cross-chat, and cross-thread use;
- records only a short token-hash prefix in mission evidence.

The public fallback command is `/mission action <button-token>`. Copying that command to another user or conversation does not transfer authority because the resolver rechecks the current message source against the durable binding.

## Detailed mission surfaces

The native terminal Mission Center (`/missions`), desktop Mission Center, and browser Kanban dashboard all consume the same mission report. The desktop and browser detail panels show:

- the mission contract, verification commands, and enforceable roots, paths, and network scope;
- repository, isolated worktree, branch, base commit, rollback ref, and verified commit;
- every controller, planner, executor, and verifier task plus dependency edges;
- the complete evidence ledger with status, command exit code, task association, and record-hash prefix;
- evidence-chain validity as a first-class pass/fail signal;
- unresolved mutation intents and their checkpoint anchors;
- current blockers and the same shared lifecycle/recovery actions used by CLI and TUI.

Clients never implement mission policy locally. They call the existing detail and action endpoints, while the mission service remains the only state-transition authority.
