# Release evidence

## Fresh run

The exact committed implementation at `a70c859e4` was verified immediately before this repository was generated.

| Measure | Result |
|---|---:|
| Release scenarios | 18 |
| Passed | 18 |
| Failed | 0 |
| Safety escapes | 0 |
| Release ready | true |
| Focused integration tests | 135 passed |

## Scenario coverage

- Successful L4 mission
- False-success prevention
- Path-scope escape prevention
- Source-checkout preservation
- Registered clean project gate
- Explicit local-commit authority
- Operator denial and retry
- Restart recovery
- Verified rollback
- Git identity tamper prevention
- Rollback-ref tamper prevention
- Task-graph mutation prevention
- Docker sandbox escape prevention
- Network-scope fail-closed behavior
- Gateway replay prevention
- Gateway operator binding
- Gateway expiry and malformed-token rejection
- Detailed mission surface contract

The machine-readable report is stored at `evidence/release-gate.json`.
