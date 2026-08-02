# Security model

## Assets protected

- Original repository checkout
- Files outside declared mission scope
- Git identity and rollback anchors
- Evidence integrity
- Operator approval authority
- Restart and recovery correctness
- Host processes and Docker boundaries

## Primary threats and controls

| Threat | Control |
|---|---|
| Agent writes outside objective scope | Canonical allowed roots and paths enforced at mutation tools and verification |
| Agent changes original checkout | Dedicated Git worktree outside the source repository |
| False success narrative | Exact commands, independent verifier, diff requirement, and evidence hash chain |
| Restart resumes an uncertain write | Mutation intent, checkpoint, process/boot/claim reconciliation, fail-closed recovery |
| Generic Kanban client rewrites mission DAG | Mission-linked structural mutations require controller capability |
| Rollback ref or `.git` identity tampering | Gitdir, base commit, branch, and rollback-ref integrity checks |
| Docker profile escapes mission containment | Mission-specific workspace and profile scrubbing |
| Network destination policy cannot be enforced | V1 rejects non-empty destination allowlists at start |
| Replayed approval button | Atomic one-use token claim |
| Approval copied to another user or chat | Token bound to platform, chat, thread, scope, principal, mission, and action |
| Token database disclosure | Only SHA-256 token hashes stored; raw capability exists only in delivery payload |

## V1 authority ceiling

The system may create a verified **local commit** under its contract. It has no authority to push, merge, deploy, restart services, or mutate the source checkout.
