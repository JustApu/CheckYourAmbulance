# Vehicle operational status and out-of-service exemption

[issue #57](https://github.com/JustApu/CheckYourAmbulance-archive/issues/57) implements [SPEC §9 (OPS-1–OPS-6)](../SPEC.md#9-operational-status) and
[§5.9](../SPEC.md#59-out-of-service-exemption-c-req-22) on the `vehicle_status_event`
and `status_request` tables of [contracts §2](data-model-contracts.md#identity-and-fleet).
It does not generate obligations or send notifications. Those consumers call the read
functions below; the endpoints and screens are [described further down](#http-endpoints-and-screens).

## Statuses

`IN_SERVICE`, `LIMITED_SERVICE` and `OUT_OF_SERVICE`, as OPS-1 names them. The issue text
listed `IN_SERVICE_DEFECT_PENDING` in place of Limited Service; SPEC wins, and OPS-2 says a
deficiency never changes status by itself, so no defect-driven state exists. Limited service
records free-text `restrictions` (OPS-3); the other statuses carry none. Both are enforced by
command validation and by a database CHECK.

## Data

- `vehicle_status_event` (append-only): one linear chain per vehicle. `sequence` is unique per
  vehicle starting at 1, `prior_event_id` names the preceding event (also unique, so a chain
  cannot fork), and `recorded_at` is the server instant of the command (TIME-4) and never runs
  backwards along the chain. The explanation is required.
- `status_request` (append-only): a crew `REQUEST` row, or a supervisor `APPROVE`/`DENY` row
  pointing at it. A request is decided once; a denial needs an explanation (OPS-4); an approval
  comes with the status event it produced in the same command.
- **There is no status column on `vehicle`.** Current status is the latest event; a vehicle that
  has never had an event has no recorded status and is not treated as out of service.

Migration 0002 adds the organization-pair foreign keys, UUID and role-snapshot checks,
append-only triggers (UPDATE/DELETE refused even for the schema owner) and insert triggers
that require each row to match its command receipt, the chain to be gapless, and a decision to
answer a `REQUEST` for the same vehicle and status. The runtime account gets SELECT/INSERT only,
through `audit_runtime_grants` like any other append-only table.

## Commands (`cya.operations.commands`)

All run through `run_command`: one transaction, an operation-id receipt (the same id replays the
original result), and a success/failure audit. They take IDs and intent, never client roles.

| Function | Who | Effect |
|---|---|---|
| `set_vehicle_status` | Supervisor, Administrator | Appends an event; explanation required; refuses a no-op |
| `request_status_change` | Any role the policy lets request (Crew, Supervisor, Administrator) | Appends a `REQUEST`; changes nothing else |
| `decide_status_request` | Supervisor, Administrator | `APPROVE` appends the requested status as an event (restrictions for limited service), `DENY` needs an explanation |

Authorization is the `set_vehicle_status` policy row: crew are `request_only`, supply officers
denied. The vehicle row is locked `FOR UPDATE`, then the newest event is read with a locking
read, so concurrent writers on one vehicle get consecutive sequence numbers. Nothing reacts to
deficiencies, checks or certifications (OPS-2, AC-19).

**Notifications** ([issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14), OPS-1, NOT-3). In the same transaction, a status change (set directly or
by an approved request) notifies all supervisors and administrators, a request notifies all
supervisors, and a decision notifies the requester; the actor is never notified, and administrators
can change the recipients ([notifications](notifications.md#which-events-notify-and-whom)). A
failure to write a notification rolls the command back; delivery failures never do.

## Out-of-service reads (`cya.operations.status`)

A status holds from its event's `recorded_at` up to, excluding, the next event's. Events may
share a `recorded_at`; `sequence` orders them, so a status followed by another at the same
instant was never in force and yields no out-of-service span.

- `current_status`, `status_at(instant)`: the event in force.
- `out_of_service_at(instant)`: the `OUT_OF_SERVICE` event in force, else `None`.
- `out_of_service_intervals(start, end)` / `out_of_service_during(start, end)`: spans of the
  closed horizon (for example `[E, G]`) that were out of service, clipped to it. A period that
  begins exactly at `end` overlaps; one that ended exactly at `start` does not.
- `routine_exemption(due_at)`: the event that exempts a routine obligation due at `D`, else
  `None`. The rule is the status **at the due instant**: out of service at `D` means exempt
  ("Exempt — out of service", never Pass); in service at `D` means the obligation exists even
  if the window began out of service.

**No backfill (OQ-31).** Events are append-only and server-timed, so the answer for a past
instant never changes. A due instant inside an outage stays exempt after the vehicle returns,
and returning to service creates nothing; only later due instants are ordinary. A supervisor's
ad hoc inspection is a separate scoped obligation, outside this issue.

## HTTP endpoints and screens

[issue #148](https://github.com/JustApu/CheckYourAmbulance-archive/issues/148) exposes the three commands over authenticated same-origin JSON and gives them screens. It adds no
rule: the command's `set_vehicle_status` permission still decides, a hidden form is only a convenience,
and status never changes from any other event (OPS-2, AC-19). A recorded Critical Failed check, an
overdue item or an expired certification leaves the status and the request queue untouched; a regression
test records one and asserts exactly that.

| Method and path | Who | Effect |
|---|---|---|
| `POST /api/v1/vehicles/{id}/status-changes` | Supervisor, Administrator | `set_vehicle_status`. Body: `operation_id`, `status`, `explanation`, optional `restrictions` (required for `LIMITED_SERVICE`, refused otherwise). |
| `POST /api/v1/vehicles/{id}/status-requests` | Crew, Supervisor, Administrator | `request_status_change`. Body: `operation_id`, `requested_status`, `reason`. |
| `POST /api/v1/status-requests/{id}/decisions` | Supervisor, Administrator | `decide_status_request`. Body: `operation_id`, `decision` (`APPROVE` or `DENY`), `explanation` (always required), optional `restrictions` when approving a Limited Service request. |
| `GET /api/v1/vehicles/{id}/status` | any built-in role | `current` event (or `null`), `history` (newest 100, newest first, `history_truncated`) and the vehicle's `pending_requests`. |
| `GET /api/v1/status-requests` | any built-in role | Undecided requests, oldest first (at most 200), optionally narrowed by `?vehicle_id=`. |

Posts answer `{status: "COMMITTED", result}` with the command's own result body, or the shared rejection
shape `{status: "REJECTED", code, field_errors}`. Codes: `MALFORMED_REQUEST` (400, unknown or missing fields, blank
text, a bad status, restrictions on the wrong status, ids that are not canonical UUIDs), `FORBIDDEN` (403),
`NOT_FOUND` (404, a vehicle or request that is missing or belongs to another organization), `NO_CHANGE`
(409, the vehicle already has that status and restrictions), `ALREADY_DECIDED` (409, the request has an
outcome), `VEHICLE_INACTIVE` (409), `OPERATION_REUSED` (409, the id carried different content) and
`RETRY_LATER` / `AUDIT_UNAVAILABLE` (503, retry with the same operation id). Reads refuse unknown or repeated
query parameters, and a user with no current built-in role gets 403 (deny by default, AC-24).

**Races.** Two supervisors deciding one request: the vehicle row lock serializes them, one outcome is
recorded and the other receives `ALREADY_DECIDED`. A replay of the same `operation_id` returns the original
result and publishes nothing again.

**Screens** (the Vehicle status section, every built-in role may read it):

- `/status/` lists active vehicles with their status and how many requests are waiting.
- `/status/vehicles/{id}/` shows the current status with who set it and when, restrictions and the
  explanation, history, and pending requests. Crew see a request form. Supervisors and Administrators see the
  set-status form and, per pending request, an Approve form (with a restrictions field for a Limited Service
  request) and a Deny form; each needs an explanation. An Out of Service vehicle says routine inspections are
  **Exempt**, never Passed (AC-20); the checklist screens keep deciding that per obligation from
  `cya.attention.checklist`, which this issue reuses and does not duplicate.
- Each form shows Pending, Saved, Failed or Unconfirmed. After a lost response the retry sends the identical
  request and operation id, and no refresh, live or forced after another form's save, replaces a form that is saving or unconfirmed: the re-read waits until the last such form settles. The check is repeated when a panel response arrives, so a form submitted while a read was in flight is not replaced by that response either.
- `/status/vehicles/{id}/panel/` is the same page's live part alone, which the open page re-reads.

**Live hints.** After commit the three commands publish identifier-only, organization-scoped hints on the
vehicle's group ([live updates](live-updates.md)): `status.changed` carrying the new status event id (setting a
status, approving a request) and `status.request` carrying the request or decision row id (filing, approving
or denying; an approval publishes both). A rolled-back or refused command publishes nothing. The status screen
re-reads its panel on a hint (debounced) and on every reconnect; the checklist screen already re-reads on any
hint, so an open checklist shows the new status banner too.

## Not in this issue

The supervisor dashboard ([issue #17](https://github.com/JustApu/CheckYourAmbulance/issues/17)). The obligation generator is in
[obligation generation](obligation-generation.md): it applies the same rule as `routine_exemption` through
`out_of_service_at`, at the due instant for one already reached and at the current status for a future one.

## Verification

`python manage.py test cya.operations --noinput` covers the chain, permissions, validation, the
request/decision flow, replay, concurrent writers (two connections), the database guards, the
interval boundaries, the HTTP endpoints and pages for every role, the live hints (delivered on commit,
silent on rollback, refusal and replay) and, through Node when it is installed, the browser script
(`cya/operations/tests/status_script_harness.js`, with a stub DOM, fetch and socket; no real browser). See the [development environment](../CONTRIBUTING.md#python-and-django).

## Require an ad hoc inspection after return to service ([issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158))

Returning to In Service or Limited Service creates **no** inspection obligation
(OQ-31). The status panel offers Supervisors and Administrators an explicit
**Require an ad hoc inspection** form, with individual item/physical-kit choices,
a due time labelled UTC and a required explanation. Crew and Supply Officer roles
alone cannot order one; permissions combine when someone also holds Supervisor.

`POST /api/v1/vehicles/{id}/ad-hoc-inspections` accepts:

```json
{
  "operation_id": "d960e2e0-1dd6-4ff7-b36b-10d3a889c2a4",
  "items": [{"item_id": "1ed271e4-7072-47c4-becb-f67070652645", "kit_instance_id": null}],
  "due_at": "2027-01-01T18:00:00Z",
  "explanation": "Synthetic return-to-service inspection."
}
```

These IDs and the example are synthetic. The server derives organization and
actor from sign-in, loads current roles in the command transaction and validates
1–200 unique item/kit pairs against the vehicle's published checklist effective
at the future due time, using configuration known when ordered. Choose checkable
items; vehicle-reading rows use the separate odometer workflow. It locks the
vehicle, its bound templates and chosen item locks, then creates the immutable
requirement and obligation rows together with receipt and audit. Any failure rolls
back the entire order. Unknown/inactive vehicles, wrong item/kit scope, a blank
explanation or a nonfuture/naive due instant are refused. Reusing the same operation
and content returns the original obligation IDs, even after the deadline; a different
request with that operation ID conflicts. No dry-run is provided.

The window opens at the server-recorded request instant and has no extra grace
past the due time. The [obligation state reader](obligation-state.md#explicit-ad-hoc-inspections-issue-158)
retains explanation, actor and command identity. Existing checklist readers show
the derived state beside each item. Generation never duplicates or retires these
orders, and later publication/kit changes preserve the historical requirement.
An identifier-only `obligations.changed` hint publishes after commit; connected
status/checklist clients re-read. The form shares the existing Pending, Saved,
Failed and Unconfirmed behavior, including identical operation/content retries
when acknowledgement is lost. If checklist choices exceed a reader bound or cannot
be resolved, the panel shows an unavailable notice for the order form while
retaining operational status and its existing controls. It never changes operational status.
