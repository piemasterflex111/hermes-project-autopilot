# Runtime containment modes

## Planning contract

Planner declarations and manual task bodies remain bound to the mission
`allowed_paths` contract. Paths outside that contract are rejected before
execution.

## Strict runtime

`strict` retains per-path Landlock and file-tool enforcement. It is appropriate
for narrowly scoped missions where every writable path is known before the
worker starts.

## Workspace-permissive runtime

`workspace-permissive` treats the isolated Git worktree mounted at `/workspace`
as the writable runtime boundary. Normal repository tools may create, modify,
rename, and delete files anywhere beneath that worktree. This avoids failures
for missing files, generated outputs, lockfiles, caches, and build artifacts.

The following remain unavailable:

- host paths outside the worktree mount;
- extra profile-controlled mounts;
- the Docker socket;
- credential and environment passthrough;
- network egress unless separately authorized;
- remote push, merge, deployment, or service restart.

Restricted mission containers run as the invoking host UID/GID so output
remains readable by host Git inspection, controller verification, rollback,
and commit operations.

New CLI missions and legacy contracts without an explicit field currently
default to `workspace-permissive` at runtime. Explicit CLI mode selection and
end-to-end `plan-auto` acceptance remain follow-up work.
