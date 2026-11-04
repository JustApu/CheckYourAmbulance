# Issue #67 verification and performance record

The [current obligation projection](current-obligation-projection.md) changes
current reads while preserving immutable events and explicit historical replay.
SPEC §17.4 permits this disposable materialization; `SPEC.md` is unchanged.

## Rehearsal acceptance remains open

The issue #10 production-like 1× shift-change target is **not yet verified for
this change**. This development environment has no available connection to the
existing Rocky rehearsal host or its 149-day synthetic history. No deployment,
remote history extension, HTTPS/WebSocket load run, or 4×/10× run is claimed.
Local synthetic measurements below cannot establish the owner targets of 95%
under one second and 98% under two seconds on that host.

After review, the rehearsal operator should deploy the reviewed head with traffic
closed, migrate, refresh runtime grants, run the documented projection rebuild
and `--verify`, extend the synthetic history to the present, then repeat issue
#10's same 1× scenario: five vehicles × 150 items, 15 crew and two supervisors,
concurrent saves and page loads, live-update checklist reloads, and fleet
readiness. Record request counts, failures, p50/p90/p95, proportions below one
and two seconds, single-process CPU and MariaDB CPU. Retain the owner's p90 >3 s
cutoff before trying higher load steps. Record any remaining bottleneck instead
of treating a faster isolated query as a passing shift-change result.

## Local verification

Development verification uses a disposable MariaDB 10.11.18 server, Python 3.13,
and the hash-pinned Django 6.1.2 dependencies. Only hand-created synthetic
fixtures and the repository rehearsal generator are used. The server and test
schemas are isolated from runtime `data/`; no real configuration or source
rehearsal database is read.

The test and measurement results will be recorded here after the checks finish.
