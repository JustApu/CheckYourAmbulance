# Paper entry after an outage

[Issue #9](https://github.com/JustApu/CheckYourAmbulance/issues/9) Part B implements SPEC 8.0.0
§7.9 (LATE-1 to LATE-8): records written on the outage paper forms are entered into the app
after service returns. Each form is entered as a **paper-entry batch**. The batch header says,
once for the whole form, what the paper says: the vehicle, the printed checklist version, the
printed kit-definition versions, the original performance date and time, and who did the work.
Each record on the form (a check, an odometer reading or a seal record) is then saved on its own
and names the batch. Part B is delivered in stacked parts; the issue #9 comment that claims Part
B lists them. This page grows with each part.

| Part | Status |
|---|---|
| B1: batch header, performer search, §3.3 permissions, kit-reference identifiers | this page |
| B2 to B6: paper checks, readings, seal records, invalidation, crew UI | later |

## Batch header (Part B1)

Code: `cya/paper/commands.py` (`record_paper_entry_batch`), `cya/paper/api.py`,
`cya/paper/models.py`; guards in migration `cya/paper/migrations/0002_paper_entry_guards.py`.

`POST /api/v1/paper-entry-batches` takes JSON:

| Field | Meaning |
|---|---|
| `operation_id` | version 4 UUID, the idempotency key (REC-4) |
| `vehicle_id` | the vehicle on the form |
| `checklist_version_id` | the "Version ID" printed on the outage sheet |
| `performed_on`, `performed_time` | the date (`YYYY-MM-DD`) and time (`HH:MM`) written on the form, in the station's local time |
| `performer_ids` | one to 20 users who did the work on paper |
| `kit_definition_version_ids` | optional: the kit versions printed on the kit reference used, at most one per kit definition |

A header saves no item result. There is no batch Submit: each later record auto-saves on its own
(LATE-6). A successful call answers `201` with `batch_id`, the stored values and an explanation
of the time conversion. A retry with the same `operation_id` returns the original result; the
same id with a different request is `409 OPERATION_REUSED`.

**Two times, two people.** The header stores the paper performance instant (`performed_at`, in
UTC) separately from the server entry instant and entry actor (`recorded_at`, `actor_user_id`),
which come from the command receipt as for every other record and are never taken from the
request (§7.2, TIME-4). The attributed performers are child rows (`paper_entry_performer`); the
entry actor is not a performer unless named as one (LATE-3).

**Time conversion (LATE-2).** The written date and time are read in the organization's station
zone (TIME-2) and resolved under TIME-3: a time in a spring-forward gap moves to the first valid
instant after the gap (`GAP_FORWARD`), and a repeated fall-back time takes its first occurrence
(`AMBIGUOUS_FIRST`). The header keeps the written date and time, the zone, the UTC offset used
and that resolution, so the conversion can be explained later. A time after the server's now is
refused (`422 PERFORMED_IN_FUTURE`). There is no age limit and no outage declaration.

**Who may enter (§3.3, LATE-3).** A Crew member may name only themself
(`enter_own_paper_record`). A Supervisor or Administrator may name any users of the
organization, active or not, themself included (`enter_paper_record_for_others`). A Supply
Officer with neither role may not enter paper records. A refusal is `403 FORBIDDEN` and is
audited as a denied action.

**Printed versions (LATE-4).** The checklist version must be a version of one of this vehicle's
own checklists, published by the entry instant and in force by the performance instant. A
version that has since been replaced is still accepted, because the printed version governs.
Each kit-definition version must be published by the entry instant, in force by the performance
instant, and of a kit definition that the printed checklist version places. Refusals:

| HTTP | Code | When |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | bad JSON, field, date, time (whole minutes only), UUID, or a repeated id |
| 404 | `NOT_FOUND` | vehicle, published checklist version, published kit version or performer absent |
| 422 | `VEHICLE_INACTIVE` | the vehicle is inactive |
| 422 | `VERSION_NOT_FOR_VEHICLE` | the checklist version belongs to another vehicle's checklist |
| 422 | `VERSION_NOT_IN_FORCE`, `KIT_VERSION_NOT_IN_FORCE` | not yet in force at the performance instant |
| 422 | `KIT_VERSION_NOT_IN_CHECKLIST` | the printed checklist version does not place that kit definition |
| 422 | `KIT_VERSION_AMBIGUOUS` | two versions of one kit definition were named |
| 422 | `PERFORMED_IN_FUTURE` | the performance instant is after now |
| 503 | `RETRY_LATER`, `AUDIT_UNAVAILABLE`, `CONFIGURATION_UNAVAILABLE` | not confirmed; retry with the same `operation_id` |

**Database guards.** All three tables (`paper_entry_batch`, `paper_entry_performer`,
`paper_entry_kit_version`) are append-only. Insert triggers repeat the command's rules: the
batch must match its `PAPER_ENTRY_BATCH_RECORD` receipt and name a version of its vehicle's own
checklist in force at the performance instant; child rows must belong to their batch's own
command; a performer other than the entry actor needs a Supervisor or Administrator role
snapshot; and a kit version must be published, in force and placed by the batch's checklist
version. A batch is never edited: wrong metadata is corrected by invalidating the batch and
entering a new one (LATE-6, INV-8; Part B5).

## Performer search

`GET /api/v1/paper-entry-performers?q=<2 to 100 characters>` finds users by display name or
email for a Supervisor or Administrator choosing performers (LATE-3). Unlike the Roles page, it
includes inactive users, because the person who did the paper check may have left since. It
returns at most 20 users (`user_id`, `display_name`, `email`, `active`) and `truncated: true`
when there are more, so the caller refines the search. Crew members and Supply Officers get
`403 FORBIDDEN`: they cannot attribute paper work to others.

## Kit reference identifiers

The printed kit reference (`/inspection/kit-reference/`, issue #9 Part A) now prints each placed
kit definition's version identifier beside its name ("Kit version …"), as DEP-11 and LATE-4
require, so a batch header can name the kit versions the crew used. Legacy kit rows that belong
to a checklist version itself have no separate kit version and print none.

## Verification

Offline tests in `cya/paper/tests/`: `test_batches.py` (time resolution, the command, its
permissions, printed-version rules and the database guards) and `test_api.py` (HTTP codes,
replay, CSRF, and the performer search).
