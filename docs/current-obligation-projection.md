# Current obligation projection

[issue #67](https://github.com/JustApu/CheckYourAmbulance/issues/67) implements the
materialization permitted by SPEC §17.4. The append-only obligation, selection,
check, link, determination, status and deficiency records remain authoritative.
`SPEC.md` is unchanged. Historical/as-of queries still replay those records.

## Stored facts and current reads

`obligation_projection` is a disposable mutable table with one row per immutable
obligation, including replaced/retired obligations with `selected=False`. Its
primary key is the obligation ID; organization, a denormalized vehicle UUID, due instant and routine
flag support bounded reads. The JSON `facts` field contains the immutable window
and requirement pins, effective linked checks, determination evidence, relevant
suspension intervals and check-chain counters. Vehicle status is intentionally not
stored in these facts: current reads join the latest immutable status event whose
recorded time is at or before `min(read time, D)`. Facts retain datetime microseconds.
`refreshed_at` detects rows refreshed after the reader's captured instant; those
rows alone replay their evidence at that instant, avoiding a whole-window replay
when a save races with a live-update reload.

This deliberately stores **per-obligation** evidence. The checklist chooses its
winner per item and physical kit using the existing urgency and inclusive late
limit rules (issue #66). Readiness still counts **every selected obligation in
its seven-day-back / 48-hour-ahead window**, including missed obligations, and
keeps Passed, Failed, Unable, supervisor completion, suspensions, exemptions,
waivers, invalidations, supersessions and late completions distinct. Shared-rule
check chains contribute their invalidation/correction counts once per window,
even when linked to multiple obligations. It is not a count of checklist rows.

The existing pure obligation evaluator computes state from the assembled facts at
read time. No authoritative state string, checklist winner, scheduler or timer
is needed for transitions at E, D, G, or just after the inclusive late limit L.
All future obligations already generated are projected, so a future candidate
can become the checklist winner as the clock advances. Waiver rule effective
instants and suspension ends are retained; an exemption at D or a suspension
at G retains the historical evaluator's capped-instant semantics.

`vehicle_checklist` with omitted `as_of` and no suspension adapter, and
`fleet_readiness` with omitted `as_of`, use this table. Supplying any explicit
`as_of`, even a recent instant, keeps event replay. Custom suspension adapters
also keep replay. Current readers compare the complete in-scope obligation ID set
with the projection rows; a missing/partial cache falls back to replay without
writing. Selection flags come from the projection, which writers refresh
transactionally with append-only selection events, avoiding a per-read walk of the
selection stream. If any projection row was refreshed after the request captured
its instant, the reader re-reads selection history at that instant and replays only
the affected selected rows. A row with another facts version is treated like a missing
cache, so current readers replay until the projection is rebuilt (version 2 carries the pinned
late limit, issue #80); evaluating such facts directly still fails visibly.
Evidence queries carry their page cap into SQL before queryset evaluation, and
per-vehicle caps still refuse an incomplete answer. Attention/known deficiencies remain separate from obligation satisfaction and
use their existing readers; they are not copied into obligation projections.

## Writer coverage and locking

The audited command runner calls `refresh_for_receipt` after all domain effects
and before the success audit/commit. It finds affected obligations from source
rows bearing that receipt, including bulk inserts and secondary effects:

| Source-changing command | Affected projection rows |
|---|---|
| Check, author correction, chain invalidation | Every obligation linked to the changed chain; determination withdrawals and suspension ends caused by those effects |
| Generation/reconciliation | Newly created obligations, replacements and the old obligations retired/replaced by selection events |
| Explicit ad hoc inspection order | Every ordered item/physical-kit obligation |
| Supervisor Unable completion/invalidation | The determination's obligation |
| Conditional evaluation, roster ingestion/unavailability, actual-roster correction | Every obligation with a determination appended by that command |
| Set status / approve status request | No projection rows; current reads join the status event at read time |
| Suspend, early end, deficiency closure/cancellation | Routine obligations of the named item/physical-kit whose grace instant could have been covered by the original START |
| Checklist/override publication, bindings, kit addition/move/deactivation | Existing reconciliation appends selection changes in its own transactions; those transactions refresh their changes |

Publication and kit reconciliation retain their existing transaction boundaries
and restart recovery; materialization does not turn their after-commit
reconciliation into a single transaction with publication/assignment.

Existing domain locks remain in place. Writers that change stored obligation facts
lock projection rows as their final lock group, acquired in obligation-ID order,
including missing-row creation. Status commands keep their existing vehicle lock but
do not lock or refresh obligation rows. The vehicle lookup is copied from the
immutable obligation and has no foreign key to the mutable vehicle row, avoiding an
implicit parent lock after projection locks. Replay starts
**after** those locks are acquired, under the configured READ COMMITTED isolation.
The projector acquires no vehicle, item, template or deficiency lock. A waiting
refresh therefore sees the preceding refresh's committed source effects without
reversing domain lock order. Failure rolls back the source effects, projection,
receipt and success audit together; the existing failure-audit path still runs.
A same-operation replay returns its original acknowledgement without rewriting
anything. Source writes that bypass audited commands require an explicit rebuild.

New source-changing command types must be added to the routing map and tested.
The projection has normal mutable-table runtime SELECT/INSERT/UPDATE grants;
append-only guards and grants on evidence tables are unchanged. Runtime DELETE
and DDL are not needed.

## Rebuild and compare

See the [operator procedure](deployment-runbook.md#obligation-projection-rebuild-and-comparison).
`rebuild_obligation_projection --organization <uuid>` streams the source
catalogue, one vehicle/due-instant group per transaction. It repairs missing,
stale and retired rows, then checks repeatability and compares selected states
against independent selection loading and full event replay. It writes only the
projection; it does not regenerate obligations or change events.

`--dry-run` and `--verify` both compare without writes and exit nonzero if rows
are missing/stale or replay disagrees. `--vehicle <uuid>` narrows the organization
scope. Invalid IDs and unknown scopes fail before writing. Results report the
number of differing and verified rows. Repeating a successful rebuild leaves
facts and timestamps unchanged. A failed group rolls back; earlier completed
groups remain committed and rerunning is safe. Existing evidence caps still
fail visibly instead of accepting a partial answer. The independent replay checks
each due group at `min(rebuild start, due + 31 days)`: refreshed facts are first
compared with current source evidence, while replay stays close to the due group
to keep the attention check-chain walk within the evidence cap on long histories.
Keep traffic closed on a failed recovery verification.

## Verification and measurements

Offline tests compare compiled evidence against full replay over seeded synthetic
histories, including later clock instants, corrections, invalidations, kits,
outages and publication/reconciliation. Database tests cover the actual command
hooks, supervisor determinations, suspensions, concurrent status/save ordering and
read-time status joins across the generation horizon,
missing-cache fallback, rebuild dry-run/idempotence and source rollback on a
projection failure. Readiness tests ensure current reads avoid check-chain replay
while explicit historical reads retain the batch replay adapter.

Performance evidence and remaining rehearsal work are recorded in
[the issue #67 measurement record](issue-67-performance.md). Local measurements
are not the production-like, single-process shift-change test required by
[issue #10](https://github.com/JustApu/CheckYourAmbulance/issues/10).
