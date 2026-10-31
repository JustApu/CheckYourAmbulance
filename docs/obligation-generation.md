# Obligation generation, the selection stream and publication reconciliation

[issue #59](https://github.com/JustApu/CheckYourAmbulance-archive/issues/59) implements [SPEC §5.1, §5.7–§5.9 and §6.4 (ODO-2)](../SPEC.md#5-inspection-obligations-windows-and-scheduling),
TIME-1 and TIME-3, and the `requirement_version`, `obligation`, `obligation_selection` and
`odometer_obligation` rows of [contracts §2](data-model-contracts.md#2-table-catalogue). It also adds one table
the contracts do not name, `requirement_binding` ([why](#requirement_binding-a-table-the-contract-does-not-name)).
SPEC and the contracts win if they disagree with this page.

It does **not** record checks, link them to obligations, derive obligation state (Due, Overdue, Satisfied,
Exempt) or evaluate carry-over waivers. Those belong to later issues; this one decides *which obligations exist*.

## What exists

| Piece | Where |
|---|---|
| Models and explicit table names | `cya/obligations/models.py` |
| Tables, checks, organization-pair and anchor keys, append-only and stream triggers | migrations `0001_initial`, `0002_obligation_guards` |
| What a vehicle owes at a due instant (bindings, schedule revisions, pins) | `cya/obligations/requirements.py` |
| The writes one unit needs (pure read) | `cya/obligations/planning.py` |
| Locking, audited commit, generation and publication reconciliation | `cya/obligations/generation.py` |
| Administrator binding command | `cya/obligations/commands.py`, `manage.py bind_requirement` |
| Operator job | `manage.py generate_obligations` |
| Publication hook | `cya/checklists/signals.py` (sender), `cya/obligations/signals.py` (receiver) |
| As-of reads | `cya/obligations/queries.py` |
| Tests | `cya/obligations/tests/` |

All five tables are append-only (A): `SELECT, INSERT` for the runtime account, and `BEFORE UPDATE/DELETE`
triggers refuse changes even for the schema owner.

## Data

### `requirement_binding`: a table the contract does not name

A checklist definition carries no schedule, and a schedule slot is not tied to a vehicle, yet "Ambulance 1 is
inspected at 06:00 and 18:00 against the ambulance checklist" has to be stored somewhere. A binding states that a
vehicle follows one checklist **template** and one schedule **slot**, from an effective instant. A vehicle may have
several (two slots for Ambulance 1; weekday and weekend slots for Ambulance 2). Revisions per
(vehicle, template, slot) are consecutive, strictly later in effective time, never retroactive, and either `BIND`
or `UNBIND`; the latest revision in force at an instant decides. Only an active administrator appends them
(`bind_requirement`).

This is a reversible implementation choice made to satisfy the issue, not a product decision. **Item-specific
schedules** (SPEC §5.2, "item-specific schedules override fleet defaults where defined") are not modelled: nothing
defines them yet, so every item of a bound template follows the binding's slot.

### `requirement_version`

A requirement resolved for one vehicle and item, pinning every input: the template's publication, the item
definition, the override version (if any), the binding revision, the exact `schedule_rule` row and the
carry-over `conditional_rule` row in force (if any). Its id is a `uuid5` of those pins, so identical inputs always
name the same row and a rerun never creates a second one. `item_def`, the publication and the override must belong
together, and the binding must be a `BIND` for that vehicle; a trigger checks this independently of the
application. Because the pins include the publication, **every publication gives each item a new requirement
version**, so pre-window obligations are replaced even for items the new version did not change (contracts §2:
"resolve the requirement effective at D"). The deviation quantity and the `na_rule` are not pinned (no `na_rule`
table exists yet).

### `obligation`

`(vehicle, item, due_at, requirement_version)` is unique per organization; `early_at ≤ due_at ≤ grace_at`
(E ≤ D ≤ G, SPEC §5.3). `routine` is true for everything generated here. `basis_at` is the instant before which
every record counted when the requirement was resolved: the obligation's own creation when it was resolved from
current data, or the cutoff a late repair was judged against (see
[judging a change](#judging-a-change-against-the-early-window)). The row never changes; whether it is the
effective one for its anchor is decided by the selection stream.

### `obligation_selection`

The immutable event stream per anchor `(vehicle, item, due_at)`. Sequence 1 is a `SELECT`. Later events are
`REPLACE` (names a new obligation) or `RETIRE` (names none) and reference the event before them. The latest event
decides what is effective; there is no mutable flag. A `BEFORE INSERT` trigger enforces:

- the predecessor is the tail of the same anchor, exactly one sequence earlier (so the stream cannot gap or
  branch; the unique predecessor and `(anchor, sequence)` keys back this up);
- replacing or retiring a **selected** obligation carries that obligation's `SUPERSEDED` determination from the
  same command (same vehicle and due instant);
- after a `RETIRE`, the next event is a `REPLACE` that supersedes nothing;
- the selected obligation was created by the same command and has never been selected before.

`SUPERSEDED` rows live in the existing `obligation_determination` table ([issue #58](https://github.com/JustApu/CheckYourAmbulance-archive/issues/58)). Its `obligation_id` is not a
foreign key ([issue #58](https://github.com/JustApu/CheckYourAmbulance-archive/issues/58) had no obligation table), so the trigger above is what ties a determination to a real
obligation here; adding the key is left to the state-derivation work.

### `odometer_obligation`

One vehicle-level odometer entry owed per **scheduled routine inspection** (ODO-2), with that inspection's
window. It records the `schedule_rule` row it came from, and is unique per `(vehicle, scheduled_inspection_key)`;
the key is `<slot>@<due instant UTC>#<schedule row id>` (a trigger checks it), for example
`first-due-0600@2026-06-01T10:00:00.000000Z#<uuid>`.

It has no selection stream of its own. It is **effective exactly while an effective item obligation of the same
vehicle and due instant pins the same schedule row** (`cya.obligations.queries.odometer_obligations`). So it
follows its inspection: a pre-window schedule revision that changes the window replaces the item obligations, the
new inspection gets a new odometer row with the new window, and the old row stops being effective (it is kept);
an inspection that is retired leaves no effective odometer obligation; and an in-window revision changes nothing.
Exactly one is therefore effective per routine inspection, and its window always equals its items' window.
Linking an entry to it is the odometer-entry issue's job.

## How a unit is reconciled

The unit of work is one vehicle and one due instant D. `reconcile_unit` does the following.

1. **Preview.** `build_plan` reads the bindings in force, the schedule revision governing D, the checklist
   resolved as of D, the conditional rules in force, the current selection per item and the vehicle's status, and
   returns exactly the events to append (or nothing). It never writes. [How a change is judged against the early
   window](#judging-a-change-against-the-early-window) is explained below.
2. **Job key.** The worker command's job key is `["obligation-unit", vehicle, D, plan digest]`. It names the
   business change, not the wake time, so a retry reuses it and a unit that is already current produces no command
   at all.
3. **Lock and recompute.** Inside the command: lock the `vehicle` row, then each bound `checklist_template` row
   (in id order), recompute the plan and compare digests. If they differ, nothing is written and the unit is
   planned again (at most three attempts, then `ReconcileConflict`; the next run retries). A rolled-back attempt
   is audited as a failed command, not hidden.
   The unlocked preview can also see another worker's just-committed obligations as "archived"; that
   `RequirementRevived` is retried like a changed plan, and only a refusal that survives every attempt (a genuine
   revival) is raised.
4. **Append.** Missing requirement versions, `SUPERSEDED` determinations, obligations, selection events and
   odometer obligations, in one transaction. Any failure rolls the whole unit back.

For each anchor:

| Current state | Requirement | Event |
|---|---|---|
| no selection | applies, vehicle in service | `SELECT` the latest requirement (this also repairs a missed run for past and current due instants) |
| no selection | applies, vehicle out of service | nothing |
| selects an obligation | same, judged as below | nothing |
| selects an obligation | changed, judged as below | `REPLACE` (+ `SUPERSEDED` determination of the old one) |
| selects an obligation | no longer applies, judged as below | `RETIRE` (+ `SUPERSEDED` determination) |
| `RETIRE` tail | applies again, judged against the new window the same way (from the records as stood then, even if the latest records no longer require it), vehicle in service | `REPLACE` with no determination |

### Judging a change against the early window

SPEC §5.7 lets a published change reach only obligations whose early window has not opened. The clock alone
cannot decide that: a repair that runs late (a publication callback that failed, a stale read that lost a race)
must still apply a change that was made in time, and must never apply one that was not. So the question is
asked about **when the change was recorded**. For an anchor that already selects an obligation O, the requirement
is resolved *as the records stood just before* `freeze = max(O.early_at, O.basis_at)` and compared with O's
requirement. `basis_at` is O's creation for an obligation made from current data (so one created after its window
opened is judged from the records before it was made), and the cutoff the repair used for an obligation made by a
late repair (so repairing does not itself move the freeze forward and let a later record in). Before the freeze
instant everything recorded so far counts. Publications, bindings, schedule revisions and conditional rules
recorded at or after it are invisible to that question.

Consequences, each covered by a test:

- a change recorded before the window opened is applied by whichever run notices it, however late;
- a change recorded after the window opened is never applied, by any run, so in-window and past obligations keep
  their selection and the requirement they were created with;
- an obligation created late (a catch-up after a lost run) is created from current data and is not downgraded or
  moved by later runs;
- a repair that replaces an obligation keeps the original cutoff, so running again, however often and however
  much later, never admits a record that arrived after the window opened;
- repeated runs give the same answer.

**The SPEC §5.7 "apply to open obligations" path is not implemented and cannot be switched on.** The owner
decided this on 2026-10-23 ("Confirmed, never, for now. We may change this in a future revision."; recorded on
[issue #59](https://github.com/JustApu/CheckYourAmbulance-archive/issues/59) and [PR #68](https://github.com/JustApu/CheckYourAmbulance-archive/pull/68), and in SPEC §5.7 as OQ-52). If the option is ever wanted it needs its own issue: an audited
administrator action, a rule for checks already recorded against the old obligation, and an owner-approved SPEC
amendment (§25.4).

### Out of service (SPEC §5.9, OQ-31)

A vehicle that is out of service when an obligation would be **created** gets none: for a due instant already
reached the status *at that instant* decides; for a future one the status *now* decides. Returning to service
therefore never backfills an outage, and a vehicle that returns before a future D simply gets D on the next run.
Status gates only creation: an existing obligation is never retired because the vehicle later goes out of
service, since its exemption is derived from the status stream (SPEC §19.2). A deactivated vehicle is skipped.
A template's own `active` flag is **not** an input to generation: it has no history, so the result could not be
reproduced, and reactivating it would have to backfill the time it was off. Instead the database enforces that **a
bound template is always active**: deactivating a template that any vehicle is bound to, or will be under a
scheduled binding, is refused ("template is still bound to a vehicle; unbind it first"). Deactivation can therefore
only follow a recorded, dated unbind, which is what stops obligations. Binding an inactive template is refused too,
and reactivating is always allowed. An administrator screen that deactivates a template should show which vehicles
are still bound and offer the unbind, rather than surface the database error.

### Schedules, DST and horizons

Due instants come from `cya.schedules` (station wall-clock time, UTC windows, TIME-3 gap and fold rules), using the
schedule revision in force at each due instant: a revision governs from its own effective instant up to the next
revision's. A binding must be in force at D itself. `generate_obligations` covers `lookback` hours back through
`horizon` hours ahead, and also revisits every due instant in that interval that already has a selection, past
ones included, so a changed schedule or binding that was recorded before the window retires obligations whose
occurrence no longer exists even when the run happens after the due instant. Anything older than the lookback is
not revisited. Nothing is generated before the earliest effective
binding, schedule and published checklist, so go-live does not invent history.

## Publication reconciliation

`publish_template_version` and `publish_override_version` send `checklist_published` **after** they commit, never
on a replay. The receiver reconciles the affected vehicles (those bound to the published template, or the
override's vehicle) in their own transactions, using the same `reconcile_unit`. Because the publication is already
committed, a receiver failure never undoes it: it is logged at `ERROR`, and the next `generate_obligations` run
repairs whatever was missed, **even after the window has opened**, because the publication was recorded in time
(see above). The receiver runs synchronously in the publishing call, after the commit, so the
publisher waits for it (one short transaction per affected vehicle and due instant). Publishing a version that takes effect beyond the horizon changes nothing yet; the
generator picks it up when its due instants come into range. Schedule revisions, conditional rules and status
changes do not send a signal; the scheduled run reconciles them.

### Races

Template publication locks the template row; override publication, binding changes and status changes lock the
vehicle row. A unit takes the vehicle lock and then the template locks (never the reverse), so a generator and
those writers serialize on a lock they both need and read their records only after holding it. A publication that
commits between a preview and the lock makes the digests differ, and the unit is replanned. The race tests cover
both orders.

Schedule revisions and conditional rules are **not** serialized with a unit: their commands share no lock with it.
A revision that commits just after a unit read the schedule is therefore not seen by that unit. It is not lost
either: it was recorded before the window it concerns, so the next run applies it by the rule above (a test
reproduces exactly this interleaving). This is why recorded time, not locking, is what keeps the result correct.

## Operator commands

```sh
local/venv/bin/python manage.py bind_requirement --organization <org-uuid> --actor <administrator-uuid> \
    --vehicle <vehicle-uuid> --template <template-uuid> --slot first-due-0600 --operation-id <new-v4-uuid>
# Unbind with --action UNBIND; --effective-at <aware ISO 8601> schedules it (never in the past).

local/venv/bin/python manage.py generate_obligations --organization <org-uuid> --dry-run
local/venv/bin/python manage.py generate_obligations --organization <org-uuid>
```

`generate_obligations` takes `--horizon-hours` (default `CYA_OBLIGATION_HORIZON_HOURS`, 48, at most 168),
`--lookback-hours` (default `CYA_OBLIGATION_LOOKBACK_HOURS`, 168, at most 744) and repeatable `--vehicle`.

- **Reads** bindings, schedules, published checklists, rules, status and selections. **Writes** only the five
  tables above plus `command_receipt`, `audit_event` and `SUPERSEDED` rows in `obligation_determination`, each unit
  in its own audited worker command (`worker.obligation_generator`, `OBLIGATIONS_RECONCILE`).
- **Idempotent.** A run that finds everything current writes nothing, and a missed run is repaired by the next one,
  never by duplicate rows. Schedule it at least hourly (for example from cron); it needs no wake-time argument.
- **`--dry-run`** writes, audits and reserves nothing; it prints the per-unit counts it would write.
- **Failures** are collected per unit and printed to stderr; units that succeeded stay committed, and the command
  exits non-zero. If the job stops for longer than the lookback, raise `--lookback-hours` for the next run so
  those due instants are still materialized.
- **Success** is verifiable by rerunning: it reports `0 selected` and every unit "already current".

`bind_requirement` is administrator-only, audited and replayable by operation id; it refuses a no-op, an unknown
slot, an inactive or foreign vehicle/template, and any effective instant in the past or not after the previous
revision. It reconciles the vehicle afterwards (logged, not fatal, on failure).

## Reading

`cya.obligations.queries` returns what is effective: `effective_selection`, `effective_obligations` and
`odometer_obligations`. `as_of` keeps only events recorded by that instant, so an answer for the past does not
change when a later publication replaces an obligation. Ranges are required and must be timezone-aware.
[issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158) also includes explicit [ad hoc orders](obligation-state.md#explicit-ad-hoc-inspections-issue-158)
in `effective_obligations`; those non-routine rows have no selection stream and
are never generated, replaced or retired by the routine job. Odometer obligations
continue to follow routine selections only.

## Where the schema completes or differs from the contract text

Reversible choices; none changes SPEC behavior.

1. **`requirement_binding`** is new (above), and `requirement_version` also pins the binding revision.
2. **Job key per unit and plan.** The contract keys generation on vehicle/item/D plus requirement revision and
   reconciliation on publication plus prior selection. One command per (vehicle, D) with a plan digest covers both
   with fewer receipts and, because the digest includes every predecessor and requirement, is replayed exactly when
   the same change recurs.
3. **`requirement_version` has no `na_rule_id`** (no table yet) and carries `binding_id`.
4. **`obligation_determination.obligation_id` is not a foreign key** (see above).
5. **No `vehicle_lock` / `item_lock`.** Those tables belong to the check-recording work; the vehicle and template
   rows are locked instead.
6. **Odometer obligations carry their schedule row and follow their inspection** (above) instead of being a
   single immutable row per `slot@instant`, so a window change cannot leave a stale entry.

## Kit instances

Since [issue #92](https://github.com/JustApu/CheckYourAmbulance-archive/issues/92) an obligation's anchor is `(vehicle, item, kit instance, due instant)`: an item inside a kit is owed once per kit instance (SPEC 1.8.0), and `kit_key` is part of every key and trigger that names the anchor. See [obligations per kit instance](kit-obligations.md). Everything above applies per anchor.

## Not in this issue

Evaluating carry-over waivers for new obligations ([issue #58](https://github.com/JustApu/CheckYourAmbulance-archive/issues/58) supplies the evaluator and the pinned rule is
recorded here); linking checks and odometer entries; obligation state, Exempt display and as-of state derivation;
item-specific schedules and shared-satisfaction rules; notifications; screens.
Ad hoc (non-routine) obligations are implemented separately by [issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158).

## Verification

`python manage.py test cya.obligations --noinput` covers horizon and idempotency, missed-run repair, out-of-service
gating, spring-forward and fall-back dates, schedule and binding history, requirement pins, every reconciliation
rule above, the stream triggers attacked directly (with the exact refusal asserted), real-connection races between
generation and publication, crash and restart recovery, the CLI, and the actual runtime account (with
`CYA_DB_SECURITY_TESTS=1`). See the [development environment](../CONTRIBUTING.md#python-and-django).
