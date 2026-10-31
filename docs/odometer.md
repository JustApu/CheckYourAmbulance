# Odometer entries, anomaly confirmation and obligation satisfaction

[issue #79](https://github.com/JustApu/CheckYourAmbulance-archive/issues/79) adds the append-only odometer tables and the audited commands behind
`POST /api/v1/odometer-entries` (SPEC §6.4, ODO-1–ODO-4, AC-21; [contracts](data-model-contracts.md)
`odometer_entry`, `engine_hours_entry`, `odometer_obligation_link`). One call records one
attributed reading; the reading satisfies the vehicle's odometer obligation for everyone, exactly
once. Part B adds author-only correction and administrator invalidation (ODO-5, `reading_event`).

Code: `cya/odometer/commands.py`, `corrections.py`, `api.py`, `state.py`, `models.py`;
guards in migrations `0002_odometer_guards.py`, `0003_reading_decisions.py` and
`0004_downstream_anomaly.py`.

## What a call does

1. The HTTP handler needs a signed-in user (401), a valid CSRF token, `Content-Type:
   application/json` and a body of at most 256 KiB. Actor, organization and roles come from the
   session, never the body.
2. Shape errors are rejected before any transaction: `value` (and optional `engine_hours`) is an
   integer or a decimal string with at most one decimal place, from 0 to under ten million;
   `unit` is `mi` or `km`; `confirm_anomaly` is a boolean.
3. `run_command` authorizes the `record_check` action (all four roles; an odometer reading is the
   record of a `VEHICLE_READING` item, so no new §3.3 row is needed), and returns the stored result
   if the `operation_id` already has a receipt.
4. It locks the vehicle row (the row obligation generation takes first), so two readings for one
   vehicle serialize, and reads the clock once under that lock; the receipt, the entry and the link
   all carry that one instant, so a window opening mid-call cannot split them. It finds the vehicle's previous valid reading, applies the anomaly rule,
   chooses the obligation to satisfy, and writes receipt, entries, link and audit atomically.

A rejected call rolls back whole: no entry, no receipt, and the `operation_id` stays usable. A
retry with the same `operation_id` returns the original result without a second row; the same id
with a different request answers `409 OPERATION_REUSED`.

## Anomaly rule (ODO-3)

The previous valid reading is the effective, non-invalidated entry whose original reading root
comes latest in the vehicle's reading sequence. A correction changes a root's effective value,
but does not move that root later in the sequence. A reading is anomalous when it is **lower** than
the previous reading, or **higher by more than the plausibility threshold**. An anomalous
reading is refused with `409 ANOMALY_CONFIRMATION_REQUIRED` (`value`, or `engine_hours`) until
the caller repeats it with `confirm_anomaly: true`; the entry then stores
`anomaly_confirmed`, the optional `explanation` and the confirming user (its `actor_user`). An
equal reading is normal. `confirm_anomaly` on a normal reading stores nothing, and an
`explanation` with nothing to confirm is refused (`INVALID_CONFIRMATION`). A reading in a
different unit than the vehicle's previous one is refused (`UNIT_MISMATCH`).

**Threshold.** Default 1,000 mi (1,600 km) per reading, recorded as decision OQ-55 in the SPEC
register. An administrator changes it with `set_odometer_threshold` (action
`configure_schedules_thresholds_notifications`): each change appends a numbered revision per
unit (`odometer_threshold`, AUD-4), the latest applies, and the insert trigger applies the same
revision. The check is `increase > threshold`, so the limit itself is allowed. A revision that lands
between the command reading the threshold and the insert makes the trigger refuse the write; nothing
is stored, the API answers `503 RETRY_LATER`, and the retry uses the new limit.

**Engine hours (ODO-4)** are optional on the same call and stored in `engine_hours_entry`.
Only the lower-than-previous half of the rule applies (SPEC states no hours threshold); a
confirmed lower value shares the one `confirm_anomaly` flag.

## Satisfaction (ODO-2)

The reading links to the **earliest-due unsatisfied** odometer obligation of its vehicle whose
window contains the instant (`early ≤ t ≤ grace`; SCH-6). Only if none does, it completes the
earliest-due obligation already past grace (a late completion, SCH-2, looking back 31 days). An
obligation whose early window has not opened is never linked, and a reading with nothing to
satisfy is still stored as history. The link table retains voided historical links; its insert
guard permits a new link only when no effective link remains. Vehicle locking serializes the
decision so two concurrent readings cannot both satisfy the same obligation.

`cya.odometer.state.get_vehicle_odometer_states_as_of` derives each obligation's state as of T
from the obligations in force, the links and the status stream (UPCOMING, CURRENT, DUE,
OVERDUE, SATISFIED, EXEMPT when out of service at min(D, T)). Corrections change the effective
entry and invalidation voids the chain's satisfaction from that instant. Only evidence recorded
by T counts.

## Correction and invalidation (ODO-5)

`POST /api/v1/readings/{kind}/{reading_id}/corrections` accepts `kind=odometer` or
`kind=engine-hours` and JSON fields `operation_id`, `expected_tip_entry_id`, `value`, and
optional `confirm_anomaly` and `explanation`. Only the original author may correct; authorship
is authorized through the `correct_check` own-only permission policy (`ResourceContext(author_id=...)`)
and non-author attempts are denied through the standard audited-denial path (`FORBIDDEN`). The
expected tip prevents a concurrent fork; the command appends an entry and a `CORRECTION` event.
The replacement is checked against the latest valid reading that precedes the corrected root in
the original reading sequence, using the same anomaly rule as a fresh entry. In addition, if the
replacement would exceed the next effective reading in sequence (making that later reading lower
than the corrected value), the correction requires explicit confirmation (`confirm_anomaly: true`,
ODO-3, OQ-58). Later roots do not become the correction's baseline and are not rewritten or
invalidated. The replacement retains the original obligation link, and its value is effective in
as-of history from the correction's server instant while the root keeps its original place in the
sequence.

`POST /api/v1/readings/{kind}/{reading_id}/invalidations` accepts `operation_id` and a
mandatory nonblank `explanation`. An administrator may invalidate another user's chain by
naming any row. The `INVALIDATION` event excludes the entire chain; earlier values do not
revive, and another correction is refused. A fresh odometer entry can satisfy its obligation
again. Both commands use audited receipts: replay returns the original response, while reuse
of an operation id with different input is refused.

## Database guards

All five tables are append-only (update/delete rejected). The insert triggers repeat the rules so
a writer that bypasses the command cannot: skip or misstate the previous reading; store an
anomalous reading without confirmation, or a confirmation without an anomaly; change unit
mid-history; link another vehicle's entry, a different command's entry or an entry before the
window opened; or skip a threshold revision number.

## Where the schema completes or differs from the contract

- `odometer_threshold` is new (the contract has no threshold table; §3.3 and ODO-3 require one).
- `engine_hours_entry.unit` is always `h`; `odometer_entry.unit` is `mi` or `km`.
- Correction entries keep their root, name their immediate predecessor, and retain an
  independent `prior_entry` baseline for anomaly validation. `reading_event` has nullable
  type-specific foreign keys so the database can validate both kinds of chain.
- `odometer_obligation_link` is unique per `(obligation, entry)` rather than per obligation:
  an invalidated link stays in history, and a replacement link is another append-only row.
- `session_id` is **refused** (`NOT_YET_SUPPORTED`) for now. SES-2 counts an odometer entry as
  session activity; wiring that into the activity clock and the auto-close triggers is a separate
  change, not part of this issue's criteria.
- Out of scope here: reading history views, waivers and suspensions of odometer obligations.

## Failure handling and verification

Rejections use the `/api/v1/checks` error shape (`code`, `correlation_id`, `field_errors` with
schema paths only). A lost response is retried with the same `operation_id`. Verified by
`cya/odometer/tests`: commands, linking, state, database guards, HTTP, and a threaded race.
