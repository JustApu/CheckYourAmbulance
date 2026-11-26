# Issue #67 verification and performance record

The [current obligation projection](current-obligation-projection.md) changes
current reads while preserving immutable events and explicit historical replay.
SPEC §17.4 permits this disposable materialization; `SPEC.md` is unchanged.

## Issue #70 latency target remains open

The issue #10 production-like 1× shift-change target, tracked in [issue #70](https://github.com/JustApu/CheckYourAmbulance/issues/70), is **not met** on the reviewer-reported rehearsal run before the status-lock fix. Only 2.8% of requests completed under one second and 3.1% under two seconds, below the owner's 95%/98% targets.
The sample and its limitations are recorded below; no 4×/10× run is claimed.
A later measurement on the current `main` after issues #73 to #75 and the worker-count test of 2026-11-09 are recorded in
[their own section](#net-benchmark-and-worker-count-test-after-issues-73-to-75-2026-11-09); the targets are still **not met**.
The 2026-11-22 Rocky rehearsal run, with direct per-operation measurements, is recorded in
[its own section](#rocky-rehearsal-1-run-with-direct-measurements-2026-11-22); the targets are still **not met**.
The 2026-11-25 re-run after issue #278 Parts A–C, with the database working set and new direct measurements, is recorded in
[its own section](#issue-278-part-e-1-re-run-after-parts-ac-2026-11-25); the targets are still **not met**.
The re-run with issue #278 Parts F–H is recorded in
[its own section](#issue-278-parts-fh-1-re-run-2026-11-26); the targets are still **not met**.
The runs with the readiness floor, several processes and pushed rows are recorded in
[their own section](#issue-278-several-processes-the-readiness-floor-and-pushed-rows-2026-11-26); the targets are still **not met**.
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

## Issue #278: several processes, the readiness floor and pushed rows (2026-11-26)

Claude ran the same 1× scenario on the Rocky rehearsal host (4 cores) on 2026-11-26. The [Parts F–H record](#issue-278-parts-fh-1-re-run-2026-11-26) describes the setup, and these runs kept it: the same base snapshot restored before every step, the same 1× step and client settings, synthetic data only, and one host. The results are directional. The 1× targets are still **not met**.

**What changed between runs.**
- **10-second floor:** [PR #291](https://github.com/JustApu/CheckYourAmbulance/pull/291), SPEC 8.1.0. The readiness screen re-reads in response to pushes at most every 10 s.
- **Several processes:** [PR #294](https://github.com/JustApu/CheckYourAmbulance/pull/294), SPEC 8.2.0, a Valkey shared layer.
- **Pushed rows:** [PR #296](https://github.com/JustApu/CheckYourAmbulance/pull/296), SPEC 8.3.0, the changed rows in the check push. It was **in review** when these runs were made, in three versions:
  - **S:** rows read inside the save.
  - **F:** S, plus rows only for sessions before their end.
  - **G:** F, with rows read off the request path.
- **Releases:**
  - The **R** runs used `81bd69b` with the files of Parts F–H and #291.
  - The **C** runs are `main` at `44783fe`, which has #291 and #294 but not #296.
  - The **S, F and G** runs are that `main` merged with #296 at the version named.
- **Channel layer:** 3-worker steps on `main` used the Valkey shared layer, so every push reached every client. Earlier multi-worker steps, including **R**, used the in-memory layer, which does not cross processes. Every 1-worker step used the in-memory layer.

**Results** (seconds, client-side wall time; pages and saves together):

| Step | Release | Workers (layer) | Saves in window | p50 / p95 | Under 1 s / under 2 s | Save p50 / p95 | Row reads | Panel reads | Pushes received | App / DB cores |
|---|---|---|---|---|---|---|---:|---:|---:|---|
| R1a | F–H + floor | 1 (memory) | 631 of 750 (600 s) | 7.40 / 10.80 | 1.5% / 2.4% | 7.51 / 10.80 | 1,635 | 59 | 2,659 | 1.03 / 0.67 |
| R1b | F–H + floor | 1 (memory) | 623 of 750 (600 s) | 7.62 / 11.12 | 1.6% / 2.7% | 7.77 / 11.12 | 1,631 | 68 | 2,622 | 1.04 / 0.56 |
| R3a | F–H + floor | 3 (memory) | 750 (360 s) | 0.61 / 2.31 | 74.7% / 93.1% | 0.62 / 2.26 | 1,151 | 75 | 1,024 | 1.37 / 1.13 |
| R3b | F–H + floor | 3 (memory) | 750 (362 s) | 0.63 / 2.59 | 69.6% / 90.9% | 0.65 / 2.52 | 1,153 | 74 | 1,037 | 1.42 / 1.12 |
| C1a | `main` | 1 (memory) | 603 of 750 (600 s) | 8.15 / 11.36 | 1.5% / 2.9% | 8.26 / 11.36 | 1,581 | 66 | 2,532 | 1.04 / 0.51 |
| C3a | `main` | 3 (Valkey) | 750 (362 s) | 0.68 / 2.32 | 67.9% / 92.2% | 0.69 / 2.30 | 1,514 | 82 | 2,233 | 1.51 / 0.80 |
| S1a | + #296 S | 1 (memory) | 420 of 750 (600 s) | 13.67 / 17.47 | 7.1% / 8.3% | 13.78 / 17.47 | 323 | 79 | 3,038 | 1.03 / 0.47 |
| S1b | + #296 S | 1 (memory) | 427 of 750 (600 s) | 13.57 / 16.58 | 8.7% / 8.9% | 13.71 / 16.61 | 322 | 79 | 3,085 | 1.03 / 0.47 |
| S1c | + #296 S | 1 (memory) | 434 of 750 (600 s) | 13.35 / 16.38 | 7.8% / 9.8% | 13.50 / 16.38 | 345 | 82 | 3,135 | 1.03 / 0.48 |
| S3a | + #296 S | 3 (Valkey) | 750 (447 s) | 2.19 / 4.75 | 5.8% / 43.4% | 2.24 / 4.77 | 552 | 103 | 3,564 | 1.83 / 0.92 |
| S3b | + #296 S | 3 (Valkey) | 750 (440 s) | 2.22 / 4.52 | 5.4% / 43.8% | 2.33 / 4.56 | 567 | 99 | 3,887 | 1.90 / 0.90 |
| F1a | + #296 F | 1 (memory) | 710 of 750 (600 s) | 6.08 / 9.09 | 4.8% / 6.1% | 6.18 / 9.11 | 549 | 107 | 5,115 | 1.02 / 0.51 |
| F3a | + #296 F | 3 (Valkey) | 750 (372 s) | 0.87 / 2.67 | 56.6% / 87.7% | 0.90 / 2.64 | 563 | 81 | 3,569 | 1.42 / 0.72 |
| G1a | + #296 G | 1 (memory) | 634 of 750 (600 s) | 7.60 / 10.71 | 1.6% / 2.5% | 7.71 / 10.73 | 1,351 | 70 | 3,244 | 1.04 / 0.50 |
| G3a | + #296 G | 3 (Valkey) | 750 (358 s) | **0.58 / 2.12** | **74.9% / 94.6%** | 0.59 / 2.04 | 103 | 80 | 4,708 | 1.23 / 0.65 |
| G3b | + #296 G | 3 (Valkey) | 750 (352 s) | **0.55 / 2.16** | **78.8% / 93.8%** | 0.56 / 2.12 | 164 | 78 | 4,410 | 1.33 / 0.73 |

The S, F and G versions also count each pushed-rows frame as a push. The 1-worker steps with errors (S1a–S1c 26–30, F1a 29) had save attempts that got no answer. They were retried, as in the [Parts F–H record](#issue-278-parts-fh-1-re-run-2026-11-26), and those saves all landed.

**Where one process's CPU goes.** For several 1-worker steps, the application ran under a per-request CPU timer that wraps the outermost middleware and adds the thread CPU of each request. Its overhead is two clock reads per request. Requests and the event loop thread (WebSockets and pushes) accounted for about 98% of the process CPU, except in G1a (93%), where most of the rest was the rows thread.

| Step | Row reads | Panel reads | Saves | Full reads | Event loop | Other |
|---|---|---|---|---|---|---|
| F–H, 1 worker (no floor; one step) | 253 s (1,680 × 0.15 s) | 159 s (60 × 2.65 s) | 139 s (641 × 0.22 s) | 26 s | 24 s | 2% |
| C1a | 244 s (1,593 × 0.15 s) | 177 s (68 × 2.60 s) | 140 s (613 × 0.23 s) | 25 s | 23 s | 2% |
| S1c | 53 s (348 × 0.15 s) | 193 s (84 × 2.30 s) | 322 s (443 × 0.73 s) | 23 s | 17 s | 1% |
| G1a | 214 s (1,363 × 0.16 s) | 167 s (72 × 2.31 s) | 148 s (641 × 0.23 s) | 22 s | 24 s | 45 s, mostly the rows thread |

Counts here are requests seen by the server; the results table counts them at the client.

**Reading the results.**
- **The floor caps panel reads where reads are fast.** At 3 workers, panel reads fell from 110–114 per step (Parts F–H record) to 74–75, and application CPU from 1.57–1.59 to 1.37–1.42 cores. At 1 worker a panel read already takes longer than 10 s, so the floor never applies.
- **Several processes are now a supported configuration** (SPEC 8.2.0). With Valkey, every push reaches every client: 2,233 pushes against about 1,030 with the in-memory layer, which drops pushes across processes. Even so, C3a matched the in-memory R3 steps (67.9% / 92.2% under 1 s / 2 s).
- **Pushed rows: version S.** It made everything worse. A direct profile on the same data found that the rehearsal snapshot holds sessions still marked OPEN after their end instant, because their session job had not run. So each save read the item's rows five times: 0.34–0.44 s of CPU and 124 queries, against 0.08 s and 23 for one session. Saves rose to 0.73 s of CPU. A deployment whose session job lags could see the same, so F sends rows only to sessions before their end.
- **Pushed rows: version F.** It helped 1 worker (p50 8.15 to 6.08 s, 603 to 710 saves) but hurt 3 workers (under 1 s from 67.9% to 56.6%). Reading rows inside the save delayed every save's answer where CPU was not the limit.
- **Pushed rows: version G.** It reads the rows on one background thread per process, which sends the rows and then the hint. This gave the best results of issue #278:
  - at 3 workers, 74.9–78.8% under 1 s and 93.8–94.6% under 2 s, with save p50 0.56–0.59 s;
  - row reads fell about 90%, to 103–164;
  - application CPU fell to 1.23–1.33 cores.
  At 1 worker G is close to `main` (p50 7.60 against 8.15 s). The single process is CPU-bound, so the rows thread competes with requests, and the saver's 1-second wait for its own rows often runs out.
- **The targets are still not met.** The best step reached 78.8% under 1 s and 94.6% under 2 s; the targets are 95% and 98%. A single process remains far from them: CPU demand at this load is about two cores. The readiness panel is now the largest remaining per-request cost, at 2.3–2.6 s of CPU each. Making it cheaper would need caching each obligation's evaluated state until its next time boundary, which was not attempted. Narrowing the RPT-5a candidate read by item made no measurable difference and was dropped.

**Deviations, recorded so the runs can be judged.**
- *Releases.* The R runs and the C, S, F and G runs differ in about 60 unrelated commits on `main`. C1a is slightly slower than R1 (p50 8.15 against 7.40–7.62 s), so those commits do not change the picture.
- *A false start.* The first Valkey attempt ran one 3-worker step without the store, which was discarded. Another drop-in on the host's service resets `EnvironmentFile=` and sorts after the one that named the store. Valkey had received only 66 commands. The store setting then went in a drop-in that sorts last, Valkey's counters confirmed use in the first later 3-worker step (about 100 commands a second from 12–13 connections). In every Valkey step the push counts confirm it: 2,233–4,708, against about 1,030 without a shared layer.
- *Copied environments.* The rehearsal releases reuse a copied virtual environment whose `uvicorn` script runs an older release's Python, so these runs started `python -m uvicorn` instead. The application code and pinned packages were those of each release.
- *Earlier profiling attempts.* Stack-sampling profilers built from Python, at 10 ms and at 50 ms, disturbed the 1-worker process (up to 61 failed saves and 50-second lock waits) and are not used for any figure here. Only the per-request timer above is. The 50-second waits were MariaDB's lock-wait timeout, reached when the slowed process held a save's locks. That did not happen in any unprofiled step.
- *Settle gate, client placement, time limit.* As in Part E.

**Not claimed:** a worker count for production, any 4×/10× step, Ubuntu results, behaviour of #296 after review changes, or any statistical statement.

## Issue #278 Parts F–H: 1× re-run (2026-11-26)

Claude re-ran the [Part E](#issue-278-part-e-1-re-run-after-parts-ac-2026-11-25) scenario on the same Rocky rehearsal
host with three more changes from [issue #278](https://github.com/JustApu/CheckYourAmbulance/issues/278):
- Part F ([PR #286](https://github.com/JustApu/CheckYourAmbulance/pull/286)): a save reads its obligations once.
- Part G ([PR #288](https://github.com/JustApu/CheckYourAmbulance/pull/288)): checklist reads batch the newest-check
  lookups and resolve the checklist once.
- Part H ([PR #289](https://github.com/JustApu/CheckYourAmbulance/pull/289)): a one-row read reads only that item's
  obligations and deficiencies.

The release was Part E's `81bd69b` with the changed files of `0718e39` (Part F) and `ecb7244` (Parts G and H, stacked).
Their checksums matched those commits. Parts G and H were not yet merged when the run took place. The arms ran from
2026-11-25 23:41 to 2026-11-26 02:07 UTC.

Everything else was as in Part E: the same base snapshot (111,884 buffer-pool pages after each restore, as in every
Part E arm) and the same 1× step. It used the same client settings, A/B/C/C/B/A order, synthetic data only and one
host, so the results are directional. The run does **not** satisfy issue #70's 1× acceptance: the targets are still
**not met**.

**Results** (seconds, client-side wall time):

| Arm | Workers | Saves in window | Pages and saves p50 / p95 | Under 1 s / under 2 s | Live reload p50 / p95 | Row reads p50 (count) | Full checklist reads p50 (count) | Panel p50 (count) | Readiness page p50 | App / DB cores | App memory peak |
|---|---:|---|---|---|---|---|---|---|---|---|---|
| W1a | 1 | 629 of 750 (600 s, stopped at the limit) | 7.5 / 10.9 | 1.3% / 2.4% | 4.8 / 7.1 | 4.7 (1,618) | 8.2 (33) | 22.0 (54) | 22.9 | 1.03 / 0.66 | 325 MB |
| W2a | 2 | 750 (393 s) | 1.2 / 3.3 | 40.6% / 76.1% | 0.7 / 5.5 | 0.66 (1,362) | 4.2 (30) | 9.4 (87) | 6.1 | 1.52 / 1.20 | 480 MB |
| W3a | 3 | 750 (361 s) | 0.65 / 2.3 | 70.9% / 92.7% | 0.34 / 4.0 | 0.31 (1,169) | 2.8 (30) | 3.9 (110) | 5.4 | 1.59 / 1.25 | 621 MB |
| W3b | 3 | 750 (369 s) | 0.71 / 2.5 | 67.5% / 91.9% | 0.36 / 4.1 | 0.32 (1,152) | 2.8 (30) | 4.1 (114) | 4.4 | 1.57 / 1.18 | 629 MB |
| W2b | 2 | 750 (393 s) | 1.1 / 3.5 | 44.5% / 75.5% | 0.69 / 5.0 | 0.63 (1,380) | 3.7 (30) | 6.2 (94) | 17.3 | 1.52 / 1.18 | 516 MB |
| W1b | 1 | 614 of 750 (600 s, stopped at the limit) | 7.8 / 11.0 | 1.2% / 2.0% | 4.8 / 7.5 | 4.8 (1,587) | 9.1 (30) | 17.5 (54) | 28.5 | 1.03 / 0.63 | 386 MB |

Both supervisors started in every arm. In Part E's 1-worker arms, each supervisor's first readiness page hit the
reverse proxy's 60 s timeout. Here the readiness page took about 23 to 32 s at 1 worker (two loads per arm), under that
limit. Swap stayed at 1,548 to 7,728 KB, and the highest 5-second I/O-wait sample was 36% (W3a).

**Save attempts with no answer.** The client recorded 7 to 16 save attempts per 2- and 3-worker arm that got no answer
(status 0), and 3 to 10 row reads. Each such save was retried, and every arm saved all 750.

The proxy's access log for each of those four arms holds exactly 750 `POST /api/v1/checks` requests, all `201`. It has
no `5xx` and no `499` inside the arms, so the failed attempts never reached the proxy. The cause is on the client's
side, most likely a pooled connection that failed when reused; it was not diagnosed further. The client ran on the
application host, as in Part E. Part E's W3a had one such save, and the 1-worker arms had none in either run. These
attempts are included in the percentiles above.

**Database working set.** Counters sampled after each arm's preparation and at its end:

| Workers | Pool pages holding data, before → after (of 194,688) | Pages read from disk during the arm | Page read requests | Served from memory |
|---|---|---|---|---|
| 1 | 148,455–148,647 → 175,170–176,000 | 12,946–13,253 | 55.9–58.5 million | over 99.97% |
| 2 | 148,501–148,605 → 179,135–179,697 | 15,306–15,385 | 73.6–75.0 million | over 99.97% |
| 3 | 148,491–148,678 → 179,216–179,408 | 15,238–15,278 | 73.8–75.1 million | over 99.97% |

Page read requests per arm fell by about a third at 2 and 3 workers (from 113–117 million in Part E). The 1-worker arms
made more page read requests than in Part E (55.9–58.5 against 50.6–51.2 million) because they completed nearly twice as many saves.

**Against Part E** (same host, data, snapshot and client; only the release changed):

| Workers | Pages and saves p50 | Under 1 s | Under 2 s | Pages and saves p95 | Saves in window | App cores |
|---:|---|---|---|---|---|---|
| 1 | 16.6–17.5 → 7.5–7.8 | 1.8–2.3% → 1.2–1.3% | 2.8–2.9% → 2.0–2.4% | 21.5–22.3 → 10.9–11.0 | 338–348 → 614–629 of 750 | 1.04–1.05 → 1.03 |
| 2 | 7.4–7.6 → 1.1–1.2 | 1.9–2.5% → 40.6–44.5% | 5.1% → 75.5–76.1% | 11.8–12.0 → 3.3–3.5 | 623–635 → all 750 (393 s) | 1.82 → 1.52 |
| 3 | 3.5 → 0.65–0.71 | 6.5–7.4% → 67.5–70.9% | 18.3–20.2% → 91.9–92.7% | 6.9–7.4 → 2.3–2.5 | all 750: 514 s → 361–369 s | 2.16–2.17 → 1.57–1.59 |

**Reading the results.**
- **The targets are not met.** The target is at least 95% of page loads and saves under 1 s and at least 98% under
  2 s, with a stop before higher steps if p90 exceeds 3 s. The best arms had 67.5–70.9% under 1 s and 91.9–92.7% under
  2 s, with p90 at 1.8 s.
- **The pairs agree.** Pages-and-saves p50 differed by 0.06 s between the 3-worker arms, 0.08 s between the 2-worker
  arms and 0.3 s between the 1-worker arms.
- **The 1-worker arms are still saturated.** The application used 1.03 cores at 1 worker, so a single process is CPU
  bound at this load. It did nearly twice the work in the window, but its latency stays far from the targets.
  Under the shipped in-memory channel layer, a single process is the only deployable configuration (SPEC DEP-2), so
  the deployable configuration remains far from the targets.
- **At 2 and 3 workers the application is no longer the only limit.** The application used about 1.5–1.6 cores, below
  the 2.2 cores of Part E's 3-worker arms, and finished all 750 saves in 6 to 6.5 minutes. The database used
  1.2–1.25 cores. This run does not show which resource limits the 3-worker arms; the load client shares the same four
  cores.
- **Row reads now cost little.** Single-row reads had p50 0.31–0.32 s at 3 workers, against 3.6–3.8 s in Part E.
  Full checklist reads (one per crew member at start) had p50 2.8 s, against 5.7–8.0 s.
- **Readiness is now the slowest read.** Panel p50 was 3.9–4.1 s at 3 workers and 17.5–22.0 s at 1 worker. A warm panel
  read costs about 1.4–1.7 s of CPU with no load (Part E direct measurements), and Parts F–H do not change it. Part D (a
  per-vehicle readiness cache) remains open for the owner to decide.

**Deviations, recorded so the run can be judged.**
- *First attempt.* The arms were first started as a transient systemd unit. SELinux denied that unit read access to the
  snapshot, so every arm's restore failed within seconds and nothing was measured. The run was restarted from an
  interactive root shell, as Part E's was. Each arm then restored the full snapshot.
- *Settle gate, client placement, time limit.* As in Part E: every arm waited the gate's full 10 minutes
  (`quiet_samples=0`). The client ran on the application host. The 1-worker arms stopped at the 600 s limit with saves
  still to do, and their percentiles cover the requests completed by then.
- *No direct measurements.* Per-operation measurements with no load were taken on this release while profiling Parts G
  and H (see #288 and #289); they were not repeated after the arms.
- *Working set.* Only the global buffer-pool counters, as in Part E.

**Not claimed:** a worker count, a hosting layout or a channel layer (SPEC DEP-2 and section 17.7 item 7 remain owner
decisions), any 4×/10× step, the cause of the unanswered save attempts, or any statistical statement.

## Issue #278 Part E: 1× re-run after Parts A–C (2026-11-25)

Claude re-ran the issue #10 1× scenario on the Rocky rehearsal host (4 cores, 7.5 GB) with
[issue #278](https://github.com/JustApu/CheckYourAmbulance/issues/278) Parts A–C merged. Part A skips a page's own
pushes, Part B re-reads only the changed checklist row, and Part C caps the readiness push window at 2 s and refreshes
every 20 s. The release was `main` at `81bd69b`. Arms ran at 1, 2 and 3 uvicorn workers in A/B/C/C/B/A order, with
synthetic data only, two runs per worker count and one host, so the results are directional. The run does **not**
satisfy issue #70's 1× acceptance: the targets are still **not met**.

**Setup.**
- **Same as 2026-11-22:** MariaDB 10.11 with the host's durable settings, a 3 GB buffer pool dumped and reloaded in
  full, and a cold copy of the whole data directory restored before each arm. Each arm used a test-only drop-in for the
  worker count and a freshly prepared 1× step (five vehicles × 150 items, 15 crew, two supervisors). The push-driven
  client ran with think time 3 to 8 s, arrivals over 30 s, the shipped page's reload after a committed save, a 600 s
  limit, seed 10 and the default 90 s request timeout. The channel layer is the shipped in-memory layer, so the 2- and
  3-worker arms do not deliver live updates across workers and are not a deployable configuration (SPEC DEP-2).
- **New data.** The 2026-11-22 copy was extended to 2026-11-24 with the old release first. It was then migrated with
  `main` through the SPEC 5.0.0 vehicle-checklist, 6.0.0 item-requirement and 7.0.0 kit-assignment cutovers. A full
  projection rebuild of 194,250 obligations then reported 0 differences. So this run measures Parts A–C together with
  the 5.0.0–7.0.0 code and data shape, not Parts A–C alone.
- **New client behaviour (Parts A and B):** a crew member skips their own save's push, and a check push re-reads only
  that row (`item_ids`). Panel reads coalesce within 2 s.

**Results** (seconds, client-side wall time):

| Arm | Workers | Saves in window | Pages and saves p50 / p95 | Under 1 s / under 2 s | Live reload p50 / p95 | Row reads p50 (count) | Full checklist reads p50 (count) | Panel p50 (count) | App / DB cores | App memory peak |
|---|---:|---|---|---|---|---|---|---|---|---|
| W1a | 1 | 338 of 750 (600 s, stopped at the limit) | 17.5 / 22.3 | 2.3% / 2.9% | 14.3 / 24.5 | 14.1 (544) | 34.4 (30) | none: both supervisors failed to start | 1.05 / 0.49 | 425 MB |
| W2a | 2 | 635 of 750 (600 s, stopped at the limit) | 7.4 / 11.8 | 1.9% / 5.1% | 7.4 / 13.3 | 7.2 (986) | 12.2 (30) | 24.5 (47) | 1.82 / 1.09 | 593 MB |
| W3a | 3 | 750 (514 s) | 3.5 / 7.4 | 7.4% / 20.2% | 4.0 / 9.5 | 3.8 (1,093) | 8.0 (30) | 17.6 (56) | 2.17 / 1.31 | 757 MB |
| W3b | 3 | 750 (514 s) | 3.5 / 6.9 | 6.5% / 18.3% | 3.7 / 8.8 | 3.6 (1,119) | 5.7 (30) | 16.5 (65) | 2.16 / 1.32 | 717 MB |
| W2b | 2 | 623 of 750 (600 s, stopped at the limit) | 7.6 / 12.0 | 2.5% / 5.1% | 7.4 / 13.6 | 7.1 (988) | 11.9 (30) | 26.6 (45) | 1.82 / 1.08 | 594 MB |
| W1b | 1 | 348 of 750 (600 s, stopped at the limit) | 16.6 / 21.5 | 1.8% / 2.8% | 13.9 / 19.5 | 13.8 (570) | 30.9 (30) | none: both supervisors failed to start | 1.04 / 0.50 | 370 MB |

In both 1-worker arms, each supervisor's first readiness page ended at the reverse proxy's 60 s read timeout, so no
supervisor ran in those arms. W3a recorded one save that did not get an answer and was retried. No other save failed
in any arm. Swap stayed at 524 to 1,548 KB, and the highest 5-second I/O-wait sample was 20%.

**Database working set.** Buffer-pool counters were sampled after each arm's preparation and again at its end:

| Workers | Pool pages holding data, before → after (of 194,688) | Pages read from disk during the arm | Page read requests | Served from memory |
|---|---|---|---|---|
| 1 | 148,529–148,545 → 166,744–167,237 | 7,845–7,978 (about 125 MB) | 50.6–51.2 million | over 99.98% |
| 2 | 148,524–148,772 → 176,662–176,925 | 13,257–13,387 (about 210 MB) | 113.2–115.5 million | over 99.98% |
| 3 | 148,313–148,773 → 180,270–180,766 | 15,342–15,390 (about 245 MB) | 115.8–117.2 million | over 99.98% |

With a 3 GB pool on this data, the database read almost nothing from disk, and the pool ended 86–93% full. No
smaller pool was tried, and the per-table breakdown in the Part E plan was not collected. So this supports "3 GB is
enough for this data", not a sizing rule.

**Direct measurements.** After the arms, the base snapshot was restored, the service was stopped and one 1× step was
prepared. One process then called the real functions with no other load, through `manage.py shell` as the schema owner
(on 2026-11-22 it was the runtime account). This was done twice: once on `81bd69b`, and once on `81bd69b` plus PR #286
(Part F, a save reads its obligations once). The second run restored the snapshot again. SQL query counts are from
Django's query capture; CPU is the process's own.

| Operation | `81bd69b`: wall (CPU) | Queries | With PR #286: wall (CPU) | Queries | 2026-11-22 (`cb45f27`), wall | Queries |
|---|---|---:|---|---:|---|---:|
| Readiness panel, five vehicles, first call | 7.91 s (5.67 s) | 44 | 8.03 s (5.57 s) | 44 | — | — |
| Readiness panel, five vehicles, warm (5 reads) | 2.14 to 3.15 s (1.43 to 2.17 s) | 31 to 33 | 2.17 to 3.31 s (1.41 to 2.32 s) | 31 to 33 | 7.94 to 8.37 s (`_picture`) | 43 to 44 |
| Vehicle checklist, 150 rows (4 reads) | 0.96 to 1.16 s (0.59 to 0.80 s) | 206 | 0.99 to 1.26 s (0.64 to 0.87 s) | 206 | 0.53 to 0.71 s | 52 to 53 |
| One checklist row (`item_ids`, 4 reads) | 0.51 to 0.71 s (0.30 to 0.49 s) | 57 | 0.52 to 0.72 s (0.31 to 0.51 s) | 57 | — | — |
| One routine check save, no session (8 saves) | 0.34 to 0.80 s (0.19 to 0.22 s) | 58 | 0.27 to 0.76 s (0.13 to 0.16 s) | 52 | 0.24 to 0.30 s after the first | 51 |

**Reading the results.**
- **The targets are not met.** The target is at least 95% of page loads and saves under 1 s and at least 98% under 2 s,
  with a stop before higher steps if p90 exceeds 3 s. The best arm had 7.4% under 1 s and 20.2% under 2 s, with p95 near
  7 s.
- **The pairs agree.** Pages-and-saves p50 differed by 0.0 s between the 3-worker arms, 0.2 s between the 2-worker arms
  and 0.8 s between the 1-worker arms. On 2026-11-22 the 1-worker arms differed by 6.6 s.
- **The application CPU is still the limit.** Application cores rose with the workers (about 1.0, 1.8 and 2.2) while
  the database used 0.5 to 1.3 cores and almost never read the disk.
- **Against 2026-11-22.** At 3 workers, pages-and-saves p50 fell from 4.2–4.3 s to 3.5 s, and the share under 2 s rose
  from 12–13% to 18–20%. Live reloads p50 fell from about 9 s to 3.7–4.0 s, and all 750 saves finished in 514 s instead
  of 555–575 s. At 1 and 2 workers the run was no better: 1-worker p50 was 16.6–17.5 s against 8.8–15.4 s, and fewer
  saves fitted in the window at 2 workers (623–635 against 675–710). The code and data both changed between the runs,
  so this does not isolate Parts A–C.
- **Part B cut full reads but not their cost.** Full checklist reads fell to 30 per arm, one per crew member at start.
  Instead, about 1.5 single-row reads ran per save (544 to 1,119 per arm). A single-row read still costs about 0.3 s of
  CPU and 57 queries, roughly half a full read, so most of a read's cost does not depend on how many rows it returns.
- **Checklist reads now cost more than saves.** Per save, about 1.5 row reads at 0.3 s of CPU outweigh the save's own
  0.19 s (0.13 s with PR #286). The full read rose from 52 to 206 queries and from about 0.6 s to about 1 s since
  2026-11-22, so the 5.0.0–7.0.0 read path has more queries per row. That is the next thing to profile.
- **Readiness is slower on the migrated data.** Warm panel reads take 2.1 to 3.3 s (1.4 to 2.3 s of CPU) with no load,
  and 16.5 to 26.6 s p50 under load. Part D (a per-vehicle readiness cache) was shelved after a measurement on the
  pre-migration data showed about 0.5 s. That premise no longer holds, so Part D is open again for the owner to decide.
- **Part F (PR #286)** removes about a third of a save's CPU (0.19 s to 0.13 s, 58 to 52 queries). Its effect under load
  has not been measured.

**Deviations, recorded so the run can be judged.**
- *Settle gate.* The gate required six quiet 5-second samples with no swap. Swap in use from earlier work kept every
  arm waiting the gate's full 10 minutes (`quiet_samples=0`). The arms are consistent with each other.
- *Client placement.* The load client ran on the application host itself, over HTTPS to the public name, sharing its
  four cores, as on 2026-11-22. `MemoryMax` was raised to 5 GB for the arms; peak memory stayed under 0.8 GB.
- *Time limit.* Every 1- and 2-worker arm stopped at the 600 s limit with saves still to do. Their percentiles cover
  the requests completed by then.
- *Readiness timeouts.* The 1-worker arms ran with no supervisor load (see above), so they are lighter than the 2- and
  3-worker arms.
- *Order.* The arms ran in W1a, W2a, W3a, W3b, W2b, W1b order.
- *Working set.* Per-table pool pages were not sampled; only the global counters above.
- *Direct measurements.* These ran as the schema owner rather than the runtime account. Earlier profiling on this host
  had bound extra daily schedules, so the snapshot was restored before each of the two measured runs and only one 1×
  step was prepared, as in each arm.
- *Load steps leave a slot bound.* As on 2026-11-22, `load_prepare` does not unbind its slot. The per-arm restores make
  this harmless here.

**Not claimed:** a worker count, a hosting layout or a channel layer (SPEC DEP-2 and section 17.7 item 7 remain owner
decisions), any 4×/10× step, Part F's effect under load, or any statistical statement.

## Rocky rehearsal 1× run with direct measurements (2026-11-22)

Claude ran the issue #10 1× scenario on the Rocky rehearsal host after it was resized to 4 cores and 8 GB, at 1, 2 and 3
uvicorn workers in A/B/B/A order. The release was `main` at `cb45f27` plus this branch's load-client and history-extension
commits; the application code under test is `main`'s. Synthetic data only; two runs per worker count, one host,
directional. It does **not** satisfy issue #70's 1× acceptance; it records that the targets are **not met** on this host.

**Setup.** MariaDB 10.11 with `innodb_flush_log_at_trx_commit=1` and binary logging on (the host's durable settings). The
synthetic history was extended from its last day to 2026-11-22 with the new `extend` step, then cleaned and verified (see
the deviations below). At each arm's start the database held 161,250 obligations and projection rows for five vehicles
(1,350 per vehicle in the readiness window). Each arm restored the same cold snapshot of the whole data directory, waited for
the buffer pool to load, started the application with a test-only drop-in setting the worker count, prepared a fresh 1× step
(five vehicles × 150 items, 15 crew, two supervisors) and ran the push-driven client: think time 3 to 8 s, arrivals over
30 s, the shipped page's reload after a committed save, a 600 s limit and seed 10. The channel layer is the shipped
in-memory layer, so as in the 2026-11-09 W3m arms the 2- and 3-worker arms do not deliver live updates across workers and
are not a deployable configuration.

**Results** (seconds, client-side wall time):

| Arm | Workers | Saves in window | Pages and saves p50 / p95 | Under 1 s / under 2 s | Live reload p50 / p95 | Supervisor readiness p50 (errors of 2) | App / DB cores | App memory peak |
|---|---:|---|---|---|---|---|---|---|
| W1a | 1 | 349 of 750 (600 s, stopped at the limit) | 8.8 / 17.9 | 1.8% / 2.0% | 13.7 / 35.5 | 60.0 (2) | 1.03 / 0.42 | 587 MB |
| W2a | 2 | 675 of 750 (600 s, stopped at the limit) | 6.5 / 11.0 | 2.6% / 5.5% | 11.6 / 17.8 | 56.0 (1) | 1.87 / 0.87 | 726 MB |
| W3a | 3 | 750 (575 s) | 4.2 / 8.9 | 5.3% / 12.3% | 8.9 / 16.1 | 28.7 (0) | 2.29 / 1.09 | 888 MB |
| W3b | 3 | 750 (555 s) | 4.3 / 8.1 | 4.0% / 13.2% | 9.0 / 16.8 | 46.3 (0) | 2.37 / 1.10 | 938 MB |
| W2b | 2 | 710 of 750 (600 s, stopped at the limit) | 5.8 / 10.0 | 1.3% / 3.7% | 10.6 / 16.2 | 60.0 (2) | 1.88 / 0.78 | 728 MB |
| W1b | 1 | 362 of 750 (600 s, stopped at the limit) | 15.4 / 22.7 | 1.0% / 2.2% | 21.7 / 34.2 | 60.0 (2) | 1.05 / 0.39 | 550 MB |

The supervisor readiness errors are the reverse proxy's 60 s read timeout (`proxy_read_timeout` on the test VM): each
failed request ended at 60.03 to 60.04 s in the run reports. The client then had no timeout of its own. A supervisor whose
first readiness page failed did not start. The client now also bounds each connection open and request
(`--request-timeout`, default 90 s), so a site that never answers is recorded as no answer and cannot keep a run from
reporting; with the default, this proxy's 60 s answer is still what gets recorded. The client recorded no save errors in any arm. Swap stayed at 268 to 524 KB and the highest 5-second I/O-wait sample
was 11%.

**Direct measurements.** After the arms, the snapshot was restored and the service stopped. One process then called the
real functions directly with no other load, through `manage.py shell` under the runtime account. SQL query counts are from
Django's query capture:

| Operation | Wall time | SQL queries |
|---|---|---:|
| Fleet readiness, five vehicles (`_picture`) | 7.94 to 8.37 s (3 reads), 8.21 s after the saves | 43 to 44 |
| Vehicle checklist, 150 rows | 0.53 to 0.71 s (3 reads) | 52 to 53 |
| One routine check save (`record_check`, no session) | 0.60 s first, then 0.24 to 0.30 s (5 saves) | 51 |
| Six crew saving at the same instant on one vehicle (threads) | 0.91 to 0.98 s each, 0.99 s wall, no errors | — |
| Six crew saving at the same instant across five vehicles | 0.88 to 0.95 s each, 0.96 s wall, no errors | — |
| Vehicle status change (`set_vehicle_status`) | 0.27 s and 0.18 s | 56 |

**Reading the results.**
- **The targets are not met.** The target is at least 95% of page loads and saves under 1 s and at least 98% under 2 s,
  with a stop before higher steps if p90 exceeds 3 s. The best arm had 5.3% under 1 s and 13.2% under 2 s, and p90 above 7 s.
- **The A/B/B/A pairs agree.** Pages-and-saves p50 differed by 0.1 s between the two 3-worker arms and by 0.7 s between
  the 2-worker arms. The 1-worker arms varied more (8.8 against 15.4 s), as expected when a single worker is saturated.
- **The application CPU is the limit.** Application cores rose with the workers (about 1.0, 1.9 and 2.3 to 2.4), while
  the database used 0.4 to 1.1 cores and the disk was not busy. At 3 workers the run finished all 750 saves in 555 to 575 s, about
  1.3 to 1.35 saves per second.
- **The live reload multiplies the checklist cost.** Each save makes every crew member on that vehicle reload the
  150-row checklist (about 0.5 s of CPU each): 836 to 874 checklist reloads per 750 saves at 3 workers. With the save
  itself (about 0.25 s) that is roughly 1.7 CPU-seconds per save, which matches the measured cores.
- **Fleet readiness is the slowest read.** It takes about 8 s with only 43 queries, so the time is spent in Python
  rather than in SQL. This is much slower than the 0.18 s measured locally on nine generated days; the difference
  (the 149-day history, the overdue and missed state this cleaned copy has, or the host) has not been isolated. The
  2026-11-09 run's readiness p50 was 33 to 106 s under load, so the issue predates this data.
- **Saves and status changes are not the bottleneck.** A single save takes 0.25 s. Six simultaneous saves on one vehicle
  took the same time as six spread over five vehicles. This contradicts the hypothesis of same-vehicle lock contention; the
  extra time is shared CPU in one process. A status change takes 0.2 to 0.3 s.

**Deviations, recorded so the run can be judged.**
- *Leftover load schedules.* Earlier load steps (2026-11-03 to 11-11) left seven daily slots bound. Projected forward,
  they gave each vehicle about 10,800 rows in the readiness window, over the 10,000-row per-vehicle bound. Readiness and
  the checklist then returned 503 and the first arm was invalid. On the throwaway copy, the 35 load bindings were unbound
  through the audited `bind_requirement` unbind. Their 63,000 obligations due from 2026-11-12 were then deleted, with
  their selections, projection rows and 7,286 check links, with the three append-only delete guards dropped and
  recreated with their original definer and SQL mode. No seal rows referred to them. Orphan checks all returned 0, and
  `rebuild_obligation_projection` and `--verify` both reported 0 differences over 161,250 rows. The 7,286 checks since
  2026-11-12 that had been linked to those slots are now unlinked, so readiness in this copy shows every obligation in
  the window as overdue, missed, due or upcoming, with none passed. Production data was not touched.
- *The 10,000-row bound itself.* Any vehicle with roughly eight or more 150-item checklists due per day reaches the bound
  in the nine-day readiness window, and readiness and the checklist then refuse service. Issue #10's scenario does not reach
  it, but a real fleet could.
- *Partial history day.* The first extension attempt hit the manage wrapper's 900 s limit, leaving 2026-11-12 with 536 of
  750 checks; the remaining days are complete.
- *Database cache.* `innodb_buffer_pool_size` was raised from 1 GB to 3 GB for this test (owner's choice), with the buffer
  pool dumped and reloaded in full so each arm started warm.
- *Settle gate.* The gate required six quiet samples with no swap. 268 KB of swap stayed in use from before the test, so
  every arm waited the gate's full 10 minutes instead; the arms are consistent with each other.
- *Client placement and limits.* The load client ran on the application host itself, over HTTPS to the public name, and
  shares its four cores. `MemoryMax` was raised from 1 GB to 5 GB for the arms so three workers could not be killed; peak
  memory stayed under 1 GB.
- *Load steps leave a slot bound.* `load_prepare` binds a daily slot for its step and does not unbind it, so repeated steps on
  one database accumulate schedules (the cause of the first deviation). Per-arm snapshot restores make it harmless here.

**Not claimed:** a worker count, a hosting layout or a channel layer (SPEC DEP-2 and section 17.7 item 7 remain owner
decisions), any 4×/10× step, or any statistical statement.

## Net benchmark and worker-count test after issues #73 to #75 (2026-11-09)

Claude ran the issue #10 1× scenario against `main` at `a7da994` (which contains issues #73, #74 and #75) on the
Ubuntu rehearsal host (4 vCPU, MariaDB 11.8.6), with the two issue #74 indexes present. It is a worker-count and
channel-layer test as well as a net benchmark, so it varies the number of uvicorn workers. Synthetic data only; one run per
arm, one host, directional. It does **not** satisfy issue #70's final 1× acceptance.

**Setup.** The synthetic twelve-month history (161 distinct days; 25,800 obligations per vehicle) held 148,500 obligations
and projection rows and 114,504 checks for five vehicles at the start of each arm, with 15 crew and two supervisors, think
time 3 to 8 s, arrivals over 30 s, the shipped page's acknowledgement reload (`--ack-reload`) and a 600 s limit. Each arm
restored the same prepared snapshot, started the application, waited until the disk was quiet (30 s consecutive, no swap,
I/O wait under 5%) and then ran the push-driven client; every arm was valid (swap 0, steal at most 0.09%, no watchdog stop;
one 5-second I/O-wait sample of 21% in the first arm). Each crew session performs a fixed 50 saves (750 in all). The arms
were run in this order:

| Arm | Workers and channel layer |
|---|---|
| W1 | 1 worker, in-memory layer (the shipped configuration) |
| W3m | 3 workers, in-memory layer. Diagnostic only: the in-memory layer does not deliver live updates across workers, so this is not a deployable configuration |
| W3v | 3 workers, Valkey as the shared layer through `channels_redis` (a scratch install; nothing was added to the repository) |
| W3v2 | W3v repeated |
| W3m2 | W3m repeated |

**Results** (seconds, client-side wall time):

| Arm | Saves done (window) | Pages and saves p50 / p90 / p95 | Under 1 s / under 2 s | Live reload p50 / p90 / p95 | Supervisor readiness p50 / p90 / p95 | App / DB cores |
|---|---|---|---|---|---|---|
| W1 | 351 of 750 (630 s, stopped at the deadline) | 18.3 / 23.5 / 24.8 | 3.5% / 4.8% | 26.1 / 29.9 / 32.1 | 106 / 136 / 140 | 1.16 / 0.34 |
| W3m | 750 (525 s) | 3.1 / 7.3 / 8.8 | 15.8% / 32.4% | 7.9 / 14.3 / 16.1 | 41 / 68 / 75 | 2.39 / 0.79 |
| W3v | 750 (492 s) | 2.6 / 6.9 / 8.7 | 22.6% / 40.0% | 6.7 / 16.5 / 18.1 | 33 / 83 / 90 | 2.48 / 0.85 |
| W3v2 | 750 (471 s) | 1.8 / 6.4 / 7.9 | 26.4% / 54.2% | 8.4 / 14.0 / 15.7 | 46 / 72 / 77 | 2.42 / 0.84 |
| W3m2 | 750 (484 s) | 1.9 / 6.1 / 8.1 | 26.2% / 51.9% | 6.1 / 16.8 / 18.4 | 35 / 84 / 92 | 2.30 / 0.81 |

Supervisor readiness reads were never under 1 s or under 2 s in any arm; the crew-visible readiness reads were under 2 s in
0 to 3% of cases.

**Reading the results.**
- **The targets are still not met.** The target is at least 95% of page loads and saves under 1 s and at least 98% under 2 s,
  and a stop before higher steps if p90 exceeds 3 s. The best arm had 26% under 1 s and 54% under 2 s, and p90 above 6 s. The remaining cost
  is the checklist and readiness reads, not the saves; issue #80 targets the readiness read.
- **Throughput.** One worker was saturated on application CPU (1.16 cores) and finished 351 of the 750 saves by its deadline
  (0.56 saves per report-second). Three workers finished all 750 in 471 to 525 s (1.43 to 1.59 per second), using 2.3 to
  2.5 application cores and about 0.8 database cores of the four. These windows end when the fixed workload ends and include
  the client's think time, so the 2.6 to 2.9 times ratio is a lower bound on the capacity difference, not a measured capacity.
- **The channel layer's cost is not distinguishable from noise.** Within one condition the two runs differ by about 8 to 25% at
  p90 and up to about 50% at p50 (pages and saves p90: in memory 7.3 and 6.1 s, Valkey 6.9 and 6.4 s), more than any difference between
  conditions. Valkey itself used 3.3 to 3.7 CPU-seconds in about eight minutes and handled about 19,000 and 17,000 commands in
  the two runs.
- **Cross-worker delivery.** A direct two-process test showed Valkey delivered a group message across processes and the in-memory layer
  did not. In the load arms the client counted 2,137 and 2,223 WebSocket pushes with Valkey against 1,135 and 1,243 in memory; the
  expected number of messages was not recorded, so these counts support no loss fraction. **Unexplained:** the two Valkey arms each had abnormal WebSocket closes
  (code 1006), 8 and 9, and the other three arms had none. Whether the layer, worker timing or the client caused them was not
  investigated; it needs an answer before any layer is recommended.
- **Relation to earlier measurements.** Three-worker figures are in the same range as the earlier matrix taken before issues
  #73 to #75 (check-save p90 6.1 to 6.2 s there, 6.1 to 7.0 s here). The arms differ in base and are single runs, so no effect of
  those issues on the 1× result is claimed in either direction.

**Not measured here.** The per-read query count and wall time with the status-at-read join, the status-change duration
and same-vehicle save contention (the client records stale-connection retries, 204 to 448 per arm, which are not a
contention measure). Issue #70 still needs them. **Not claimed:** a worker count, a hosting layout or a channel layer
(SPEC DEP-2 and section 17.7 item 7 remain owner decisions), any split-host result, or any statistical statement.

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

Claude profiled PR #72 head `eef007fc6329742a0d4608c2875c8e54ae4ed6fa`, using synthetic 12-month
history extended to the present (129,000 obligations/projection rows), one two-vCPU rehearsal
host, MariaDB 10.11, and one uvicorn process. The earlier sequential comparison against base
`00f412dc5cd84d4a8dee5502fe295427c40938da` is recorded in the issue #70 comment thread. The
external stdlib HTTP harness ran over loopback with a 30-second client
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
removing checklist reloads alone cannot meet the target. The 33/97 and 19/58 save conflicts in
the multiplier-0.5 and multiplier-1 runs occurred in runs started more than an hour after data
preparation and are not attributed to the multiplier. No conflicts occurred in later runs
started within about 55 minutes of preparation. The cause of the older-run conflicts was not
isolated. These observations point to readiness and save cost as well as reload volume.

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
