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
| B2: paper item checks | this page |
| B3: paper odometer and engine-hours readings | this page |
| B4: paper seal verification and seal break | this page |
| B5 and B6: invalidation, crew UI | later |

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

## Paper item checks (Part B2)

A paper check is an ordinary `POST /api/v1/checks` call naming `paper_entry_batch_id`. It is saved
on its own, like every check (REC-1, LATE-6), with its own `operation_id`, and the response adds
`paper_entry: {batch_id, performed_at}`. A live check's request, digest and response are
unchanged.

**Two times.** The check row keeps `recorded_at`, the server entry instant, and repeats the batch's
performance instant in `performed_at` (null for a live check). The check's qualifying time t_c
(SPEC 5.3) is `performed_at` for a paper check and `recorded_at` for a live one:

- **Linking (SCH-1, SCH-2, SCH-6).** A paper check links to every obligation of its item
  requirement whose on-time window (E to G) or late interval (G to L) holds the performance
  instant, even when it is entered after L. It is On time or Late by that instant. One performed
  after L does not complete that obligation. Obligations, selections and the evidence that has
  already satisfied an obligation are read as the app knows them at entry, so a paper check never
  links an obligation that a known check already satisfied.
- **Compliance and as-of history (LATE-5, RPT-5a, AC-36).** Obligation state counts a check from
  its `recorded_at` (as-of inclusion is by server time, 19.3) and judges it at t_c (completion
  time, lateness, windows). An eligible paper check entered after L therefore clears Missed in
  the current view, while an as-of view before its entry still shows the miss.
- **Current item state (19.4).** The latest check of an item is the greatest
  (t_c, `recorded_at`, id), so a paper check entered later but performed earlier never becomes
  the item's current check. The attention walk ranks chains the same way.

**Printed version (LATE-4).** The check must name the batch's vehicle and printed checklist
version (`PAPER_BATCH_MISMATCH`, `PAPER_VERSION_MISMATCH`). It is resolved against that version as
in force at the performance instant, even if a later version has replaced it, with stocking
deviations and kit assignments as of then. Kit content resolves through the kit-definition
version printed on the batch; kit content whose kit version the batch does not name is refused
with `PRINTED_KIT_VERSION_MISSING`, and a kit that has since left the vehicle with
`KIT_MOVED_SINCE_PAPER`. Each refusal leaves the batch's other records saveable.

**Who and what.** Only the batch's entry actor adds records to it; another user's batch answers
`404`. The SPEC 3.3 paper-entry roles are checked again for every record. A paper check takes no
session and is ROUTINE (`400 MALFORMED_REQUEST` otherwise).

**Conflict warning (LATE-6).** A paper check warns only about an effective check already linked to
an obligation its performance instant falls in, newest by arrival; a check for another obligation
of the item does not warn. It is not a conflict when that check's actor is one of the batch's
performers (the entry actor alone does not count). The warning, proceed-or-cancel flow and
operation-id rules are those of CON-3; the warning names the newer check's `performed_at` when it
is a paper check.

**Corrections.** A correction follows COR-1 to COR-4: the entry actor is the author. The
replacement repeats the batch and performance instant and relinks at that instant.

**Database guards** (`cya/checks/migrations/0022_paper_checks.py`). The check insert trigger judges
publication, override, kit-content and kit-assignment effectiveness at t_c, refuses a paper row
that does not match its batch (vehicle, version, entry actor, performance instant), has a session,
is not ROUTINE or is after its entry instant, and requires a printed kit version for kit content;
a correction must keep its batch and instant. The link trigger refuses a paper link whose
obligation window does not hold the performance instant.

## Paper readings (Part B3)

`POST /api/v1/odometer-entries` accepts `paper_entry_batch_id` (SPEC 8.0.0 ODO-6). The entry, and
its engine-hours entry if any, repeat the batch's performance instant in `performed_at` beside
the server `recorded_at`; the response adds `paper_entry`. The same batch checks apply as for a
paper check: only the batch's entry actor adds records, the SPEC 3.3 paper roles are checked
again, and the vehicle must be the batch's.

**Place in the sequence.** A reading's place in its vehicle's sequence is the key
(performance instant or recorded instant, recorded instant, id) of its root reading. A live
reading is placed at its server instant, after every other reading, exactly as before. A paper
reading is placed at its performance instant, so it may fall between earlier and later readings.
Its previous reading is the newest effective reading placed before it; a later live reading's
previous reading is still the newest reading by place, not the paper reading entered last.

**Anomalies against both neighbours.** A paper reading is anomalous, and needs `confirm_anomaly`,
when it is lower than the previous reading, more than the plausibility threshold above it,
higher than the next reading, or (odometer only) more than the threshold below the next reading.
Later readings are never rewritten. Engine hours follow the same place, without a threshold.

**Obligation.** The reading satisfies the odometer obligation whose window, or late interval up to
L, holds its performance instant, even when entered after L, unless a reading known at entry
already satisfied it. Odometer obligation state counts the link from its recorded instant and
completes it at the performance instant, so a late paper reading clears the miss now while an
earlier as-of view keeps it.

**Corrections** keep the batch and place; the existing downstream-confirmation rule applies.

**Database guards** (`cya/odometer/migrations/0005_paper_readings.py`): the reading insert trigger
computes the expected previous reading and the next reading by place, applies the rules above,
and requires a paper reading to match its batch; the link trigger judges the early window at the
reading's qualifying instant and refuses a paper link past L.

## Paper seal records (Part B4)

`POST /api/v1/seals/verify` and `POST /api/v1/seals/break` accept `paper_entry_batch_id` (SPEC 8.0.0
LATE-7 and the §10 paper-record seal paragraph). The batch rules are those of paper checks. The
response adds `paper_entry`. A paper reseal, full inspection or audit request is not accepted:
SEAL-6 needs a fresh count and expiration for every item, which the outage sheet does not record,
and the owner has been asked how to handle it ([issue #9](https://github.com/JustApu/CheckYourAmbulance/issues/9)).

**Order of a seal's records.** A seal record's qualifying instant is its performance instant for
paper, else its server instant. Within one kit's or cabinet's stream, sequence order stays
qualifying-instant order, so every reader that orders a stream by sequence is unchanged.

**Paper seal verification ("Seal intact").** It is judged at the performance instant t:

- the seal relied on is the one intact at t (a reseal no later than t, with no break, reseal or
  audit request after it by t); with none, `422 SEAL_NOT_INTACT_AT_PERFORMANCE`;
- the kit or cabinet designation, compartment revision, covered scope, kit assignments and lines,
  nested kit seals and expiration (t's station-local date) are those at t; a kit not on the
  vehicle at t, or moved since, is `422 KIT_MOVED_SINCE_PAPER`;
- deficiencies and suspensions block when they blocked at t, as recorded by then, or block now
  (fail closed: the app cannot tell whether a deficiency still open was found after t);
- it links the obligations whose window holds t (late up to L), skipping any already satisfied by
  evidence known at entry. Obligation state completes them at t; as-of views before entry do not
  see them.

It writes no seal event, so it never changes the seal's current state.

**Paper seal break.** It is appended with its performance instant only when no seal event or
substitution of the subject has a later qualifying instant (`422 SEAL_LATER_EVIDENCE` otherwise),
so it never rewrites later evidence. A paper verification at an instant after a paper break finds
no intact seal; one before it still relies on the earlier reseal.

**Fresh evidence.** A paper verification or paper check is never fresh evidence for a live
reseal (nested kit verifications and kit counts).

**Database guards** (`cya/seals/migrations/0010_paper_seal_records.py`): seal events keep
qualifying order along the chain and a paper event may only be a break, matching its batch and
preceding no later substitution; a paper substitution must rely on the seal intact at its
instant; a paper substitution link must hold the instant in the obligation's window; a paper
substitution cannot be nested in a live cabinet inspection.

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
permissions, printed-version rules and the database guards), `test_api.py` (HTTP codes,
replay, CSRF, and the performer search) and `test_paper_checks.py` (timing, current and as-of
compliance, printed version, refusals, conflicts, ordering, corrections, guards) and
`test_paper_readings.py` (obligations, sequence and both-neighbour anomalies, corrections) and
`test_paper_seals.py` (verification at the performance instant, seal intact then, breaks, later
evidence).
