# Obligation state and historical projections

[issue #60](https://github.com/JustApu/CheckYourAmbulance-archive/issues/60) and [issue #87](https://github.com/JustApu/CheckYourAmbulance-archive/issues/87) implement SPEC §5.3–§5.4 and §19, with offline AC-26/27 checks.
No state column, projection table, command, migration or dependency is added.

## Read API

```python
from cya.obligations.queries import get_vehicle_obligations_as_of

states = get_vehicle_obligations_as_of(
    vehicle_id,
    instant_T,
    organization_id=organization_id,
    start=due_start,
    end=due_end,
)
```

Instants must be timezone-aware. The caller supplies the authorized organization;
this internal service does not replace request authorization. A vehicle outside
that organization raises `Vehicle.DoesNotExist`. Inactive vehicles remain readable
historically: a mutable active flag cannot erase an earlier answer.

Bounds are inclusive due instants, defaulting to T minus seven days through T plus
48 hours. Intervals must be ordered and no longer than 31 days. Each evidence
collection is capped at 10,000 rows; exceeding a cap raises `ValueError` rather
than returning an incomplete compliance answer. Narrow the interval when needed.
Status history also has this cap, so a large status stream needs a future
checkpoint/pagination implementation rather than silent truncation.

The result is a deterministic tuple of immutable `ObligationState` values ordered
by due instant, item ID and kit instance. Each includes the obligation and its requirement,
publication, schedule and conditional-rule IDs, state, evidence ID, outcome,
completion instant, late seconds and a `self_completed` report flag. Failed inspections are `SATISFIED` with
outcome `FAILED`, never Passed. Projection results additionally include `attention` (the
immutable item/kit attention result) and `deficiencies` (all applicable deficiency facts,
including cancelled/closed statuses). Attention is separate from satisfaction: a later
Pass does not clear an unresolved deficiency. The pure single-obligation evaluator
leaves these two fields at their defaults; the vehicle rebuild populates them.

## Pure evaluator and precedence

`cya.obligations.state.evaluate_obligation` takes an `ObligationEvidence`, T and
explicit immutable determination, linked-check, vehicle-status and suspension
evidence. It performs no queries, writes, external calls or clock reads.

Distinct exemption/waiver/suspension reasons take display precedence over checks:

- Routine `EXEMPT` uses vehicle status at `min(D, T)`. After D, an outage at D stays
  exempt on return to service; an outage beginning after D cannot erase an overdue
  inspection. No extra obligation is fabricated for an exempt interval in which
  generation created none; the existing operational-status interval reader displays
  those intervals ([vehicle status](vehicle-status.md)).
- Routine `WAIVED` uses the visible leaf determination and its recorded, effective rule.
  Non-routine ad hoc orders ignore carry-over waiver evidence (ADH-1).
  Reinstatement or invalidation does not revive an older waiver. Ambiguous branches
  raise `ValueError`. This reader never re-evaluates rosters or writes determinations.
- `SUSPENDED` is distinct from Pass (SPEC §8.9, AC-18); an active suspension prevents a
  check from claiming satisfaction, and `display_text` is "Known Deficiency — Check Suspended".
  Only a routine obligation is suspended (SUS-1); an explicit ad hoc order never is. A suspension
  is judged at `min(T, G)`, as exemption is judged at `min(D, T)`: an obligation whose window
  closed while it was suspended stays `SUSPENDED` after the suspension ends (never Overdue, never
  Satisfied), a suspension that starts after G cannot erase an inspection already missed, and one
  that ends while the window is open makes the check required again (Current, Due, then Overdue
  unless checked). A qualifying check, or supervisor completion, recorded before the suspension
  started still satisfies the obligation, through G and after the end: a suspension covers only an
  obligation still unsatisfied when it starts. `evidence_id` is the suspension's `START` id; with
  several, the earliest start.
  `suspension_ended_at` is set when that suspension had ended by T (`suspension_in_force` is
  false), so a reader can tell an ended suspension kept in history from one still in force.
  The `min(T, G)` boundary readings are implementation assumptions of [issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13), recorded on its PR;
  the earlier-check rule is an owner decision (2026-11-01, PR #29).
- An effective, explicitly linked Passed or Failed check satisfies the obligation.
  Not Applicable additionally needs applicable administrator-rule evidence.
  Unable alone does not satisfy it. A supervisor completion additionally requires an
  effective supporting Unable check explicitly linked to that determination;
  it yields outcome `SUPERVISOR_COMPLETED`, never Passed. Direct administrator
  invalidation appends an `INVALIDATED` child and makes the obligation unsatisfied
  unless another qualifying check applies. The self-completion flag follows OQ-41, decided by the owner
  (2026-10-25): a supervisor may complete their own Unable check, and the case is flagged.
- Otherwise: `UPCOMING` before E, `CURRENT` from E through just before D, `DUE`
  from D through G inclusive, and `OVERDUE` after G.

The earliest valid qualifying check governs satisfaction. Before-E and future
checks do not qualify. A late check needs an explicitly eligible SCH-2 link; the
evaluator does not decide competing-window link allocation. Lateness is preserved.

## Check and suspension adapters

The default projection reads stored check/link and determination-check evidence,
and stored supervisor check suspensions (`stored_suspensions`, [issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13)): each
`check_suspension` `START` recorded by T covers the routine obligations of its item and physical
kit instance whose window it can touch, ending at its `END` if that was recorded by T, so a later
closure never changes an earlier answer. Optional `check_lookup` and
`suspension_lookup` callables receive keyword arguments `organization_id`,
`vehicle_id`, `obligation_ids` (a bounded tuple) and `as_of` (T). They return
`CheckEvidence` and `SuspensionEvidence` respectively; a supplied `suspension_lookup` replaces
the stored suspensions. Wrong types, unrequested
obligations and cross-organization/vehicle checks raise `ValueError`.
Adapter exceptions propagate; a failed lookup is not treated as an empty history.

Check adapters must resolve authorized links and correction chains under SPEC
§19.1: `voided_at` is the earliest correction or chain-invalidation instant that
excludes that effective check row. Later invalidation is ignored at an earlier T.
`late_eligible=True` attests a valid late link; `na_rule` supplies the applicable
rule; `supervisor_determination_id` attests the supporting determination/check
relationship. These optional adapters replace stored satisfaction evidence, not attention evidence,
and are not permission to accept arbitrary client-provided evidence. Invalidating a chain
must exclude all its check rows; the evaluator never revives an earlier outcome.

## Historical reconstruction and verification

`load_projection_history` exports immutable evidence recorded by T; its adapter
reads the append-only selection, obligation, requirement, determination, rule,
check, check-event, check-link, determination-check, deficiency, deficiency-scope,
deficiency-event, check-suspension and vehicle-status tables. It preserves original rows and link timestamps,
rather than exporting only the effective checks at export time: a final export can therefore
reconstruct a check before its later correction or whole-chain invalidation. Unlinked
participation checks contribute to attention but never satisfy an obligation. Satisfaction
keeps the root check's inspection instant; attention orders effective rows by their own
recorded instant, matching the live attention reader. All added collections have the same
10,000-row fail-closed cap. Deficiency status is replayed from complete lifecycle chains;
missing predecessors, forks, inconsistent statuses and illegal transitions raise `ValueError`.
Hand-built exports also raise `ValueError` for malformed determination timestamps (including
naive datetimes) or a visible effective check's link to an obligation absent from the export.
The mutable deficiency projection is never read by reconstruction. `rebuild_vehicle_projection` folds the export without a
database. Input order does not affect replay. Events after T do not change a
past selection, waiver, check, suspension or exemption. Determination rules must
be recorded and effective by T and effective by D. Obligation requirement pins
remain the versions resolved for D when selected, including already-published
future-effective requirements on pre-created future obligations; this API never
substitutes today's template or schedule for a historical pin.

`cya/obligations/tests/synthetic_history.py` generates seeded valid histories through the actual
audited commands over 14–28 simulated days, including publications, schedule
revisions, outages, missed jobs and recovery. Its optional `checks=True` histories also
record Failed/Passed races, author corrections, administrator invalidations, Unable checks,
supervisor completions and deficiency cancellation/reopening through audited commands.
With `kits=True`, kit instances are added, removed and returned, and checks name an instance.
Check-rich histories change kit assignments while inspection windows are open. The AC-26/27 tests compare daily
projections against the established selection/status readers and a separate
boundary oracle, then shuffle the final immutable history and replay every
earlier observation with zero database queries. Check-rich histories independently compare
stored links and the existing chain reader, historical attention readers and every live
`DeficiencyProjection` status against lifecycle-event recomputation (SPEC §19.5). These
tests run in CI through the existing full `manage.py test cya` job. Past API answers must also
remain byte-for-field equal after all later events. Pure tests separately cover
check outcomes, late links, correction/invalidation timestamps, waiver chains,
supervisor completion, suspension and malformed input. Stored suspensions are tested end to end
in `cya/deficiencies/tests/test_suspension.py`, which also replays the final export at every
earlier observation with zero database queries.

Run offline with the configured disposable development database:

```sh
local/venv/bin/python manage.py test cya.obligations.tests.test_state cya.obligations.tests.test_history cya.obligations.tests.test_projections --noinput
```

These functions only read. Repeated calls with the same immutable evidence and
time yield the same result. There is no mutation or `--dry-run` flag. No production
history, performance benchmark, external service or backup restore is involved.

## Explicit ad hoc inspections ([issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158))

SPEC ADH-1 orders are non-routine `obligation` rows. They do not have selection
rows: each human command's item/physical-kit pairs retain their own requirement
versions, even alongside a routine obligation at exactly the same due instant.
Both `effective_obligations` and the bounded historical projection include them;
the routine generator and publication reconciliation cannot replace or retire them.
The odometer reader considers only routine selections.

`ObligationEvidence` includes the explanation, actor ID and command ID, and has a
null schedule ID for an ad hoc order. Requirement binding, schedule and conditional
rule pins are null for these explicit orders; publication, overlay, definition and
physical-kit assignment pins remain immutable. The window is `E = recorded_at`,
`D = G = requested due_at`. Before creation the order is absent; after the deadline
it is Overdue until qualifying linked evidence satisfies it. Out-of-Service does
not exempt a non-routine order, and carry-over does not waive it. Later publications
or kit changes do not erase the order. Normal check-link allocation still applies:
one primary link, earliest eligible due time (stable ID breaks ties), additional
links only under an explicit shared-satisfaction rule. A session satisfies nothing.

The checklist opts into `include_all_ad_hoc=True` so an explicit order remains
visible even beyond the routine seven-day lookback/48-hour horizon. This bypasses
only the due-interval filter for ad hoc rows, retaining the 10,000-row evidence caps
and as-of cutoff. Missing current checklist/kit rows are displayed from their
immutable request-time pins. Normal historical callers keep bounded due intervals.

The immutable history export includes ad hoc rows directly. Database-free replay
uses those rows alongside the routine selection tails and replays checks,
corrections, invalidations and determinations identically for both origins.
Supervisor completion of an Unable-to-Inspect check currently applies only to a
routine selected obligation; an ad hoc order still requires a qualifying check.
