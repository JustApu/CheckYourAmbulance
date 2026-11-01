# Deficiencies: creation, evidence and links

[issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80) adds the deficiency tables and the three ways to bring one into being: a Failed or
Unable to Inspect check (`POST /api/v1/checks`), an independent report
(`POST /api/v1/deficiencies`), and an informational link between two existing deficiencies
(SPEC §8.1–§8.3, DEF-1..DEF-3; [contracts](data-model-contracts.md) "Checks and deficiencies",
"Report a deficiency independently"). Code: `cya/deficiencies/` (`models.py`, `service.py`,
`commands.py`, `api.py`, `transitions.py`) and the call in `cya/checks/commands.py`. The M4
lifecycle handlers reuse the closed transition table and the lock protocol below; [issue #146](https://github.com/JustApu/CheckYourAmbulance-archive/issues/146)
adds [assignment, deadlines and starting work](#assignment-deadlines-and-starting-work), [issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149)
adds [Awaiting External Dependency and progress notes](#awaiting-external-dependency-and-progress-notes),
[issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153) adds correction and its scope, [issue #154](https://github.com/JustApu/CheckYourAmbulance-archive/issues/154) adds verification checks and automatic closure, and
[issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13) adds [supervisor check suspension](#supervisor-check-suspension).

## Tables

| Table | Kind | Role |
|---|---|---|
| `deficiency_category` | V | Effective-dated revisions of a category; `supply_related` is read from the revision in force, never from the request |
| `deficiency_severity` | V | Effective-dated revisions of a severity level: code, label, rank, concise criteria, active flag (SPEC DEF-8) |
| `deficiency` | M | Immutable creation header (a trigger rejects any change to a creation fact); no state column |
| `deficiency_lock` | M | One empty row per deficiency, held to serialize every writer of it |
| `deficiency_projection` | M | Current status, assignee, deadline and severity level revision, with the event it last applied; disposable and rebuildable from the events |
| `deficiency_affected_item` | A | The scope snapshot at creation: item and definition pairs |
| `deficiency_event` | A | Append-only lifecycle events; status is the latest event's `to_status` |
| `verification_scope_row` | A | Append-only correction-time scope revision; item and checklist definition plus physical kit instance, or explicit no-kit key; required-after instant |
| `deficiency_scope_expansion` | A | Items (definition, physical kit instance or no-kit key) a supervisor added with `SCOPE_EXPANDED` or `VERIFICATION_ADDED`; every later correction's scope includes them ([issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)) |
| `check_suspension` | A | `START` of a supervisor suspension of one routine check (item and physical kit instance, or no-kit key) and its `END`, written by the event that closed or cancelled the deficiency ([issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13)) |
| `deficiency_evidence` | A | What supports the deficiency: a Failed or Unable check today. Never removed |
| `deficiency_link` | A | Informational `ADD`/`REMOVE` history of a pair, stored lower id first |
| `deficiency_age_rule` | A | Administrator-set revisions of the age-highlight rule: thresholds in days and the long-running test; none until configured (SPEC DEF-6, [issue #150](https://github.com/JustApu/CheckYourAmbulance-archive/issues/150)) |
| `deficiency_acknowledgement_rule` | A | Administrator-set maximum acknowledgement period; no default (SPEC DEF-7, [issue #151](https://github.com/JustApu/CheckYourAmbulance-archive/issues/151)) |

## Explicit deficiency closure and reopening

`POST /api/v1/deficiencies/{id}/administrator-closures` closes any active deficiency as
`CLOSED_ADMINISTRATOR`; only an Administrator may use it. The separate
`POST /api/v1/deficiencies/{id}/empty-scope-closures` closes an active deficiency as
`CLOSED_UNVERIFIABLE` only before correction has been recorded and when current correction-time
scope is empty; Supervisors and Administrators may use it. A deficiency in `PENDING_VERIFICATION`
keeps the scope materialized by its correction and cannot use this route. Both require
`operation_id`, `expected_event_id` and a nonblank
`explanation`. The server re-resolves empty scope under the vehicle, checklist-template and
deficiency locks used by correction recording, rejects stale event IDs with `409
DEFICIENCY_CHANGED`, and refuses an empty-scope closure if any current verification item remains
(`422 SCOPE_AVAILABLE`). Each command records its distinct append-only event and actor. Replays
with the same operation ID return the stored result. The Administrator-only
`POST /api/v1/deficiencies/{id}/administrator-reopenings` returns either explicit-closure status
to `OPEN`; verified or `REPORTED_IN_ERROR` deficiencies cannot use it.

If checklist publication or kit-assignment changes remove every current verification item after
a deficiency was reported but before correction, correction remains refused as `422 EMPTY_SCOPE`
and records no repair event. There is no fallback to report-time items. The authorized
empty-scope closure records that
the deficiency was closed without verification; it does not claim that a repair occurred. Its
event and status are shown distinctly in history. Both closure types end every
[check suspension](#supervisor-check-suspension) still in force and list it in
`suspensions_ended` (the response repeats the list); neither changes vehicle status, and reopening
resumes no suspension. The assignment/history read includes both closure
event types and administrator reopen events, including their explanation and suspension list;
for those lifecycle entries, assignee and deadline show the values in effect at that point. These
commands only change deficiency state and never vehicle status. An invalidated Failed check still
follows its correction/invalidation path and does not close a deficiency.

Every table has organization-pair foreign keys, UUID and role-snapshot checks, and the runtime
account gets `SELECT, INSERT` on `deficiency` and the A and V tables and `UPDATE` only on the
projection and lock (`tools/audit_enforcement.py` lists `deficiency` as an insert-only header).
The insert triggers repeat the command rules at the database: a header, event, evidence or link
row must match its command receipt (organization, actor, instant, command type, and the header's
operation id); a category and a severity level must be the revision in force at that instant (not a superseded or retired one); a kit must be the vehicle's own; evidence
must be a Failed or Unable check of the deficiency's vehicle written by the same command; an
event chain cannot skip or fork; a link history alternates on its own pair and cannot fork.

## Creating from a check

`deficiency` on `POST /api/v1/checks` is exactly one of `{"existing_id": id}` or
`{"create": {category_id, severity, summary, description}}`. `severity` is a level code (below).

- **Required for `FAILED` and `UNABLE_TO_INSPECT`** (including a quantity count that derives
  Failed): `422 DEFICIENCY_REQUIRED`. Any other outcome with a `deficiency` is
  `422 DEFICIENCY_NOT_ALLOWED`. Unable still needs its reason (`EXPLANATION_REQUIRED`).
- **The server never picks a match.** The crew member names an existing deficiency or asks for a
  new one; the suggestion query is read-only (below).
- **Atomic.** The check, its links, the deficiency (header, lock, scope, initial event,
  projection) and the evidence row commit with the receipt and audit, or none of them does.
  A replay returns the stored result and creates nothing; a conflict warning (`CHECK_CONFLICT`)
  also creates nothing, so a Pass and a Fail that race are both kept once the warned writer
  proceeds, and exactly one deficiency results (AC-10, data half).
- **Existing deficiency.** It must be in the organization (`404 NOT_FOUND` otherwise, also for
  another organization's), on the check's vehicle (`DEFICIENCY_VEHICLE_MISMATCH`) and still
  active (`DEFICIENCY_NOT_ACTIVE` for Closed or Reported in Error). The deficiency is locked
  after the item lock. Several failed checks may support one deficiency (DEF-3). Added evidence
  does not widen the scope snapshot; scope expansion is a supervisor action in M4.
- **New deficiency.** It concerns the checked item (and the kit instance, for a kit item); its
  affected scope is that item and the definition checked. It reuses the check's `operation_id`
  as its own creation operation and the check's receipt, so the one receipt describes every
  effect. The reporter is the check's author.
- Response `result.deficiency_ids` has the id; `event_ids` has the creation event for a new
  deficiency and is empty when evidence was added to an existing one.
- Permission: `record_check`, and `create_deficiency` for a new one (all four roles).
  Assignment and deadline cannot be sent here; a check-created deficiency is unassigned and
  stays visible for a supervisor to assign (SPEC §3.3). The UI must not invent an assignee.

## Severity levels

Severity is data, not a fixed list (SPEC DEF-8, OQ-56). The reporter sends a level `code`; the
server resolves the revision in force and stores that revision on the frozen header
(`severity_version_id`), so a later reword or retirement never changes an old report. An unknown,
retired or not-yet-effective code is `422 INVALID_SEVERITY` (path `severity`, or
`deficiency.create.severity` on a check). The starting levels, seeded by `manage.py
create_organization` and by migration 0003 for existing organizations, are Low, Medium, High and
Critical, each with a concise criteria description (`cya/deficiencies/severity.py`).

`GET /api/v1/deficiency-severities` returns the levels a reporter may choose now, least severe
first, as `{code, label, description, rank}`: the screen shows the label and criteria beside the
choice. An administrator adds, rewords, reorders or retires a level by appending a revision
(`deficiency_severity` is a V table: no update or delete). **Not built here:** the audited
administrator command and screen that append revisions (and the same for categories); until they
exist, revisions are inserted by the schema owner. Severity never changes a vehicle's operational
status (OPS-2).

## Independent report

`POST /api/v1/deficiencies` takes `operation_id`, `vehicle_id`, `target`, `category_id`,
`severity`, `summary`, `description` and optionally `related_deficiency_ids`, `assignee_id`,
`deadline`. `target` is exactly one of `{item_id}`, `{kit_id}`, `{compartment_id}` or
`{vehicle: true, system_ids: [...]}`. `session_id` answers `422 NOT_YET_SUPPORTED` until session
wiring is implemented.

- **Scope is resolved on the server**, from the checklists the vehicle currently follows: the
  union of every published template with a version in force, vehicle override included (a
  template with none in force does not apply to it; any other resolution failure, including a
  slot the vehicle lacks, is `409 CHECKLIST_UNAVAILABLE` and the report rolls back). An item target stores the item; a
  kit target stores that physical kit and snapshots its current checklist items; a compartment
  target stores the compartment and snapshots every item in the vehicle's own
  compartment of that slot (kit items included); a systems target stores only the vehicle and
  snapshots the items of those systems. A target covering no checklist item is
  `422 EMPTY_SCOPE`: a deficiency that affects nothing could never be verified.
- **Assignment and deadline** need the `assign_deficiency_deadline` privilege (SPEC §3.3):
  supervisors and administrators, and supply officers only when the category revision in force
  is `supply_related`. Otherwise `403 FORBIDDEN`, audited as the actor's denied action. The
  assignee must be an active user of the organization (`INVALID_ASSIGNEE`, the same answer for
  unknown, foreign and inactive); a deadline must be a future, timezone-aware instant. Both are
  carried on the creation event and the projection. A crew report is unassigned and visible.
- **Related links** are explicit `related_deficiency_ids` (at most 50, no repeats, each in the
  organization; they also require `link_unlink_deficiencies`), locked in id order for the whole command. They create `ADD` rows only; nothing
  is merged and no other deficiency changes (DEF-2).
- Result: `deficiency_id`, `creation_event_id`, `related_link_event_ids`, `server_recorded_at`.

## Links and suggestions

`POST /api/v1/deficiencies/{id}/links` and `.../link-removals` with `operation_id` and
`related_deficiency_id` (the contract names no URL for these two; they are this issue's
addition). Any of the four roles may link and unlink (`link_unlink_deficiencies`). Both
deficiencies are locked sorted by id. A pair is one set of rows in canonical order, so naming it
from either side is the same link; the newest row decides whether it is linked. `ALREADY_LINKED`
and `NOT_LINKED` are `409`; a self-link is `422 SELF_LINK`. A link or its removal writes only its
own audited row: no assignee, status, projection or event of either deficiency changes, and
nothing closes (DEF-2). Removal keeps the history, and the pair can be linked again.

`GET /api/v1/deficiencies/suggestions?vehicle_id=&item_id=` (item optional) lists up to 20
active deficiencies of the vehicle, newest first, narrowed to those whose scope covers the item.
It reads only; nothing is selected, linked or attached by asking.

## Corrections and invalidations

[issue #81](https://github.com/JustApu/CheckYourAmbulance-archive/issues/81); SPEC COR-3, COR-4, INV-3, INV-7, AC-13, AC-14, AC-32. Code: `cya/deficiencies/effects.py`,
called from `correct_check` and `invalidate_chain`. The deficiencies a chain supports are those
with `deficiency_evidence` from any row of the chain; one check may support several.

- **Failed to Passed Correct-entry** (the correction command, tip outcome Failed). Each supporting
  deficiency is locked (sorted by id, after the item lock the command already holds) and then:
  - **Reported in Error** (`REPORTED_IN_ERROR` event, status `REPORTED_IN_ERROR`, ending every
    [check suspension](#supervisor-check-suspension) still in force, SPEC SUS-4) only if its
    status has that arrow in SPEC 8.2 (Open, In Progress, Pending Verification), no *other*
    effective chain still ends Failed or Unable to Inspect, and no event other than the
    creation, correction-effect and reopening events exists (any future repair, corrective
    action or verification event type blocks it, because only listed types are neutral).
  - otherwise it **stays open** and a `CORRECTION_NOTICE` event (status unchanged) records the
    intent to notify a supervisor; delivery of notifications is M4.
  - a deficiency already closed or cancelled is left alone. Unable to Inspect to Passed does
    not act on its deficiency at all (UTI-3, INV-3).
  - Deliberate, owner-confirmed rule (SPEC COR-4 as amended by 1.13.0, OQ-57): another effective
    Failed **or Unable to Inspect** chain keeps the deficiency open, because an unverified item
    is not a resolved defect.
    "Effective" means the chain's replay outcome (`cya.checks.state.effective_checks`): an
    invalidated chain, or one since corrected to Passed, does not count.
- **Invalidation** never closes or cancels a deficiency (INV-3). If a supporting deficiency is
  still Reported in Error and its *last* event is this chain's `REPORTED_IN_ERROR`, a
  `REOPENED` event returns it to Open in the same transaction, attributed to the administrator,
  with the invalidation's explanation (INV-7). The reopening resumes no ended check suspension. A deficiency cancelled by another chain, or closed
  independently, is untouched. The chain stays in the evidence list (evidence is never removed);
  its invalid state is read from the chain, so no earlier outcome revives, no repair is inferred
  and no vehicle status changes (OPS-2). The response fills `reopened_deficiency_ids`.
- **Atomic, ordered, replayable.** The effects commit with the correction or invalidation, its
  receipt and audit, or none does (tested by failing the effect write). A replay returns the
  stored result and appends nothing. A correction racing an invalidation of one chain
  serializes on the item lock: either the correction is refused (`CHAIN_INVALIDATED`) or it
  cancels and the invalidation reopens; two chains of one deficiency corrected together
  serialize on its lock, so exactly one cancels it and the other leaves a notice.
- **Bounded work.** A chain may support at most 100 deficiencies (more is `503
  CONFIGURATION_UNAVAILABLE` before any lock), and evaluating "other effective support" reads at
  most 1,000 other chains and 10,000 check or event rows. Each deficiency's own history is
  checked for repair or verification events by reading at most 1,001 event types (newest first).
  Over any of these three bounds the answer is "still supported" or "may hold repair work", so
  the deficiency stays open with a notice and nothing is cancelled.
- **Database guard.** The event insert trigger requires the receipt type (correction or
  invalidation command), that the named chain supports the deficiency and was corrected or
  invalidated by that same command, and for a reopening that its immediate predecessor is the
  `REPORTED_IN_ERROR` event naming the chain. The evidence guard no longer needs the check to be written by the attaching command, so a
  Failed check can support more than one deficiency of its vehicle.
- Each effect event carries `correction_ref` (the chain's root check id), a system explanation (no
  free text), and moves `deficiency_projection` to its `to_status`.

## Record a correction

`POST /api/v1/deficiencies/{id}/corrections` takes `operation_id` and `expected_event_id`.
Any organization role may record the repair. The command is accepted only while the deficiency
is In Progress; it appends `CORRECTION_RECORDED`, moves the projection to Pending Verification,
and writes the complete scope revision in the same transaction. Stale event IDs return
`409 DEFICIENCY_CHANGED`; an empty or inconsistent resolved scope fails closed with
`422 EMPTY_SCOPE`. An initially empty target is refused at report time; current correction scope
can later become empty after checklist publication or kit-assignment changes. In that case the
scope-specific explicit closure route described below is available before a correction is
recorded. The command never falls back to the report-time affected-item snapshot.

For kit and compartment deficiencies, current effective checklist definitions and physical kit
assignment history are resolved under the vehicle lock at correction time. Membership added after
the report is included. A compartment containing two physical kits with the same logical item
produces two scope rows, one per kit instance. Vehicle-level deficiencies use their report-time
`deficiency_affected_item` snapshot. Every target type also includes the deficiency's supervisor
expansions ([below](#supervisor-scope-expansion-and-added-verification)). A verification row
stores the item, the checklist definition that supplied it, its kit instance or explicit no-kit
key, and the correction instant. A same-item pass in another kit therefore cannot stand in for
the required physical instance. A kit item with no active assigned instance produces no row for
that item; if the complete scope is empty, correction is refused.

The correction event counts as recorded repair work for COR-4. A later Correct-entry of a
supporting Failed check therefore leaves the deficiency open and records the supervisor notice.

For an item-level deficiency the scope is one row per active physical kit instance of a
kit-defined item, or one row for an item outside any kit; each row needs its own qualifying pass
(SPEC VER-3, amendment 1.20.0).

## Verification and automatic closure

A verification check (`POST /api/v1/checks` with `purpose` `VERIFICATION`; see
[check records](check-records.md#verification-checks)) is the only way scope is satisfied.
Ordinary routine passes never count (VER-6, AC-16). Under the deficiency lock the check's own
transaction appends one of two events, each naming the check in `verification_check_id` and
carrying nothing else:

- `VERIFICATION_CLOSED`: Pending Verification to Closed Verified when a counting Pass leaves no
  row of the newest scope revision unsatisfied. The projection moves in the same transaction, and
  every [check suspension](#supervisor-check-suspension) still in force ends with it and is listed
  in `suspensions_ended` (SPEC SUS-3; the response's `verification.suspensions_ended` repeats it).
- `VERIFICATION_FAILED`: Pending Verification to In Progress when a verification check Fails; the
  Failed check is also added as `deficiency_evidence`.

The event trigger requires the check to be that command's own verification check for this
deficiency (a Pass for a closure, a Fail for a return) and, for a closure, every row of the newest
revision satisfied by an effective Pass recorded strictly after the row's instant. Both event
types are repair/verification work for COR-4 (not in the neutral list).

INV-3 holds over verification. If an administrator invalidated a check chain that supports the
deficiency (the original Failed check, or any other supporting evidence), a verification Pass is
still recorded and counts, but the command does not append `VERIFICATION_CLOSED` (the response
says `closure_withheld: true`) and the closure trigger refuses one. The deficiency stays Pending
Verification until an explicit closure under §8.8, which is generally Administrator-only.

## Invalidating or correcting a verification check

[issue #155](https://github.com/JustApu/CheckYourAmbulance-archive/issues/155); SPEC INV-4, INV-2a, VER-4. Code: `cya/deficiencies/effects.py`
(`plan_invalidation_effects`, `plan_verification_correction_effects`), called from
`invalidate_chain` and `correct_check`.

- **What counts is recomputed, not remembered.** Scope satisfaction is always derived from the
  effective verification Passes (VER-4), so an invalidated chain, or one its author corrected from
  Passed to Failed or Unable to Inspect, simply stops counting for its row. If another effective
  Pass of the same row and revision remains, the row stays satisfied.
- **Pending Verification stays Pending Verification.** No event is appended; the row needs a new
  qualifying Pass before the deficiency can close.
- **A verified closure that loses its support reopens** (INV-4, fail toward open). When the
  deficiency is Closed Verified, the chain was recorded against the revision its newest
  `VERIFICATION_CLOSED` satisfied (or an earlier revision of the same correction that an expansion
  repeated), and the remaining effective Passes no longer satisfy every row of
  that revision, the same transaction appends `VERIFICATION_REOPENED` (Closed Verified to Open)
  with a system explanation, naming the chain's root in `verification_check_id`, and moves the
  projection. A Pass of another correction's revision, or a chain whose row another Pass still satisfies,
  leaves the closure as it is. A deficiency closed by an administrator is untouched. The
  invalidation response lists it in `reopened_deficiency_ids`; a correction lists the event in
  `deficiency_event_ids` and `deficiency_effects`. The chain and its rows are retained and no
  earlier outcome revives (INV-2a); an as-of read before the reopening still shows the closure
  (`dependency_state(..., as_of=...)`). A correction never closes a deficiency and does not
  return it to In Progress; that is what a new Failed verification attempt does (VER-5).
- **Locks.** The deficiencies a chain supports as evidence and the one it verifies are locked
  together, once, sorted by id, after the item lock, so mixed sets cannot be taken in opposite
  orders. A correction and an invalidation of one chain serialize on the item lock: the
  reopening happens once, whichever commits first.
- **Database guard.** The event trigger accepts `VERIFICATION_REOPENED` only from a check
  invalidation or correction command that invalidated or corrected the named verification chain
  of this deficiency, from Closed Verified to Open, when the chain is of the closing revision and
  some row of that revision has no effective Pass left. A CHECK constraint
  (`defevent_verification_reopen_shape`) allows only the explanation and `verification_check_id`.
  `VERIFICATION_REOPENED` records no repair, so it is neutral for COR-4.
- Notification intents to the assignee and supervisors are [issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14); this command writes none.

## Supervisor scope expansion and added verification

[issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156); SPEC VER-3, VER-4, §8.7 and amendment 1.22.0 (owner decisions recorded on [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)),
AC-16, AC-17. Code: `cya/deficiencies/expansion.py`. Both commands need the `expand_verification`
permission (Supervisor, Administrator); Crew and Supply Officers are refused and the denial is
audited. Each request names `expected_event_id` (409 `DEFICIENCY_CHANGED` when stale), an
`explanation` and 1 to 50 `items`, each `{item_id, kit_instance_id?}`: the item must be on the
vehicle's current checklist (`422 ITEM_NOT_IN_CHECKLIST`), a kit-defined item names one of its
active physical kit instances on the vehicle (`KIT_REQUIRED`), and any other item names none
(`KIT_NOT_ALLOWED`). An item may lie outside the deficiency's own target.

- **Expansion while Pending Verification** (`POST /api/v1/deficiencies/{id}/scope-expansions`)
  appends `SCOPE_EXPANDED` (status unchanged) and a new scope revision of the same correction. It
  repeats every row of the newest revision with that row's own instant and adds the named rows,
  whose instant is the expansion instant, so only a Pass recorded after the expansion satisfies
  an added row. A Pass that satisfied a repeated row still satisfies it, so the crew keep their
  work, and automatic closure needs every added row too. An item already in the newest revision
  is refused (`422 ALREADY_IN_SCOPE`); any other status is `422 INVALID_TRANSITION` (a closed
  deficiency takes added verification instead). The response gives `scope_revision` and
  `scope_count`.
- **Added verification after closure** (`POST /api/v1/deficiencies/{id}/added-verifications`,
  also requiring a future `deadline`) is accepted only while the deficiency is Closed Verified. It
  appends `VERIFICATION_ADDED`, which reopens it to Open with the new resolution deadline. The
  former deadline stays in the history (`assignment_state`, `extension_state`, as-of reads), the
  assignee is kept, and a pending extension request is superseded as by any new deadline. The
  named items are verified after the next correction. Notifying only the current assignee is
  [issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14); this command writes no notification intent.
- **Expansions persist.** The named items are kept as `deficiency_scope_expansion` rows. Every
  later correction's scope includes each of them, resolved like the rest of the correction-time
  scope: the item must still be on the vehicle's current checklist (its current definition is
  used), and a kit instance must still hold that item there, so a kit moved to another compartment
  or retired drops out instead of blocking the correction. A pair the scope already holds is not
  repeated. A new correction carries no earlier Pass over (VER-4).
- **Locks and races.** The vehicle, checklist-template and deficiency locks are taken in the
  correction command's order. A last verification Pass therefore either commits first (closing the
  deficiency, after which the stale expansion is refused with 409 and is never turned into a
  reopening), or waits and is recorded against the expanded revision, where the added rows remain
  open. Both orders are tested with two connections.
- **Database guards.** `defevent_scope_shape` allows only the explanation and, for
  `VERIFICATION_ADDED`, the deadline. The event trigger requires each event's own command type, a
  Supervisor or Administrator actor role, the status pair and, for added verification, a future
  deadline. The scope-row trigger accepts an expansion's rows only from the command that wrote the
  `SCOPE_EXPANDED` event, keeps the correction of the revision being expanded, and requires each
  repeated row to keep that revision's instant. A row repeated verbatim skips the kit-placement
  and target checks it passed when first written, so a kit moved since cannot block an expansion
  (and is not dropped). An added row outside the target must match one of the deficiency's
  expansions by item and instance. The `deficiency_scope_expansion` trigger checks the receipt,
  the event, the kit key, and that the kit instance is active on the vehicle in the slot the
  item's definition names. The closure trigger now counts a Pass across the
  correction's expansion revisions, and refuses a closure whose revision drops a row of an
  earlier revision of the same correction (an expansion never shrinks scope).
- Both event types record no repair: they are not in the COR-4 neutral list, because a
  deficiency that has them has already had a correction recorded.

## Supervisor check suspension

[issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13); SPEC §8.9 SUS-1 to SUS-4, §5.4, VW-2, AC-18. Code: `cya/deficiencies/suspension.py`, the
closing commands, and the obligation and attention readers ([obligation state](obligation-state.md#check-and-suspension-adapters),
[attention views](attention-views.md#the-checklist-query)).

`POST /api/v1/deficiencies/{id}/check-suspensions` takes `operation_id`, `expected_event_id`, 1 to 50
`items` (`{item_id, kit_instance_id?}`) and an optional `explanation` (nonblank, at most 4,000
characters). It needs the `suspend_routine_checks` permission (Supervisor, Administrator); Crew and
Supply Officers are refused and the denial is audited. Each item must be on the vehicle's current
checklist (`422 ITEM_NOT_IN_CHECKLIST`); a kit-defined item names one of its active physical kit
instances on the vehicle (`KIT_REQUIRED`) and any other item names none (`KIT_NOT_ALLOWED`). Each
item and kit instance must lie within the deficiency's current affected scope, the scope a
correction would resolve now: its report-time target plus supervisor expansions, against today's
checklist and kit placement (`422 ITEM_NOT_IN_SCOPE`, owner decision 2026-11-01 on PR #29). A
deficiency on one kit instance therefore cannot suspend the other instance. The deficiency must still be worked (Open, In Progress,
Awaiting External Dependency or Pending Verification; otherwise `422 DEFICIENCY_NOT_ACTIVE`), must
not already suspend that item and kit (`422 ALREADY_SUSPENDED`), and may hold at most 200
suspensions at once (`422 TOO_MANY_SUSPENSIONS`). A stale `expected_event_id` is
`409 DEFICIENCY_CHANGED`; a replay with the same `operation_id` returns the stored answer.

- **Effect.** One `CHECKS_SUSPENDED` event (status unchanged, the optional explanation, nothing
  else) and one `check_suspension` `START` row per item, in one audited transaction; the projection
  moves only its last applied event. The response gives `event_id`, `status`, `items` and
  `suspension_ids`. The audit snapshot holds identifiers only. Nothing else changes: no status,
  assignee, deadline, scope or vehicle status (SUS-2, OPS-2). Another deficiency may suspend the
  same item; the item is suspended while any suspension of it is in force.
- **What it does to obligations.** The item's routine obligations (for a kit item, only those of the
  named physical instance) show `SUSPENDED`, displayed "Known Deficiency — Check Suspended". They are
  never Satisfied, a check recorded anyway does not satisfy them, and they leave Needs Attention Only
  but stay in the full checklist with the deficiency beside them (AC-18). Every other check stays
  required. An obligation already satisfied by a check recorded before the suspension started stays
  Satisfied (owner decision 2026-11-01): a suspension never revokes completed work. Explicit ad hoc
  orders (ADH-1) are not routine and are never suspended. See
  [obligation state](obligation-state.md#pure-evaluator-and-precedence) for the instant at which a
  suspension is judged.
- **Ending.** Nothing ends a suspension except the deficiency leaving the active statuses: verified
  closure (SUS-3), administrator closure, empty-scope closure and Reported in Error (SUS-4; a
  suspension with no open deficiency is invalid). That event lists, in `suspensions_ended`, the
  sorted ids of every `START` still in force, and the same command writes one `END` row naming each
  (`prior_suspension_id`) and the event. Reopening (administrator, INV-7 or added verification)
  resumes none; suspending again is a new decision with its own `START`. There is no command to end
  a suspension by hand.
- **Correction effects.** `CHECKS_SUSPENDED` is in `NEUTRAL_EVENT_TYPES`: it records no repair, so a
  later qualifying author correction still marks the deficiency Reported in Error, which then ends
  the suspensions.
- **Locks and races.** The vehicle, checklist-template and deficiency locks are taken in the
  correction command's order. A closure that commits first leaves a stale suspension request to fail
  with 409 (and a fresh one with `DEFICIENCY_NOT_ACTIVE`); a suspension that commits first is ended by
  the closure waiting behind it. Both orders are tested with two connections.
- **Database guards** (migration `0017_check_suspension`). CHECK `defevent_suspension_shape` keeps
  the event's closed shape; `defevent_suspensions_closing` allows a nonempty `suspensions_ended` only
  on the four closing types (it replaces `defevent_suspensions_empty`, and the empty-list rule is
  dropped from `defevent_admin_close_shape` and `defevent_verification_shape`). The event trigger
  requires `CHECKS_SUSPENDED` to have its own receipt (`DEFICIENCY_CHECKS_SUSPEND`), a Supervisor or
  Administrator role snapshot and an active, unchanged status; it requires every closing event to list
  exactly the deficiency's `START` rows still in force (an array of distinct id strings: no missing,
  foreign, ended, repeated or malformed entry) and every other event to list none. The
  `check_suspension` trigger requires the receipt of the row's own command type, the deficiency's
  vehicle, and the instance key; a `START` must belong to its command's `CHECKS_SUSPENDED` event, which
  is the deficiency's newest and active, must not repeat a suspension in force, and its kit instance
  must be active on the vehicle in the item's slot; an `END` must name a `START` of the same
  deficiency, item and kit, and the closing event of its own command that lists it (and a `START` has
  at most one `END`). Rows are append-only. Reversing the migration is refused once a suspension
  exists.
- **Reader.** `GET /api/v1/deficiencies/{id}/check-suspensions` returns `deficiency_status`,
  `last_event_id`, `in_force` (the ids not yet ended) and `suspensions`: each `START` with its item,
  kit, `started_at`, `started_by`, explanation and, once ended, `ended_at` and the ending event's id
  and type. `?as_of=<instant>` answers for that moment (AC-26, AC-27): an instant before the
  deficiency was reported is `404`, a malformed or naive one `400`. Any signed-in member may read it.
  The answer is bounded (`503` rather than truncated).
- **Deficiency page.** The history lists a start as "Routine checks suspended", and an event that
  ended suspensions shows how many ("Check suspensions ended: 1"). **Not built here:** the supervisor screen
  ([issue #16](https://github.com/JustApu/CheckYourAmbulance/issues/16)) and the fleet dashboard ([issue #17](https://github.com/JustApu/CheckYourAmbulance/issues/17)).

## The closed transition table

`cya/deficiencies/transitions.py` holds the SPEC §8.2 arrows as one set and
`validate_transition(from, to)`, which rejects every other pair (`None` is creation and may go
only to Open). [issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80) and [issue #81](https://github.com/JustApu/CheckYourAmbulance-archive/issues/81) exercise creation, Reported in Error and its reopening; the
event type check admits `REPORTED`, `REPORTED_IN_ERROR`, `CORRECTION_NOTICE` and `REOPENED`, and
M4 widens it together with its handlers, which must call the same validator under the
deficiency lock. Reporting never changes a vehicle's operational status (OPS-2).
The arrow Pending Verification to In Progress (a Failed verification, VER-5, SPEC 1.15.0) is in the
table and is used only by `VERIFICATION_FAILED`.

## Empty-scope closure and the closed transition table

Correction scope is resolved from the current checklist and kit assignments when correction is
attempted. A deficiency can therefore lose its verification items after reporting if configuration
changes while it remains active. The correction command returns `422 EMPTY_SCOPE` and writes no
event. A Supervisor or Administrator may instead call the empty-scope closure route with an
explanation; if the command finds current items, it refuses with `422 SCOPE_AVAILABLE`, leaving the
ordinary correction path available. A deficiency already in Pending Verification keeps its
materialized correction scope and cannot use this route. The separate `EMPTY_SCOPE_CLOSED` event and
`CLOSED_UNVERIFIABLE` status make the no-verification decision explicit. Only an Administrator may
reopen either explicit-closure status, using the existing administrator-reopenings route. The
event is neutral for correction effects: closure does not imply repair and does not turn a later
check correction into repair evidence.

The command locks the vehicle, checklist templates and deficiency in the same order as correction
recording. The database requires its own command receipt, a Supervisor or Administrator role
snapshot, an active-to-closed status transition and the event's closed field shape. Scope itself
is resolved in the command because it depends on effective checklist and kit-assignment state.

`cya/deficiencies/transitions.py` holds the SPEC §8.2 arrows as one set and
`validate_transition(from, to)`, which rejects every other pair (`None` is creation and may go
only to Open). Reporting never changes vehicle operational status (OPS-2).

## Re-grading severity

[issue #123](https://github.com/JustApu/CheckYourAmbulance-archive/issues/123) makes a reported severity re-gradable as a logged event (SPEC DEF-8, §3.3, OQ-56).
`POST /api/v1/deficiencies/{id}/severity-changes` takes `operation_id`, `severity` (a level
code), `explanation` (mandatory, nonblank, at most 4,000 characters) and `expected_event_id`, and needs the
`regrade_deficiency_severity` permission: supervisors and administrators by default, denied to
the other two roles (`403 FORBIDDEN`). The command (`cya.deficiencies.commands.regrade_severity`)
locks the deficiency, then refuses unless:

- `expected_event_id` is the deficiency's newest event (`409 DEFICIENCY_CHANGED`, path
  `expected_event_id`): the person decides about the deficiency as they last saw it, and a
  concurrent event of any kind makes them reload;
- the deficiency is still being worked (`422 DEFICIENCY_NOT_ACTIVE`). A closed or cancelled
  deficiency is finished, and an event after a Reported in Error cancellation would stop its
  reopening when the correcting chain is invalidated (INV-7);
- `severity` names an active level in force now (`422 INVALID_SEVERITY` for an unknown, retired
  or not-yet-effective code) that differs from the current level (`422 SEVERITY_UNCHANGED`). A
  reworded revision of the current level is still that level.

It then appends a `SEVERITY_CHANGED` event after the newest one with the same status as its
`from_status` and `to_status`, the previous and the new level revision (`previous_severity_version_id`
and `severity_version_id`), the explanation, the actor, their roles and the instant, and moves the
projection's `severity_version_id` and last applied event, all in one audited transaction. The
header is never changed, so the grade the deficiency was reported under stays readable. Nothing
else changes: no status, assignee, deadline, link, evidence or vehicle status (OPS-2). A
re-grade is a neutral event for the correction effects: it does not count as recorded repair
work, so a later author correction can still cancel the deficiency. The audit record carries the
event and the two revision ids, not the explanation.

The crew checklist lists each deficiency under the level in force at its `as_of` instant: the
newest re-grade recorded by then, else the grade it was reported under, so a past view stays as
it was and a current view shows the supervisor's grade.

The insert trigger repeats the rules at the database: the receipt must be a `DEFICIENCY_REGRADE`
command; the level left must be the one in force (the newest re-grade's, else the header's); the
new level must be an active revision in force at that instant, of a different level; and CHECK
constraints require an explanation, a status carried over from an active status, no assignment,
deadline, plan or check-chain reference, and forbid the two level columns on every other event
type. The response acknowledges `deficiency_id`, `event_id`, `previous_severity`, `severity` and
`server_recorded_at`. A replay returns the stored answer without a second event.

`GET /api/v1/deficiencies/{id}/severity` returns `severity` (the current level: `code`,
`revision`, `label`, `description`, `rank`), `deficiency_status`, `last_event_id` (what a
re-grade names as `expected_event_id`) and `history`: the grade it was reported under, then each
re-grade with the level it left, the level it set, the explanation, the actor and the instant.
The answer is replayed from the header and the events; the projection's level is a cache of it
and the tests compare the two. Any signed-in member of the organization may read it; another
organization's deficiency is `404`. The match suggestions show the current level, not the
reported one. **Not built here:** a screen for the re-grade (the supervisor deficiency UI is
[issue #86](https://github.com/JustApu/CheckYourAmbulance-archive/issues/86)) and the administrator command that names other roles for the permission.

## Assignment, deadlines and starting work

[issue #146](https://github.com/JustApu/CheckYourAmbulance-archive/issues/146) adds three append-only lifecycle events (SPEC DEF-4, §3.3, §8.2): `ASSIGNED`,
`DEADLINE_SET` (a deadline set or revised) and `WORK_STARTED` (Open to In Progress). Each has its
own command and endpoint, all `POST` with `operation_id` and `expected_event_id`:

| Endpoint | Command | Extra field | Event |
|---|---|---|---|
| `/api/v1/deficiencies/{id}/assignments` | `assign_deficiency` | `assignee_id` | `ASSIGNED` |
| `/api/v1/deficiencies/{id}/deadlines` | `set_deadline` | `deadline` (timezone-aware ISO 8601, in the future) | `DEADLINE_SET` |
| `/api/v1/deficiencies/{id}/work-starts` | `start_work` | none | `WORK_STARTED` |

**Permission.** All three need `assign_deficiency_deadline` (SPEC §3.3 row PERM-07): Supervisors
and Administrators on any deficiency; a Supply Officer only where the deficiency's category
revision is supply related, read from the database and never from the request; Crew never.
Starting work uses the same permission, because §3.3 lists only that row for working a deficiency.
Reporting never grants assignment. A denial is audited (`DENIED`) and reveals nothing: for a
Supply Officer, who may act on only some deficiencies, an unknown deficiency is the same `403` as
one out of reach, while someone with the permission on every deficiency gets `404 NOT_FOUND`.

**Rules.** Under the deficiency lock the command refuses unless `expected_event_id` is the newest
event (`409 DEFICIENCY_CHANGED`, so two people working from the same view commit exactly one
change), the deficiency is still being worked (`422 DEFICIENCY_NOT_ACTIVE`: closed and cancelled
deficiencies take no assignment or deadline, and an event after a Reported in Error cancellation
would stop its reopening, INV-7), and the change is real (`422 ASSIGNEE_UNCHANGED`,
`DEADLINE_UNCHANGED`). A deadline in the past or not timezone-aware is `422 INVALID_DEADLINE` or
`400 MALFORMED_REQUEST`. Work starts only from Open (`422 INVALID_TRANSITION`); resuming from
Awaiting External Dependency is a different event (`DEPENDENCY_ENDED`, [issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149)), though §8.2 lists
that arrow.

**Assignee.** The assignee must be an active user of the organization who holds Crew, Supervisor
or Supply Officer (owner decision 2026-10-27; the legacy EMT grant counts as Crew). An unknown id,
a user of another organization, a disabled user and an account that holds only Administrator are
refused with the same `422 INVALID_ASSIGNEE` (path `assignee_id`), so nothing is revealed about
users. The same rule now applies to an assignee named when reporting a deficiency. Nothing
assigns a deficiency automatically: an unreported assignee stays `null`.

**Effect.** The event keeps the status except `WORK_STARTED` (`from_status` Open, `to_status` In
Progress, through `validate_transition`), carries only its own field (the assignee, or the
deadline, or neither) and no explanation, plan or check-chain reference, and the projection's
`assignee_id`, `deadline` or `status` and last applied event move with it in one audited
transaction. Every earlier assignee and deadline stays in the event history. Nothing else
changes: no link, evidence or vehicle status (OPS-2). After commit the command publishes the
identifier-only `deficiency.changed` live hint. The audit record carries the deficiency, event
and assignee ids and the event type. A replay returns the stored answer without a second event.
Notification intents are not emitted here ([issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14)).

**Correction effects.** All three types are in `NEUTRAL_EVENT_TYPES`: they record no repair, so a
Failed-to-Passed author correction still cancels the deficiency after them (COR-4, AC-13), and
invalidating that chain reopens it (INV-7). A later lifecycle event that records real repair or
verification stays out of the set and keeps blocking the cancellation; each later issue states
its own classification.

**Database guards** (migration `0006_assignment_deadlines`). CHECK constraints keep each type's
closed shape and the active-status rule; the insert trigger requires the type's own command
receipt (`DEFICIENCY_ASSIGN`, `DEFICIENCY_DEADLINE`, `DEFICIENCY_START_WORK`), an actor role
snapshot that holds Supervisor or Administrator (or Supply Officer on a supply-related
category), a deadline after the instant it is set, and, for `ASSIGNED` and for an assignee named
at creation, an active user of the organization holding a working role at that instant. The
existing predecessor, status and organization-pair foreign-key guards apply. Reversing the
migration refuses while any of these events exist.

**Reader.** `GET /api/v1/deficiencies/{id}/assignment` returns `assignee_id`, `deadline`,
`deficiency_status`, `last_event_id` (what a lifecycle command names as `expected_event_id`) and
`history` (each assignment and deadline event with its actor and instant). It is replayed from
the creation event and the events after it; the projection's three fields are a cache and the
tests compare them. `?as_of=<instant>` answers for that moment (AC-27); an instant before the
deficiency was reported is `404`, a malformed or naive one `400`. Any signed-in member may read
it. The replay refuses an oversized history (`503`) rather than truncating it.

## Awaiting External Dependency and progress notes

[issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149) adds four append-only events (SPEC §8.5, §8.1, §3.3, amendment 1.17.0): a dependency is
entered, revised and ended, and a progress note is appended. Every command is a `POST` with
`operation_id` and `expected_event_id`, and follows the re-grade and assignment conventions:
`run_command`, the deficiency lock, `409 DEFICIENCY_CHANGED` on a stale `expected_event_id`,
`422 DEFICIENCY_NOT_ACTIVE` for a closed or cancelled deficiency, replay on `operation_id`, and an
audit snapshot of identifiers only (the explanation and the note text are not in it).

| Endpoint | Command (`cya/deficiencies/dependency.py`) | Body besides the two ids | Event | Permission |
|---|---|---|---|---|
| `/api/v1/deficiencies/{id}/dependencies` | `enter_dependency` | `explanation`, optional `follow_up_at` | `DEPENDENCY_ENTERED` | `update_external_dependency` |
| `/api/v1/deficiencies/{id}/dependency-revisions` | `revise_dependency` | `explanation`, optional `follow_up_at` | `DEPENDENCY_REVISED` | `update_external_dependency` |
| `/api/v1/deficiencies/{id}/dependency-exits` | `end_dependency` | optional `return_to` (`OPEN` or `IN_PROGRESS`) | `DEPENDENCY_ENDED` | `update_external_dependency` |
| `/api/v1/deficiencies/{id}/progress-notes` | `add_progress_note` | `note` | `PROGRESS_NOTE` | `add_progress_note` |

**Permission.** Both actions give the same §3.3 cells: Crew, Supervisors and Administrators on any
deficiency; a Supply Officer only where the category revision of the deficiency is supply related,
read from the database after the lock and never from the request. `add_progress_note` is the new
§3.3 row (PERM-36). A denial is audited and, for a Supply Officer, an unknown deficiency is the same
`403` as one out of reach. No command takes or changes the deadline, the assignee or an escalation
(SPEC §8.5): the event shape forbids those fields and the projection's assignee and deadline are
never written, so Crew cannot bypass them through this command.

**The dependency record.** There is one current record per deficiency: the entering event as revised
by the later `DEPENDENCY_REVISED` events, existing only while the status is Awaiting External
Dependency (the reader decides this from the deficiency's final status, so a later closure or reopening
leaves the record in the history and never as the current one). Entering needs an Open or In Progress deficiency (the §8.2 arrows) and a nonblank
explanation of at most 4,000 characters (`400 MALFORMED_REQUEST`); a second entry while waiting is
`422 DEPENDENCY_ALREADY_RECORDED`, and revising or ending without a record is
`422 NO_ACTIVE_DEPENDENCY`. A revision states the whole record, so leaving out `follow_up_at` clears
the date, and one that changes neither text nor date is `422 DEPENDENCY_UNCHANGED`. The old text and
date stay in the events; nothing is overwritten.

**Follow-up date.** Optional, a timezone-aware instant that must lie in the future
(`422 INVALID_FOLLOW_UP`; `400` when naive or malformed). It is stored and shown by the reader and is
the only schedule fact the event carries: no reminder, notification or outbox row is created
(SPEC §8.5; notifications are [issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14)).

**Leaving.** `return_to` names `OPEN` or `IN_PROGRESS` (anything else is `400`). With none, the
deficiency returns to the status it was in before it began waiting, which the entering event records
as its `from_status`; revisions do not change it (owner decision 2026-10-27). The move goes through
`validate_transition`. A later dependency may follow an ended one; only the newest is current.

**Progress notes.** An active deficiency (Open, In Progress, Awaiting External Dependency or Pending
Verification) takes a `PROGRESS_NOTE` event that keeps the status and carries its text, of at most
4,000 characters, in `explanation`. It is attributed to the actor with the instant and is never
edited or removed. The projection moves only its last applied event.

**Correction effects.** The three dependency events are in `NEUTRAL_EVENT_TYPES`: waiting records no
repair, so after the dependency ends a Failed-to-Passed author correction still cancels the
deficiency, and while it waits the correction leaves it open with a notice (Awaiting External
Dependency has no arrow to Reported in Error). A progress note is **not** neutral: its free text may
describe work done, so a deficiency with a note stays open with a notice rather than being cancelled.
Administrator close and reopen events are also neutral under COR-4: they record administrative
status decisions, not completed repair or verification. After reopening, a qualifying author
correction may still mark the deficiency Reported in Error when all other COR-4 conditions hold
(owner decision on [issue #152](https://github.com/JustApu/CheckYourAmbulance-archive/issues/152), 2026-10-29).

**Database guards** (migration `0007_dependency_and_progress_notes`). CHECK `defevent_dependency_shape`
keeps each type's closed shape (explanation required for entering, revising and notes; none for
ending; no follow-up on ending or notes; no assignee, deadline, plan or check chain; the status moves
only along Open or In Progress to Awaiting to Open or In Progress, and a note keeps it). The insert
trigger requires the type's own command receipt (`DEFICIENCY_DEPENDENCY_ENTER`, `_REVISE`, `_END`,
`DEFICIENCY_PROGRESS_NOTE`), an actor role snapshot holding Crew, Supervisor or Administrator (or
Supply Officer on a supply-related category), and a follow-up instant after the instant it is set.
The predecessor and status chain and the organization-pair foreign keys are the existing ones.
Reversing the migration refuses while any of these events exist. The projection is a cache: only
`status` and the last applied event move, and it is rebuildable from the events.

**Readers.** `GET /api/v1/deficiencies/{id}/dependency` returns `deficiency_status`,
`awaiting_external_dependency`, `dependency` (`explanation`, `follow_up_at`, `entered_event_id`,
`entered_at`, `entered_by`, `returns_to`, and `revised_*` when revised; `null` when not waiting),
`last_event_id` (what a command names as `expected_event_id`) and `history` of every dependency
event. `GET /api/v1/deficiencies/{id}/progress-notes` returns the notes oldest first with author and
instant, plus `deficiency_status` and `last_event_id`. Both accept `as_of` (a timezone-aware
instant; before the deficiency was reported is `404`, malformed or naive `400`), are replayed from
the events, may be read by any signed-in member of the organization, and refuse an oversized
history (`503`) rather than truncating it. The deficiency detail page lists the new events with
readable labels. **Not built here:** a screen to enter a dependency or add a note, and the
dashboard display of the follow-up date.

## Deadline extensions, overdue and age

[issue #150](https://github.com/JustApu/CheckYourAmbulance-archive/issues/150) (SPEC DEF-5, DEF-6, section 3.3, 8.4) adds two `deficiency_event` types, a derived
deadline state and an administrator rule for age highlighting. Migration 0008 adds the types and
two columns (`decision`, and `request_event`, a one-to-one reference to the request a decision
answers); the insert trigger repeats the rules below.

| Route | Command | Body | Event | Permission |
|---|---|---|---|---|
| `POST /api/v1/deficiencies/{id}/extension-requests` | `request_extension` | `reason`, `proposed_deadline`, `expected_event_id`, `operation_id` | `EXTENSION_REQUESTED` | `request_extension` |
| `POST /api/v1/deficiencies/{id}/extension-decisions` | `decide_extension` | `request_event_id`, `decision` (`APPROVED`, `DENIED`, `DIFFERENT_DATE`), `explanation`, optional `deadline`, `expected_event_id`, `operation_id` | `EXTENSION_DECIDED` | `decide_extension` |
| `GET /api/v1/deficiencies/{id}/deadline` | `service.extension_state` | optional `as_of` | none | any signed-in member |
| `GET`/`POST /api/v1/deficiency-age-rule` | `age.set_age_rule` | `age_thresholds_days`, `long_running_age_days`, `long_running_extensions`, `operation_id` (all required; the last two may be null) | `deficiency_age_rule` row | `configure_schedules_thresholds_notifications` |

**Permissions.** Crew, Supervisors and Administrators may request an extension on any deficiency; a
Supply Officer only on one assigned to them, which is read from the locked projection and never from
the request. Only Supervisors and Administrators decide. Roles combine (SPEC 3.2), so a Supply
Officer who also holds Crew may request anywhere. Extensions are unlimited; each request and each
decision is an event, and neither changes the status, the assignee or the cached deadline.

**Rules.** A request needs a deficiency that has a deadline (`422 NO_DEADLINE`) and a proposed date
in the future and later than the deadline now governing (`422 INVALID_EXTENSION_DATE`). A decision
needs an explanation and names one pending request of this deficiency (`422 REQUEST_NOT_FOUND`,
`REQUEST_ALREADY_DECIDED`); approval governs the requested date, a different date is chosen by the
decider (`deadline` is required for `DIFFERENT_DATE` and accepted for nothing else, `400`), and a
denial carries no date. A decided date must also be in the future and later than the governing
deadline, so an approval whose date has passed or been overtaken is refused. Both commands need an
active deficiency, the `expected_event_id` (`409 DEFICIENCY_CHANGED`) and an `operation_id`; the
audit snapshot holds identifiers and the outcome, never the reason or the explanation.

**Deadline revisions and cycles.** A deadline revision is an event that carries a deadline and is
not an extension decision: the report (which may carry the first one) and every `DEADLINE_SET`, and
later the reopening deadline of [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156). The governing deadline is that of the newest event
carrying a deadline, so a new revision supersedes every extension decided under an older one. A
request still undecided when a revision arrives is shown with `superseded: true` and cannot be
decided (`422 REQUEST_SUPERSEDED`, owner decision of 2026-10-29): ask again against the new
deadline. An as-of answer from before the revision still returns the older governing date.

**Reader.** `GET .../deadline` returns `base_deadline`, `governing_deadline`, every request with its
status (`PENDING`, `APPROVED`, `DENIED`, `DIFFERENT_DATE`), `extension_count`, `approved_count`,
`reported_at`, `age_seconds` (since the original report, whatever the deadline, DEF-6), `overdue`
(past the governing deadline while the status is active) and `last_event_id`. Derivation lives in
`cya.deficiencies.deadlines`, which reads neither the database nor the clock, so the dashboard and
notification jobs can share it; the answer is bounded (past 5,000 events it is `503`, never cut).

**Age rule.** `deficiency_age_rule` is an append-only table of administrator-set revisions (AUD-4):
up to five ascending `age_thresholds_days`, and a long-running test by age
(`long_running_age_days`), by extension requests (`long_running_extensions`), or both. Nothing is
hard-coded: until a revision is recorded the reader reports `age_level: null` and
`long_running: false`, and age and the extension count stay plain facts (owner decision of
2026-10-27). The revision recorded by an instant applies at that instant, so as-of reads do not
change when the rule does. A revision with no thresholds and both tests off switches highlighting
off, and a repeat of the current revision is `422 AGE_RULE_UNCHANGED`. There is no age-based
notification (DEF-6). Extension requests and decisions are neutral events for an author
correction (COR-4): they never block a cancellation (owner decision of 2026-10-29).

**Database guards.** The insert trigger requires each event's own command receipt
(`DEFICIENCY_EXTENSION_REQUEST`, `DEFICIENCY_EXTENSION_DECIDE`), the role snapshot rules above, dates
after the instant and after the governing deadline, and that a decision answers an undecided request
that no later deadline-bearing event has superseded, and an approval carries exactly the
requested date (only `DIFFERENT_DATE` carries another); `request_event` is unique, so a request is
decided once. CHECKs keep each type's shape and forbid the new columns on every other type.
`deficiency_age_rule` has the receipt, next-revision and append-only triggers, UUID, role and
array CHECKs, a trigger requiring at most five strictly ascending whole numbers of days from 1 to
3,650 (the command's own rule), and organization-pair foreign keys; a stored rule that is
malformed anyway makes the reader answer `503` rather than fail.


## Acknowledging escalation

[issue #151](https://github.com/JustApu/CheckYourAmbulance-archive/issues/151) implements DEF-7 without producing notifications; escalation delivery and timing belong
to [issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14). POST /api/v1/deficiencies/{id}/escalation-acknowledgements takes operation_id,
expected_event_id, a nonblank explanation, a nonblank action_plan, and period_days. Supervisors
and Administrators may acknowledge; Crew and Supply Officers are denied. The command requires an
active deficiency, uses the deficiency lock and stale-event check, and appends
ESCALATION_ACKNOWLEDGED. Repeating an active acknowledgement or acknowledging after expiry appends a
new event; neither the earlier event nor its text is overwritten. The event and audit receipt commit
atomically, and the audit snapshot contains identifiers only. No notification or outbox event is
written here.

An administrator must first configure the organization's maximum at
POST /api/v1/deficiency-acknowledgement-maximum with operation_id and positive integer maximum_days.
This appends a versioned, audited deficiency_acknowledgement_rule row using the existing
configure_schedules_thresholds_notifications permission. There is no default. Until a maximum is
configured the command returns 422 ACKNOWLEDGEMENT_MAXIMUM_UNCONFIGURED; a requested period above
it returns 422 ACKNOWLEDGEMENT_PERIOD_TOO_LONG. Repeating the current maximum returns
422 ACKNOWLEDGEMENT_MAXIMUM_UNCHANGED. GET on the same route returns the current and historical
maximum, optionally as of an instant.

GET /api/v1/deficiencies/{id}/escalation-acknowledgement returns the current acknowledgement,
history and the maximum in force now or at ?as_of=<instant>. It derives NONE, ACTIVE or EXPIRED from
the recorded expiry and time; a later acknowledgement marks earlier history entries RENEWED.
Expiry is automatic and requires no write or job. The last event id is global to the deficiency so
subsequent commands can use it as expected_event_id; the bounded reader refuses more than 5,000 acknowledgement events rather than truncating history.

**Correction effects.** `ESCALATION_ACKNOWLEDGED` is neutral for COR-4: it records intent and
suspends escalation, not completed repair, so it does not block a later qualifying author
correction from marking the deficiency Reported in Error (owner decision 2026-10-29).

The database trigger repeats the command receipt, supervisor/administrator role, active-status,
required-text, expiry and configured-maximum rules. Acknowledgement events and configuration revisions are append-only and use organization-pair
foreign keys. Reversing the migration is
refused once either kind of record exists.

## Locks and failure handling

Lock order is vehicles, kits, items, deficiencies (contract step 4). `record_check` holds the
item lock and then at most one deficiency lock (a verification check locks the deficiency it
verifies, or the one its Failed check names, never both); `report_deficiency` and the link commands lock
deficiencies sorted by id after the identity locks. A new deficiency's lock row is created in
the same transaction as the header. A missing lock row or projection is a
`503 CONFIGURATION_UNAVAILABLE`, never created lazily. A deadlock is retried at most twice by
`run_command`; an ambiguous write is not retried blindly; every rejection rolls back whole and
leaves the `operation_id` usable. These commands have no dry-run: a command either commits all
effects or none, and the operation id makes the call idempotent. Verify with the offline tests in
`cya/deficiencies/tests/`.

## Where the schema completes or differs from the contract

- `deficiency` carries both the contract's `reporter` and `reported_at` and the shared evidence
  columns (`actor_user`, `recorded_at`, `command`, `actor_roles_at_time`); a trigger requires
  them equal, because the receipt guard is defined on the shared columns.
- `deficiency_category` has `stable_category_id` (the id a client sends) beside the revision's
  own `id` (`category_version_id` on the header), as slots do. No command creates categories yet;
  administrator configuration of them, with the audit of flag changes, is separate work.
- `deficiency_evidence.kit_inspection_item_id` waits for the sealed-kit tables; `note` exists but
  nothing writes it yet. The "exactly one source" CHECK is widened with that column.
- `deficiency_link` names its two keys `lower` and `upper`.
- A target of one item may also carry the item's kit instance; a compartment target carries
  neither item nor kit (CHECK `deficiency_one_target`).
- `deficiency.severity_version_id` (a reference to the level revision) replaces a literal severity column; the contract's `severity` is read through it (`code`).
- `deficiency_event` gains `decision` and `request_event_id` ([issue #150](https://github.com/JustApu/CheckYourAmbulance-archive/issues/150)), set only on `EXTENSION_DECIDED`; the contract's `proposed_deadline` is written by `EXTENSION_REQUESTED` and `deficiency_age_rule` is new (the contract has no age table; DEF-6 requires one).
- `deficiency_scope_expansion` is new ([issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)); the contract's `verification_scope_row` note "expansion appends, never shrinks past evidence" is implemented by the expansion revision and the closure guard.
- `check_suspension` ([issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13)) adds `definition_id`, nullable `kit_instance_id` and the non-null `instance_key` beside `item_id`, so a suspension names one physical kit instance; one `deficiency_event_id` names the `CHECKS_SUSPENDED` event for a `START` and the closing event for an `END` (the contract's `ending_deficiency_event_id`); the contract's `starts_at` is the `START` row's `recorded_at` (a suspension starts when it is recorded). `deficiency_event` gains the type `CHECKS_SUSPENDED`, and `suspensions_ended` holds the sorted `START` ids a closing event ended.
- `deficiency_event` gains `verification_check_id` ([issue #154](https://github.com/JustApu/CheckYourAmbulance-archive/issues/154)), set only on `VERIFICATION_CLOSED` and `VERIFICATION_FAILED`, and ([issue #155](https://github.com/JustApu/CheckYourAmbulance-archive/issues/155)) on `VERIFICATION_REOPENED`, where it names the root of the verification chain that lost its Pass.
- `deficiency_event` gains `previous_severity_version_id` and `severity_version_id` ([issue #123](https://github.com/JustApu/CheckYourAmbulance-archive/issues/123)), set only on `SEVERITY_CHANGED`; `deficiency_projection` gains `severity_version_id`, the level revision now in force.

## Crew and supervisor screens

[issue #86](https://github.com/JustApu/CheckYourAmbulance-archive/issues/86) exposes these commands through the [deficiency and supervisor screens](deficiency-workflows.md): explicit match/create selection on Failed/Unable checks, independent reports, informational links, own Correct entry, administrator invalidation and supervisor completion. The write commands and their authorization/transaction contracts are unchanged.
