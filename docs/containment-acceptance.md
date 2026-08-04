# Terminal containment acceptance

Issue #2 is satisfied by seven executable scenarios against the Hermes 0.20 compatibility tree. The gate covers out-of-scope writes, repository creation commands, allowed-path continuity, manifest rollback, containment evidence, pre-execution nested Git detection, and controller-verifier rejection of nested `.git`.

```bash
python3 scripts/run_containment_acceptance.py \
  --hermes-repo /path/to/replayed-hermes \
  --python /path/to/hermes/venv/bin/python \
  --output containment-acceptance.json
```

The committed report records 7/7 passed, zero safety escapes, Landlock ABI 8, the Docker image digest, source commit, source tree, node IDs, durations, and output tails.
