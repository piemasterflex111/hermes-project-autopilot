# Provenance

This repository contains two complementary records.

## Exact source snapshot

`hermes_integration/` contains the committed contents of every file touched by the five-commit implementation series, read directly from Git object `a70c859e4` rather than from the working tree.

`provenance/source-manifest.json` records for each file:

- repository-relative path;
- byte count;
- SHA-256 digest;
- upstream Git blob ID.

## Apply-ready patch series

`prerequisites/` contains the one local prerequisite commit required after the nearest public upstream ancestor.

`patches/` contains five patches representing the direct Autopilot commit chain:

1. restart-safe Project Autopilot missions;
2. v1 safety gates;
3. native TUI Mission Center;
4. authenticated controls and detailed surfaces;
5. safe retry executor requeue fix.

Patch author email headers were replaced with the GitHub noreply identity before publication. Patch content and resulting Git tree are unchanged.

The integration-replay workflow proves the patches recreate the recorded final Git tree exactly.

## Publication privacy exception

The prerequisite patch preserves one literal `/home/.../Work` string from an upstream unit-test parameter that verifies short path-like model responses are accepted. It is not a runtime path, credential, or configuration value. The publication scanner allowlists only that exact patch fixture so the replayed tree remains byte-identical.
