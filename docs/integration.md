# Integration and replay

## Safe replay into a temporary clone

```bash
./scripts/replay_integration.sh /tmp/hermes-autopilot-replay
```

The script:

1. clones `NousResearch/hermes-agent`;
2. checks out public base commit `e444d165807f489b5c1ab8e4a612c8d09c2e67a2`;
3. applies one required local prerequisite patch, then the five Autopilot patches in order;
4. verifies the resulting tree equals `7ba19cb3ce7b36f9b856b6290e247c050dc03ab3`;
5. compiles the key Python modules.

## Existing clone

Do not apply the patches to a dirty or unrelated revision.

```bash
git status --short
git checkout e444d165807f489b5c1ab8e4a612c8d09c2e67a2
git am /path/to/hermes-project-autopilot/prerequisites/*.patch
git am /path/to/hermes-project-autopilot/patches/*.patch
```

Then compare:

```bash
git rev-parse HEAD^{tree}
# expected: 7ba19cb3ce7b36f9b856b6290e247c050dc03ab3
```

## Full runtime verification

Inside the patched Hermes environment:

```bash
venv/bin/python scripts/evaluate_project_autopilot.py \
  --output /tmp/project-autopilot-evaluation.json
```

The release threshold is all 18 scenarios passing with zero safety escapes.
