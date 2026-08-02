# Reddit launch draft

## Recommended community

`r/LocalLLaMA`

The post is framed as an engineering discussion and disclosed self-promotion, not a request for stars. The screenshot is a real running mission state and has been cropped to exclude browser tabs, the desktop dock, unrelated cards, and local filesystem paths.

## Title

I added restart-safe, evidence-gated repository autopilot missions to Hermes Agent

## Body

I kept hitting the same problem with autonomous coding agents: the model could make progress, but the system still depended too much on chat history and trust.

A restart could lose the actual execution state. A worker could say a task was complete without deterministic proof. A broad repository instruction could touch the wrong checkout. Approval buttons could become unsafe if they were only stored in process memory.

So I built **Project Autopilot** as a subsystem for Hermes Agent.

A mission now has a durable contract containing:

- the required outcome and exact verification commands;
- autonomy level L0–L4;
- registered project and clean source-repository gates;
- allowed roots, paths, and offline-only network scope;
- an isolated Git worktree, branch, base commit, and rollback ref;
- a dependency graph with controller, planner, executor, and verifier roles;
- mutation intents and checkpoints for restart recovery;
- a hash-chained evidence ledger;
- explicit approval before L3 commit, or separate L4 local-commit authority.

The verifier is read-only and receives the contract, diff, and evidence rather than the executor’s justification. V1 never pushes, merges, deploys, or restarts services.

The browser Mission Center in the screenshot is showing a real L3 mission while Hermes is decomposing and executing the task graph. The same mission state is available through CLI, TUI, desktop, and authenticated gateway controls.

The gateway controls use random one-use capabilities stored only as SHA-256 hashes and bound to the platform, chat, thread, scope, operator, mission, action, and expiry. Replay and cross-user use fail closed.

Verification results:

- 18/18 deterministic safety scenarios passed;
- 0 safety escapes;
- 135 focused integration tests passed;
- exact upstream replay reproduces the final Git tree.

The repository contains the real five-commit patch series, 74 exact integration files, cryptographic provenance manifests, architecture/security docs, and CI that reapplies the changes to the public Hermes upstream base.

GitHub: https://github.com/piemasterflex111/hermes-project-autopilot

Self-promotion disclosure: I implemented and published this subsystem. I would value technical criticism of the recovery model, evidence design, and autonomy boundaries more than general agent-framework feedback.

## Suggested first comment

The part I am least certain about is the v1 network model. It deliberately rejects non-empty destination allowlists because destination-level egress enforcement is not implemented yet. I chose fail-closed/offline execution instead of pretending that a prompt-level network policy was enforceable.

For the next milestone I am building a model-driven multi-repository benchmark that measures completion rate, false-success rate, interventions, retries, context consumption, latency, and rollback success across L3 and L4 missions.
