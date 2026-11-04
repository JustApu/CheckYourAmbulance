# Issue #67 verification and performance record

The [current obligation projection](current-obligation-projection.md) changes
current reads while preserving immutable events and explicit historical replay.
SPEC §17.4 permits this disposable materialization; `SPEC.md` is unchanged.

## Issue #70 latency target remains open

The issue #10 production-like 1× shift-change target, tracked in [issue #70](https://github.com/JustApu/CheckYourAmbulance/issues/70), is **not met** on the reviewer-reported rehearsal run before the status-lock fix. Only 2.8% of requests completed under one second and 3.1% under two seconds, below the owner's 95%/98% targets.
The sample and its limitations are recorded below; no 4×/10× run is claimed.
The local synthetic measurements below do not establish the owner targets.

After review, the rehearsal operator should deploy the reviewed head with traffic
closed, migrate, refresh runtime grants, run the documented projection rebuild
and `--verify`, extend the synthetic history to the present, then repeat issue
#10's same 1× scenario: five vehicles × 150 items, 15 crew and two supervisors,
concurrent saves and page loads, live-update checklist reloads, and fleet
readiness. Record request counts, failures, p50/p90/p95, proportions below one
and two seconds, single-process CPU and MariaDB CPU. Retain the owner's p90 >3 s
cutoff before trying higher load steps. Record any remaining bottleneck instead
of treating a faster isolated query as a passing shift-change result.

On reviewed head `0154162a6d6e99de61eb9a8c47b8e290260ac9b0`, before the follow-up query and rebuild fixes below, the cross-family reviewer reported 750 saves with
zero errors, but the save p50/p95 were 17.9/23.4 seconds and checklist reload
p50/p95 were 26.3/31.1 seconds. Only 2.8% of requests completed under one second
and 3.1% under two seconds. Both supervisor readiness pages timed out in the
reported run. The base comparison completed 300 saves with 23 errors or timeouts
and had 21 timeouts among 164 checklist reloads, so the runs are not directly
comparable. These results come from the trusted cross-family review on PR #69;
host details are omitted. The follow-up fixes have not been measured on the rehearsal host,
so no result is claimed for the new head; the observed run shows the acceptance target
remains unmet.

## Twelve-month sequential profile before the status read-time join

On head `7f46aaba14d5fde8a735b6b47c892ba4fd706960`, the cross-family reviewer
also profiled the same five vehicles against twelve months of synthetic history
(129,000 obligations). These are in-process, sequential samples with no HTTP or
concurrent load; they do not replace the 1× scenario above.

| Operation | Samples | p50 | p90 |
|---|---:|---:|---:|
| Current checklist | not stated | 1.44 s | 1.52 s |
| Fleet readiness | not stated | 2.83 s | 3.12 s |
| Explicit-as-of readiness replay | not stated | 14.1 s | not stated |
| `record_check` save | 40 | 0.43 s | 0.57 s |
| `set_vehicle_status` | 2 | 8.5 s | not stated |

All five vehicle caches were hit and current checklist/readiness results matched
replay on the measured data. The status command time included refreshing 1,650
future obligations while holding the vehicle lock, which delayed a concurrent crew
save by 6.6 seconds. The follow-up implementation removes status from persisted
projection facts and joins the applicable immutable status event at current read
time; these samples predate that change, and its rehearsal-host latency has not been
measured. Checklist and readiness remained above the 1-second target in these
isolated samples.

The follow-up reviewer completed the full rebuild on the same 129,000-obligation
synthetic history under the runtime role in 4,872 seconds (about 26 obligations per
second). It wrote 113,258 projection rows and verified all 129,000 obligations,
with no evidence-cap errors. This is the observed duration operators should budget
for a comparable history before reopening traffic. It is a one-host measurement,
not a general throughput guarantee. The separate `--verify`-only pass was still
running when the reviewer reported these results; its completion has not yet been
confirmed.

The read-time status join adds one bounded `VehicleStatusEvent` query per current
page read: one query for a checklist vehicle and one page-wide query for readiness,
independent of the number of obligations. Relative to the recorded 27-query
checklist / 22-query readiness samples above, this implies 28 and 23 queries if
the remaining query paths are unchanged. Its wall-time effect has not been measured
against the same year-long rehearsal data because the local MariaDB service was
unavailable; the follow-up reviewer will measure it on the new head. In a separate
in-memory microbenchmark, the empty-evidence evaluator path ran 3,000 synthetic
obligations in a median 0.0141 seconds versus 0.0154 seconds for the previous
conversion path (1.09× faster). This excludes database access and JSON decoding,
so it does not establish full read latency. No issue #70 target pass is claimed.

## Local verification

Development verification uses a disposable MariaDB 10.11.18 server, Python 3.13,
and the hash-pinned Django 6.1.2 dependencies. Only hand-created synthetic
fixtures and the repository rehearsal generator are used. The server and test
schemas are isolated from runtime `data/`; no real configuration or source
rehearsal database is read.

Measured source: `d3a325c685e819500d500e8675e0330007d113b5` on
`issue-67-checklist-read-performance`, 2026-11-04 UTC.

Completed checks:

- Ruff lint and format checks, Django system checks and migration-drift checks passed.
- 32 current-projection/readiness tests passed on the final lookup migration,
  including concurrent saves/status refresh, captured-time replay, shared-chain
  counters, clock boundaries, rollback, missing-cache fallback and rebuild recovery.
- 34 workflow-screen tests passed after routing current forms through current reads.
- 17 bounded-history/projection tests passed, including seeded weeks through real
  commands, invalidated newest chains, long gaps and sibling-kit isolation.
- The broad offline DBA/security run discovered 1,927 tests: 55 skipped and one
  failure. The failure was the M2 fixture's assumption that the clock after a
  status command equals its event timestamp; refresh advanced that test clock by
  one microsecond. The fixture now uses the recorded status event timestamps.
  The subsequent scenario/runtime-account run passed 65 tests (55 skipped),
  including the corrected M2 scenario. The broad suite was not repeated end to
  end after the final focused fixes; those fixes received the checks above.
- 88 check-tool tests, 31 offline helper tests and 26 authorization-policy tests
  passed. Contract-manifest, scenario, Markdown-link and normative-registry
  checks passed. The pinned, checksum-verified Gitleaks 8.30.1 history scan found
  no leaks. `tblib` 3.2.2 was installed only in the local verification environment
  for parallel traceback reporting; project dependency manifests are unchanged.
- The vehicle-lookup migration preserved all values of all 9,750 projection rows
  on application and through a reverse/forward round trip. Inspection confirmed
  that the cache no longer references the mutable vehicle row with a foreign key.

Remote CI and independent cross-family/human review are required for a review
handoff. During local verification the connector approval gate rejected draft-PR
creation and lifecycle label updates because its approval policy is `never`;
source commits can be pushed and issue evidence can be recorded. The signed
issue/PR evidence records subsequent remote-check and review status.

## Controlled local service measurements

The repository's `tools.rehearsal_history.generate` built nine synthetic days
(seed 7), five vehicles × 150 presence/condition items and 15 crew. It produced
6,750 checks: 6,660 Passed, 70 Failed and 20 Unable, plus 48 weekly deficiency
closures. Obligation generation then extended the history to the present and
48 hours ahead. After 100 measured saves there were 6,850 checks, 9,750 immutable
obligations and 9,750 projection rows.

The timing run used one sequential Python process after the background test
runs finished. It called `vehicle_checklist`, `fleet_readiness` and the real
`record_check` command directly, with no forced cache reset. Checklist samples
rotated across all five vehicles. Saves were 100 distinct item checks by one
synthetic crew member, with their displayed basis IDs and no inspection session.
Percentiles use nearest rank. Times are seconds:

| Operation | Samples | p50 | p90 | p95 | Maximum | Under 1 s / under 2 s |
|---|---:|---:|---:|---:|---:|---:|
| Current checklist, 150 rows | 20 | 0.1544 | 0.1860 | 0.1983 | 0.5556 | 20 / 20 |
| Current readiness, five vehicles | 10 | 0.1803 | 0.1903 | 0.2042 | 0.2042 | 10 / 10 |
| Explicit-as-of checklist replay | 5 | 0.3302 | 0.4826 | 0.4826 | 0.4826 | 5 / 5 |
| Explicit-as-of readiness replay | 3 | 1.2522 | 1.5759 | 1.5759 | 1.5759 | 0 / 3 |
| Record-check command | 100 | 0.0538 | 0.0641 | 0.0983 | 0.1182 | 100 / 100 |

Current checklist and readiness used 27 and 22 SQL queries respectively.
Current rows and readiness were also compared with explicit replay at the same
captured instant and matched. Rebuild returned successfully in 26.13 seconds;
read-only verification then returned successfully in 17.29 seconds.
The replay samples include this branch's query fixes, so the comparison isolates
the materialized read path after those fixes.

The disposable database used a 1 GiB buffer pool,
`innodb_flush_log_at_trx_commit=2` and disabled binary logging. The development
machine was not constrained to the rehearsal host's two vCPUs. These small
sequential service samples exclude HTTP/TLS, browser/page rendering, middleware,
inspection-session writes, concurrent crew traffic, connected WebSockets and
live-update reload fan-out. They also use nine generated days rather than the
existing 149-day rehearsal history. Production durability costs and the owner's
1× shift-change target remain unverified. The preliminary first checklist read
while background verification was active took 5.54 seconds; it was not part of
the controlled sample above.

## Cleanup

The disposable database, generated synthetic rows, socket directory and private
measurement/checkpoint artifacts are removed after verification. Reusable tools
and the development virtual environment remain under ignored `local/`. No runtime
`data/` or retired-checkout configuration is read or modified.
