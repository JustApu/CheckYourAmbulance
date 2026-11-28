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
| B4: paper seal verification, seal break and paper reseal | this page |
| B5: batch invalidation and mistaken-Normal invalidation | this page |
| B6a: crew setup page and paper checklist screen | this page |
| B6b: history marks, timing and invalidation UI; participation credit note | this page |
| B6c: paper seal page | this page |

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
entering a new one (LATE-6, INV-8; [Part B5](#batch-invalidation-part-b5)).

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

`POST /api/v1/seals/verify`, `POST /api/v1/seals/break` and `POST /api/v1/seals/reseal` accept
`paper_entry_batch_id` (SPEC 8.5.0 LATE-7 and the §10 paper-record seal paragraph). The batch
rules are those of paper checks. The response adds `paper_entry`. An audit request is never
entered from paper.

**Order of a seal's records.** A seal record's qualifying instant is its performance instant for
paper, else its server instant. Within one kit's or cabinet's stream, sequence order stays
qualifying-instant order, so every reader that orders a stream by sequence is unchanged. A paper
break or reseal is therefore appended only when no seal event or substitution of the same kit or
cabinet has a later qualifying instant (`422 SEAL_LATER_EVIDENCE` otherwise): it never rewrites
later evidence.

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

**Paper seal break.** It is appended with its performance instant under the placement rule
above. A paper verification at an instant after a paper break finds no intact seal; one before
it still relies on the earlier reseal.

**Paper reseal (SPEC 8.5.0 SEAL-1, CAB-9, LATE-7).** The outage sheet prints a paper reseal
section for each sealed kit and sealable cabinet (see
[Paper checklists for outages](crew-inspection-ui.md#paper-checklists-for-outages)). A reseal
done during an outage is entered from it under the batch of the same sheet:

1. If the seal was intact at t, enter its break from the sheet first (a paper break of the same
   batch; it is taken as performed at t). A seal already broken earlier needs no new break.
2. Enter each covered kit's presence and each applicable kit-list count as paper checks of the
   same batch (Part B2), as for any paper check.
3. `POST /api/v1/seals/reseal` with `paper_entry_batch_id`, the new seal's `marking`, both
   attestations, `observations` (one per covered item: the result, the fresh count for a
   quantity item and the fresh expiration date for an expiration-tracked item, copied from the
   section) and `kit_count_checks` / `kit_presence_checks` naming the paper checks of step 2.
   `start_operation_id` and `nested_substitution_ids` are refused with a paper batch: there is no
   live inspection start, and a paper cabinet reseal inspects every covered kit's items (a kit
   inside whose own seal was intact at t is first broken on paper, or the cabinet is resealed
   live).

The full inspection and the reseal are taken as performed at t. Scope, designation, compartment
revision, kit assignments, kit-list lines and the expired-item rule (t's station-local date) are
judged at t. Every covered item must come from the checklist version the batch names, or from the
kit-definition version it names for that kit's definition, and that version must be the one in
force at t: otherwise the item printed on the section is no longer identifiable under LATE-4 and
the reseal is `422 PAPER_ITEM_UNIDENTIFIABLE`. A missing count or expiration date is refused like
a live one (`422 INVALID_SEAL_REQUEST`). Every refusal writes nothing, so the seal stays open
until a live full inspection and reseal. A pending audit is fulfilled by the paper reseal as by a
live one (its `AUDIT_FULFILLED` event carries the batch and t). The inspection's obligation links
are judged at t (late up to L), like a paper check's, and obligation state completes them at t.

*Inspection evidence for a paper reseal (design).* The seal_inspection tables already require
each kit-count and kit-presence row to name an immutable check record, so a paper reseal names
paper checks of its own batch rather than carrying the values itself. Such a check was copied
from the same sheet, by the same entry actor, at the same performance instant; that is what
"fresh for that paper inspection" means here. A live check is not evidence for a paper reseal,
and a paper check is never evidence for a live one. This reuses the check's own validation,
printed-version mapping, deficiency on mismatch and obligation links, adds no columns to the
seal_inspection tables (their rows reach the batch through their reseal event), and keeps a
live reseal's request, digest and response unchanged.

**Fresh evidence.** A paper verification or paper check is never fresh evidence for a live
reseal (nested kit verifications, kit counts and kit presence).

**Database guards** (`cya/seals/migrations/0010_paper_seal_records.py`): seal events keep
qualifying order along the chain; a paper event is a break, reseal or audit fulfilment (never an
audit request), matches its batch, has its kit designation and compartment revision judged at its
instant and precedes no later substitution; a paper substitution must rely on the seal intact at
its instant; a paper substitution or inspection link must hold the instant in the obligation's
window; a paper inspection has no start receipt, names only kit-count and kit-presence checks of
its own batch (a live one only live checks made after its start), with kit assignments current at
its instant, and takes no nested kit verification; a paper substitution cannot be nested in a
live cabinet inspection.

## Batch invalidation (Part B5)

Code: `cya/paper/invalidation.py` (`invalidate_paper_entry_batch`), `cya/paper/api.py`; guards in
migrations `cya/paper/migrations/0003_paper_invalidation.py`, `cya/checks/migrations/0023_paper_invalidation.py`,
`cya/odometer/migrations/0006_paper_invalidation.py`, `cya/seals/migrations/0011_paper_invalidation.py`
and `cya/deficiencies/migrations/0029_paper_batch_invalidation.py`.

A wrong paper time or performer is never edited (SPEC 8.0.0 INV-8, LATE-6). A Supervisor or
Administrator invalidates the whole batch, with a mandatory explanation, and the form is entered
again as a new batch.

`POST /api/v1/paper-entry-batches/{batch_id}/invalidations` takes `{operation_id, explanation}`.
In one transaction it appends, and never modifies:

- an `INVALIDATION` `check_event` for every check chain of the batch, with the ordinary chain
  consequences: a Failed check's deficiency stays open (INV-3), a deficiency the chain's
  correction put in Reported in Error reopens (INV-7), a supervisor completion it supported is
  voided and supervisors are notified (UTI-3);
- an `INVALIDATION` `reading_event` for every odometer and engine-hours chain of the batch, so the
  reading sequence and odometer compliance are recomputed without it;
- last, one `paper_entry_invalidation_event` naming the batch. Its guard refuses it while any
  check or reading chain of the batch is not invalidated, so the batch is invalidated as a unit.

A chain an Administrator had already invalidated keeps its own event. Paper seal records get no
event of their own; every reader applies the batch event from its instant:

- **Seal substitutions** (paper "Seal intact" verifications) and the **full inspection of a paper
  reseal** are void: they satisfy no obligation, so a fresh check, inspection or seal verification
  can take the obligation, and a voided substitution is no longer "later evidence" that blocks a
  paper seal event.
- **A paper reseal cannot support reliance** (INV-8, LATE-7). From the invalidation the seal reads
  as open: neither a live nor a paper verification may rely on that reseal (a paper verification
  at an instant after it finds no intact seal), and a fresh full inspection and reseal may start
  without a new break. Its paper `AUDIT_FULFILLED` no longer ends a pending audit, so the audit
  stays pending until a live or valid paper reseal fulfils it (fail closed).
- **Seal breaks stay in force.** The seal stays open, and a new full inspection and reseal are
  required before reliance resumes. Reviving the earlier seal would fabricate reliance no one
  recorded.

The seal readers apply only the invalidations known at their instant, so an as-of view before
the invalidation still shows the paper seal in force. Reliance is recomputed without the batch's
records, so any other substitution, live or paper, that relied on one of its paper reseals (for
example a later live "Seal intact") is void from the invalidation as well; as-of views before it
keep it.

Every record keeps its original performance and server-entry times and stays visible. Derived
compliance is recomputed without the batch's records; an as-of view before the invalidation still
shows them as they were (REG-6). An invalidated batch takes no further record: a paper check,
reading or seal record naming it is `409 PAPER_BATCH_INVALIDATED`. An invalidation is never
undone (INV-6).

**Who.** A Supervisor or Administrator (`invalidate_paper_entry_batch`), never the batch's entry
actor: they get `403 SELF_INVALIDATION_FORBIDDEN`, and another Supervisor or Administrator must do
it. "Own entry" is the batch's entry actor, not its performers: a Supervisor may invalidate a batch
another user entered that names them as a performer. Crew members and Supply Officers get `403
FORBIDDEN`. Refusals are audited as the actor's denied action.

**Response** (`201`): `batch_id`, `invalidation_event_id`, `check_invalidation_event_ids`,
`invalidated_check_root_ids`, `reading_invalidation_event_ids`, `invalidated_reading_root_ids`,
`voided_substitution_ids`, `dependent_substitution_ids` (other substitutions, live or paper,
that relied on the batch's paper reseals), `voided_inspection_ids` (inspections of the batch's paper reseals),
`voided_reseal_event_ids` (paper reseals that no longer support reliance),
`voided_audit_fulfillment_event_ids`, `seal_break_event_ids` (breaks that stay in force),
`affected_obligation_ids`, `voided_determination_ids` and `reopened_deficiency_ids`. A replay with
the same `operation_id` returns the stored result. The explanation is not copied into the audit
snapshot.

| HTTP | Code | When |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | bad JSON or fields, a batch id that is not a UUID, an explanation over 4000 characters |
| 403 | `SELF_INVALIDATION_FORBIDDEN` | the actor entered the batch |
| 403 | `FORBIDDEN` | the actor is neither a Supervisor nor an Administrator |
| 404 | `NOT_FOUND` | no such batch in the organization |
| 409 | `BATCH_INVALIDATED` | the batch is already invalidated |
| 409 | `OPERATION_REUSED` | the operation id was used for another request |
| 422 | `EXPLANATION_REQUIRED` | the explanation is missing or blank |
| 503 | `RETRY_LATER`, `AUDIT_UNAVAILABLE`, `CONFIGURATION_UNAVAILABLE` | not confirmed; retry with the same `operation_id` (also when a batch holds more than 1000 check and reading chains) |

**Lock order.** Vehicle, batch, the item locks of the batch's checks (in item order), then their
deficiencies (sorted), as for the paper record commands, so a record being added to the batch
either commits first and is invalidated too, or waits and is then refused.

**A check mistakenly saved in Normal mode.** Re-enter the same work in Paper entry mode first.
Then a Supervisor or Administrator other than the check's author invalidates the mistaken check
through `POST /api/v1/checks/{check_id}/invalidations` with `replacement_paper_entry_batch_id`
(see [paper-entry invalidation](check-records.md#paper-entry-invalidation-spec-800-inv-8) for its
rules and refusals). The Supervisor who entered the replacement batch may do it, since it is not
their check. This and batch invalidation are the only Supervisor exceptions to INV-1; every other
check invalidation stays Administrator-only.

**Database guards.** `paper_entry_invalidation_event` is append-only, one per batch; its insert
trigger requires its `PAPER_ENTRY_BATCH_INVALIDATION` receipt (naming the batch), a Supervisor or
Administrator role snapshot, an actor other than the entry actor, and every check, odometer and
engine-hours chain of the batch invalidated. The `check_event` and `reading_event` triggers accept
a batch invalidation's events only from such an actor for chains of the named batch. The check,
reading, seal event and seal substitution insert triggers refuse a paper record of an invalidated
batch; the paper-event guard ignores a voided substitution as later evidence; a seal
substitution may not rely on a paper reseal of an invalidated batch; and the
`deficiency_event` trigger accepts an INV-7 reopening under a batch invalidation receipt.

## Crew UI (Part B6a)

The crew pages are described in [crew inspection screens](crew-inspection-ui.md#paper-entry-mode);
this section lists what they read and send. Code: `cya/inspections/paper_views.py`,
`cya/paper/readers.py`, `templates/inspections/paper_entry.js` and `checklist.js` in paper mode.

- **Setup** (`/inspection/vehicles/{id}/paper-entry/?template={checklist}`): offers the
  checklist's published versions and, per placed kit definition, its published kit versions
  (`version_choices`, at most 10 each, the ones in force now preselected; the command still judges
  them at the paper time). It sends the B1 header unchanged; `kit_definition_version_ids` only
  when a kit version is chosen. A Crew member's `performer_ids` is themself; a Supervisor or
  Administrator chooses by the performer search below.
- **Paper checklist screen** (`/inspection/paper-entry/{batch}/`, the batch's entry actor only):
  reads `GET /api/v1/vehicles/{id}/checklist?view=NORMAL&needs_attention_only=false&paper_entry_batch_id={batch}`
  ([attention views](attention-views.md#the-checklist-query)): the rows of the printed version as
  a paper check of the batch resolves them, each with the batch's effective check of it
  (`paper_entry_check`). Each tap is a B2 paper check with `paper_entry_batch_id` and no
  `session_id`; the readings form is a B3 paper reading. Nothing is saved per batch.
- **Paper source on every row.** `latest_check_paper` names the performers, entry actor and entry
  instant of a row's newest check when it is a paper check, so the paper marker shows in Normal
  mode too (LATE-5).
- **Fresh print (LATE-4, LATE-8, AC-43 (9)).** The page config says whether the batch already has
  a record (`has_records`: a check, reading, seal event or seal substitution names it); if not,
  the first acknowledged record shows the print offer once.
- **Seal records link.** The paper checklist screen links "Seal records from this sheet"
  (`config.urls.paper_seals`, Part B6c below).

## History and invalidation UI (Part B6b)

The check-history page (`/inspection/vehicles/{id}/check-history/`) is described in
[crew inspection screens](crew-inspection-ui.md#paper-entries-in-check-history); this section
lists what it reads and sends. Code: `cya/inspections/workflows.py` (`check_history`,
`paper_batches`, `mistaken_normal_candidates`), `cya/inspections/workflow_views.py` (`checks`),
`templates/inspections/check_history.html`, `paper_source.html` and `workflows.js`.

- **Paper marks (LATE-1, LATE-5).** Each paper check (a row with `paper_entry_batch_id`,
  including a correction of one) shows "Paper entry", the batch's performance instant and
  performers, and its own server entry instant and entry actor, plus whether the batch is
  invalidated. Performers and invalidations are read in one bulk query each.
- **Timing (SCH-1, SCH-2).** For each obligation linked to the check, the detail view shows
  "Timing: On time" when the qualifying instant t_c (`performed_at` for a paper check, otherwise
  the server `recorded_at`) is at or before the obligation's grace deadline G, and "Late
  completion" otherwise. A link exists only for an eligible check, so t_c is never before E or
  after L. The source ("Source: Paper entry") is a separate line.
- **Paper entries list.** The vehicle's batches entered in the last 14 days, newest entry first,
  at most 20, with their checklist and revision, both times, performers, entry actor, the number
  of check entries, and any invalidation (when, by whom, explanation). "Continue copying" is
  offered only to the entry actor of a batch not invalidated (only they add records, Part B2).
- **Batch invalidation form.** Offered when `invalidate_paper_entry_batch` is permitted with
  `ResourceContext(author_id=<batch entry actor>)` and the batch is not invalidated, so the
  entry actor never gets it (the server would answer `403 SELF_INVALIDATION_FORBIDDEN`); a
  Supervisor or Administrator who entered it is told another must do it. The form
  (`data-kind="paper-batch"`) sends `POST /api/v1/paper-entry-batches/{batch}/invalidations`
  with `{operation_id, explanation}`.
- **Mistaken-Normal form.** On the detail view of an effective check whose chain is a live
  (not paper) `ROUTINE` check, when `invalidate_mistaken_normal_paper_check` is permitted with
  the check's author as `author_id` (a Supervisor or Administrator other than its author). It
  lists the candidate replacements: batches of the same vehicle, not invalidated, performed no
  later than the check's chain was saved, holding a paper check of the same work (item, kit
  instance, kit line or presence, item requirement) whose chain is not invalidated, newest
  performance first, at most 20. These mirror the command's rules, which still judge the choice.
  With no candidate, the page says to copy the sheet through Paper entry first. The form
  (`data-kind="mistaken-normal"`) sends `POST /api/v1/checks/{check}/invalidations` with
  `{operation_id, explanation, replacement_paper_entry_batch_id}`.
- **Client rules.** Both forms post to constant paths in `workflows.js` with the id as one
  percent-encoded segment (never empty, `.` or `..`; the issue #27 rule, pinned in
  `test_workflow_scripts.py`), never to a URL read from the page. The request and its operation
  id are kept whole until a definite answer (REC-4): a lost response or a 5xx is retried
  unchanged, and only a refusal or success ends it. Refusals are listed in the form's error
  summary (`role="alert"`, focused) with the named field marked `aria-invalid`, in plain words
  for `SELF_INVALIDATION_FORBIDDEN`, `FORBIDDEN`, `NOT_FOUND`, `BATCH_INVALIDATED`,
  `PAPER_BATCH_INVALIDATED`, `EXPLANATION_REQUIRED`, `CHAIN_INVALIDATED`,
  `PAPER_CHECK_NOT_NORMAL`, `REPLACEMENT_NOT_APPLICABLE`, `REPLACEMENT_BATCH_MISMATCH`,
  `REPLACEMENT_BATCH_INVALIDATED`, `REPLACEMENT_PERFORMED_LATER` and
  `REPLACEMENT_CHECK_MISSING`.

**Participation credit (RPT-4, LATE-3).** History credits the attributed performers of a paper
check ("performed … by A, B") and shows the entry actor only as the person who entered it. A
future participation report must do the same: count a paper check for each of its batch's
`paper_entry_performer` rows, and give the entry actor no participation credit for entering
someone else's paper work (an entry actor who is also a named performer is credited as a
performer). **Gap:** the app has no participation report yet (RPT-4 is not implemented), so
nothing counts participation today; this rule is recorded here for when it is built.

## Paper seals page (Part B6c)

The page is described in [crew inspection screens](crew-inspection-ui.md#paper-seal-records);
this section lists what it reads and sends. Code: `cya/seals/views.py` (`paper_seals`),
`cya/seals/api.py` (`inspection_scope`), `templates/seals/vehicle.html` and `vehicle.js` with
`config.paper_entry`.

- **Page** (`/inspection/paper-entry/{batch}/seals/`, the batch's entry actor only; anyone else's
  batch and a malformed id answer 404). The live seal page's template and script in paper mode:
  `config.paper_entry` carries the batch id, the printed `checklist_version_id`, the performance
  instant and its label, the performers (plain text, read through `json_script`), `has_records`
  and `state` (`OPEN` or `INVALIDATED`). `can_request_audit` is false: an audit request is never
  entered from paper (B4). The subject list is the live one (`GET /api/v1/vehicles/{id}/seals`),
  so each command's `expected_tip_id` is the current tip the B4 commands compare against; the
  commands judge everything else at the sheet's time.
- **Scope at the sheet's time.** `GET /api/v1/vehicles/{id}/seals/{kit|slot}/{subject}?paper_entry_batch_id={batch}`
  reads the reseal scope (items, kit presence, kit-list lines) at the batch's `performed_at`. The
  batch must be the user's own and of that vehicle (else `404 NOT_FOUND`). No other query key is
  taken with it, the key is given once, and a malformed id is `400 MALFORMED_REQUEST`. Without
  the key the read is unchanged (now). A subject not sealable at the sheet's time is 404.
- **Seal intact on the sheet** sends B4's `POST /api/v1/seals/verify` with `paper_entry_batch_id`.
  **Seal broken on the sheet** (offered unless the seal is open now) sends `POST /api/v1/seals/break`
  with it. **Resealed on the sheet** (offered when the seal is open now; a seal still intact is
  first broken from the sheet) reads the scope at the sheet's time, with no inspection start.
  Its kit-list counts and kit-presence counts are B2 paper checks: `paper_entry_batch_id` and the
  batch's `checklist_version_id`, never `inspection_start_operation_id` or `session_id`. The reseal
  (`POST /api/v1/seals/reseal`) names the batch and those checks, never `start_operation_id` or
  `nested_substitution_ids`; a cabinet's nested kit seals are not offered for verification.
- **Client rules.** A request and its operation id are kept whole until a definite answer
  (REC-4): a lost response or a 5xx is retried unchanged, and no other record starts meanwhile.
  Refusals are announced in an alert summary (`role="alert"`), with the input to fix marked
  `aria-invalid` for a missing entry, in plain words for `SEAL_NOT_INTACT_AT_PERFORMANCE`,
  `SEAL_LATER_EVIDENCE`, `PAPER_ITEM_UNIDENTIFIABLE`, `KIT_MOVED_SINCE_PAPER`,
  `PAPER_BATCH_INVALIDATED`, `PAPER_BATCH_MISMATCH`, `NOT_FOUND`, `SEAL_CHANGED`, `FORBIDDEN`,
  `INVALID_SEAL_REQUEST` and the paper check codes (`PAPER_VERSION_MISMATCH`,
  `PRINTED_KIT_VERSION_MISSING`, `ITEM_NOT_IN_CHECKLIST`, `CHECKLIST_UNAVAILABLE`); any other
  code is named. The page navigates only by server-rendered links, so it adds no script path.
- **Print offer.** As on the paper checklist screen: once, after the first acknowledged record of
  a batch with no records when the page opened.

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
evidence, paper kit and cabinet reseals, refusals and guards) and `test_invalidation.py`
(batch invalidation of checks, readings and seal records, as-of history, deficiency effects,
permissions, refusals, database guards, HTTP, and the mistaken-Normal invalidation).
Part B6a: `cya/attention/tests/test_paper_rows.py` (printed-version rows after a replacement,
printed kit content, a kit moved after the paper time, the batch's check through correction and
invalidation, item reads, paper sources), `cya/inspections/tests/test_paper_entry_ui.py` (pages,
access, config, escaping, the checklist read and its refusals), and the Node harnesses
`paper_entry_script_harness.js` and `workflow_script_harness.js` (paper mode).
Part B6b: `cya/inspections/tests/test_paper_history_ui.py` (paper marks, On time and Late
completion apart from the source, the paper entries list, batch and mistaken-Normal form
visibility per role including the entry actor, replacement candidates, escaping),
`test_workflow_scripts.py` (the constant invalidation paths) and `workflow_script_harness.js`
(both forms' payloads, paths, error texts, `aria-invalid` and operation-id reuse).
Part B6c: `cya/seals/tests/test_paper_seals_ui.py` (page access for the entry actor only, config,
banner, ordering hint, escaping, an invalidated batch, the paper screen's link, the scope read at
the performance instant, its 404s and its refusal of other keys) and
`cya/seals/tests/vehicle_script_harness.js` (paper buttons, the verify, break, count, presence and
reseal payloads, no inspection start or nested verification, refusal texts, `aria-invalid`,
operation-id reuse and the print offer).
