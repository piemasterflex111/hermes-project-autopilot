# Product surfaces

## CLI and slash commands

Mission creation, planning, start, pause, resume, verify, approve, deny, retry, reconcile, commit, cancel, and rollback are available through the shared backend.

## TUI Mission Center

The native terminal overlay renders mission lists, contracts, task dependency edges, evidence, open intents, recovery state, and lifecycle controls using shared RPCs.

## Desktop and browser

Both Mission Centers display:

- objective, outcome, constraints, and verification commands;
- repository, worktree, branch, base commit, rollback ref, and verified commit;
- controller, planner, executor, and verifier tasks;
- dependency graph edges;
- evidence records and hash prefixes;
- evidence-chain validity;
- unresolved mutation intents and checkpoint anchors;
- blockers and valid recovery actions.

## Gateway platforms

Relay-based Telegram, Discord, Slack, Matrix, and Feishu, plus WhatsApp Cloud and QQ, receive native controls. Other adapters use the same identity-bound token through a text fallback.
