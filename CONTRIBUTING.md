# Contributing

This repository primarily preserves an exact Hermes integration series.

## Change categories

1. **Documentation or repository tooling** may be changed directly here.
2. **Project Autopilot product code** should be developed in a Hermes Agent clone, tested there, and exported as a new patch with updated provenance.
3. **Source snapshot files** under `hermes_integration/` must never be edited manually without updating their source commit and manifests.

## Required checks

```bash
./scripts/verify_repository.sh
./scripts/replay_integration.sh /tmp/hermes-autopilot-replay
```

A product-code change must include focused tests and an updated release-gate report.
