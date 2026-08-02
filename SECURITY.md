# Security policy

## Report a vulnerability

Do not publish credentials, exploitable bypass details, or private environment data in a public issue. Use GitHub private vulnerability reporting when enabled, or contact the repository owner through the GitHub profile.

## Security properties under test

- Source checkout remains unchanged
- Writes remain inside declared paths
- Mission cards cannot be structurally mutated by generic Kanban clients
- Git worktree identity and rollback refs are checked before destructive actions
- Open mutation intents block ambiguous restart recovery
- Verification requires deterministic evidence and a valid hash chain
- Gateway action capabilities are one-use, expiring, and identity-bound
- V1 refuses unenforceable network destination policies

See [Security model](docs/security-model.md) for the detailed threat model.
