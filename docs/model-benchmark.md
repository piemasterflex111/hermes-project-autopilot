# Model-driven multi-repository benchmark

Issue #1 is satisfied by twenty bounded missions across five real repositories. Each repository receives an L3 success mission, L4 success mission, narrative-only false-success challenge, and forced verification failure followed by retry and rollback.

## Results

- 5 repositories
- 20 missions: 10 L3 and 10 L4
- 20 expected outcomes achieved
- 0 false successes
- 0 safety escapes
- 6 retries
- 5/5 rollback cases restored successfully
- 660.067 seconds aggregate mission latency
- 1,573,451 measured executor input tokens
- 12,855 measured executor output tokens

Independent-verifier token use is marked unavailable rather than estimated because auxiliary verifier calls are not persisted in `session_model_usage`.

```bash
python3 scripts/run_model_benchmark.py \
  --workspace-root /isolated/benchmark/root \
  --output model-benchmark.json \
  --repository name=/path/to/repository
```

Repeat `--repository` five or more times. The runner uses disposable no-hardlink clones, a dedicated board, isolated mission worktrees, durable resume state, exact executor-session token accounting, and source-checkout cleanliness checks.
