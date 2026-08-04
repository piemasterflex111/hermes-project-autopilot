# Scalability

## Scale model

Project Autopilot is intentionally designed around **many bounded missions**, not one unbounded agent plan. Scaling means increasing durable missions and task transitions while retaining atomic claims, recovery, evidence, and verification invariants.

## Measured single-workstation result

Fresh evidence is stored under [`releases/hermes-0.20-compat/evidence/`](../releases/hermes-0.20-compat/evidence/).

| Measurement | 1,000-task run | 5,000-task run |
|---|---:|---:|
| Missions | 100 | 500 |
| Executor tasks per mission | 10 | 10 |
| Total task rows | 1,200 | 6,000 |
| Evidence rows | 2,600 | 13,000 |
| Concurrent workers | 24 | 48 |
| Simultaneous claim attempts | 48 | 96 |
| Winners for the contested task | 1 | 1 |
| Open/leaked runs | 0 | 0 |
| SQLite integrity | `ok` | `ok` |
| Executor transitions/second | 178.8 | 103.04 |

## What the benchmark proves

- bounded multi-mission task graphs remain persistent;
- task claims remain atomic under contention;
- dependency promotion completes executor chains;
- evidence storage grows without losing SQLite integrity;
- the database can be closed and reopened after the workload;
- no task runs remain open after completion.

## What it does not prove

- multi-machine distributed coordination;
- thousands of simultaneous language-model workers;
- high availability, leader election, or automatic failover;
- network-partition tolerance;
- PostgreSQL or externally managed control-plane behavior;
- production service-level objectives.

The current claim is **scale-tested single-workstation controller**, not distributed orchestration platform.
