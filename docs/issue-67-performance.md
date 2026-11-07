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

## PR #72 readiness coalescing paired rehearsal (2026-11-07)

Claude compared PR #72 with readiness coalescing (`e057d48c5dade1282df29d9a82e12a1511c3ead3`)
against its parent without coalescing (`eef007fc6329742a0d4608c2875c8e54ae4ed6fa`). This is an
exploratory comparison, not the issue #10 acceptance rehearsal. Both blocks used the same host,
synthetic 129,000-obligation dataset (five vehicles × 150 items, 15 crew), one uvicorn process,
and two supervisors reading readiness every 10–20 seconds. Each block used a fresh data prepare
and four runs in `head, coalesced, coalesced, head` order, each on a fresh restore; runs started
within 36 minutes of preparation. Each run lasted 300 seconds with a 60-second warm-up discarded.
The external closed-loop HTTP harness used a 30-second client timeout. Failures include every
non-2xx response (503, 409 and client timeout/status 0), and under-one-/under-two-second rates
use all requests as the denominator. No conflicts occurred. The records omit host identifiers
and session data.

| Reload multiplier | Version/run | Requests | Failures | Under 1 s / 2 s | p50 / p90 / p95 / p98 | Save p50 / p95 | Checklist p50 | Readiness success / attempts, p50 | App / DB cores | App CPU s/request |
|---:|---|---:|---:|---|---|---|---:|---|---|---:|
| 0 | Base A | 334 | 0 | 45% / 72% | 1.16 / 3.61 / 5.46 / 9.04 s | 1.05 / 3.61 s | n/a | 18 / 18, 7.8 s | 0.48 / 0.23 | 0.35 |
| 0 | Coalesced A | 336 | 1 | 47% / 82% | 1.12 / 3.20 / 5.44 / 14.5 s | 1.01 / 3.20 s | n/a | 18 / 19, 9.2 s | 0.51 / 0.26 | 0.36 |
| 0 | Coalesced B | 346 | 0 | 48% / 81% | 1.03 / 2.87 / 4.93 / 8.89 s | 0.97 / 2.71 s | n/a | 22 / 22, 6.7 s | 0.54 / 0.25 | 0.37 |
| 0 | Base B | 345 | 0 | 52% / 81% | 0.96 / 3.20 / 5.02 / 9.19 s | 0.84 / 3.10 s | n/a | 21 / 21, 6.6 s | 0.54 / 0.25 | 0.38 |
| 0.25 | Base A | 179 | 40 | 0% / 0% | 19.4 / 30.0 / 30.0 / 30.0 s | 10.3 / 17.9 s | 29.2 s | 0 / 11, 30 s | 0.93 / 0.42 | 1.25 |
| 0.25 | Coalesced A | 211 | 18 | 0% / 0% | 13.5 / 27.9 / 30.0 / 30.0 s | 8.0 / 12.5 s | 25.0 s | 0 / 12, 23.5 s | 0.93 / 0.44 | 1.05 |
| 0.25 | Coalesced B | 213 | 12 | 0% / 0% | 13.5 / 25.3 / 26.8 / 29.7 s | 8.0 / 13.3 s | 22.0 s | 0 / 12, 22.3 s | 0.93 / 0.43 | 1.05 |
| 0.25 | Base B | 195 | 21 | 0% / 0% | 14.2 / 30.0 / 30.0 / 30.0 s | 9.0 / 15.3 s | 26.9 s | 0 / 11, 30 s | 0.92 / 0.46 | 1.13 |

At zero reloads, differences were within the two base runs' spread. At multiplier 0.25, the
coalesced runs completed 8–19% more requests per paired run; the mean was 212 requests versus
187 for base (about 13% more). App CPU per request was 7–16% lower per pair, with a mean about
12% lower. None of the four runs completed a readiness panel, and all were at 0% under one or
two seconds. Thus the coalescer did not approach the latency targets. The rehearsal report says
one supervisor's first page load returned 503 in each of the four coalesced runs during cold
simultaneous setup, then succeeded on retry. Those setup requests are excluded from the table's
measured-window failure counts. At saturation, measured readiness 503s after the 20-second
follower bound replaced some 30-second client timeouts. Any future comparison must count 503s
as failures.

These are two runs per cell with 10–15% saturated-run variation, so the saturated gain is
suggestive rather than conclusive. They are not production-like 1× results: the harness is
closed-loop and external, there is no real browser/WebSocket client behavior, and no result meets
the target. PR #72 drops the coalescer because the modest, noisy overload gain does not offset
the added coordination and initial-page 503 risk. Per-read cost and reload work remain open
bottlenecks for issue #70.

## Issue #70 profiling, reload sweep and cache experiment

Claude profiled PR #72 head `eef007fc6329742a0d4608c2875c8e54ae4ed6fa` against base
`00f412dc5cd84d4a8dee5502fe295427c40938da`, using synthetic 12-month history extended to the
present (129,000 obligations/projection rows), one two-vCPU rehearsal host, MariaDB 10.11, and
one uvicorn process. The external stdlib HTTP harness ran over loopback with a 30-second client
timeout, 15 crew saving every 6–14 seconds, and two supervisors reading readiness every 10–20
seconds. Each load run used a fresh restore, discarded a 60-second warm-up, and measured 240
seconds. The driver was closed-loop; it is not the coordinated issue #10 browser/WebSocket
rehearsal. Runs varied materially, so only paired alternating runs are compared. An initial
load attempt mishandled keep-alive and cold-cache startup and is excluded.

`cProfile` samples (12 reads, approximately 1.5× profiler overhead) identified the remaining
read cost on the head: checklist p50 was 1.45 seconds/26 queries, with
`latest_effective_checks`/`_attention_roots` about 47% of profiled time and `current_rows` about
34%; readiness p50 was 1.97 seconds/21 queries, mostly Python, with `evaluate_facts` about 40%,
JSON decode plus `_restore` about 40%, and SQL about 0.5 seconds. The profiled checklist call
using explicit `as_of=now` took 2.8 seconds/42 queries, but Claude corrected the initial claim:
no production caller on the reviewed tree passes `as_of`; production current-read callers omit
it, and explicit instants are historical paths. The 2.8-second call shape is not a production
bottleneck finding.

### Reload-rate sweep on head

The reload multiplier is the probability each crew member on a vehicle reloads after a save;
1.0 represents three reloads per save. The zero-reload row is from earlier clean runs under the
same settings and is a baseline bound, not a paired point. The four sweep runs used fresh
restores from one prepared snapshot at 0, 11, 22 and 33 minutes after preparation.

| Reload multiplier | Checklist reads/240 s | Requests | Under 1 s / 2 s | Overall p50 / p90 | Save p50 / p90 | Checklist p50 | Readiness p50 | App / DB cores | App CPU s/request |
|---:|---:|---:|---|---|---|---:|---:|---|---:|
| 0 (earlier clean runs) | 0 | 336–347 | 41–49% / 71–82% | 1.0–1.2 / 3.1–3.6 s | 1.0–1.1 / 3.1–3.5 s | n/a | 4.5–9.3 s | 0.46–0.53 / 0.20–0.26 | 0.33–0.38 |
| 0.05 | 39 | 352 | 20% / 51% | 1.9 / 6.4 s | 1.7 / 4.2 s | 6.0 s | 16.2 s | 0.71 / 0.39 | 0.49 |
| 0.10 | 75 | 298 | 3% / 9% | 5.6 / 15.7 s | 4.6 / 7.6 s | 13.5 s | 26.3 s | 0.90 / 0.45 | 0.72 |
| 0.15 | 78 | 256 | 1% / 2% | 8.1 / 21.7 s | 5.9 / 10.1 s | 18.2 s | 30 s (8/10 timed out) | 0.92 / 0.46 | 0.87 |
| 0.25 | 75 | 179 | 0% / 0% | 16.3 / 30 s | 10.0 / 15.2 s | 27.2 s | 30 s (10/10 timed out) | 0.92 / 0.44 | 1.24 |

Even at multiplier 0.05, under-one-/under-two-second rates fell well short of the 95%/98%
targets. The app core saturated from multiplier 0.25 upward. At zero reloads the app used about
0.5 core; the observed profile attributed roughly 0.2 core to readiness and 0.3 to saves, so
removing checklist reloads alone cannot meet the target. At multipliers 0.5 and 1 in the earlier
sweep, respectively 33/97 and 19/58 saves returned 409 conflicts; there were none at 0 or 0.25.
These observations point to readiness and save cost as well as reload volume.

### Abandoned requests and status-change contention

A readiness read used about 3.2 app CPU seconds. Six reads abandoned by clients after a 0.5-second
timeout consumed 24.8 app CPU seconds in total, versus 19.3 seconds when allowed to finish; ten
requests abandoned in two groups kept the server busy about 45 seconds. Requests were not
cancelled when clients disconnected. A post-burst latency probe was inconclusive.

An idle status change measured p50 0.21 seconds/p95 0.68 seconds. Under three crew continuously
saving disjoint items on one vehicle, with status changes every 1.5 seconds, 150 saves had zero
failures/conflicts and p50/p95 1.7/2.5 seconds; status changes measured p50/p95 1.2/2.4 seconds
with two errors in 42. When three crew saved the same items, 271 of 407 requests saved and the
remaining 136 returned the expected HTTP 409 conflict.

### Unmerged facts-cache prototype

Claude also tested, but did not push, a bounded process-local cache of decoded projection facts
keyed by obligation ID, refresh instant, and an MD5 of facts. The digest is needed because a
same-timestamp facts change made a refresh-instant-only key stale. Five checklist digests and
the readiness digest matched the uncached head at a fixed instant. In three alternating
sequential rounds, readiness p50 moved from 3.17–3.28 seconds to 2.61–2.66 seconds (about 18%);
checklist timing was unchanged at about 2.0 seconds. After clearing cache state between query
count cases, the changed tests passed: `FanOutTests` on both head and prototype, and
`cya.obligations.tests.test_current` plus `cya.attention` (100 tests, one skipped). The clean
paired multiplier-zero load showed readiness p50 about 4.5 seconds versus 8–9 seconds and about
8% lower app CPU per request, while saves were unchanged; under-one-second rates (41–49%) were
within run spread. An earlier load comparison with many unexpected conflicts was withdrawn as
confounded by aged prepared data. No paired multiplier-0.25+ comparison was run. The prototype
is not part of PR #72, does not establish a target pass, and needs further memory-bound and
multi-process review before adoption.

The host/data were synthetic and restored after the runs; identifiers and session details are
omitted. These external-driver measurements and the prototype are exploratory evidence, not the
issue #10 acceptance run. Independent review of the recorded claims is still required.

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
