# Check records and the record-check command

[issue #73](https://github.com/JustApu/CheckYourAmbulance-archive/issues/73) adds the append-only `check_record` table and the audited command behind
`POST /api/v1/checks` (SPEC §7.1–§7.3, REC-1, REC-4; [contracts](data-model-contracts.md)
"Record a check"). [issue #74](https://github.com/JustApu/CheckYourAmbulance-archive/issues/74) adds append-only `check_obligation_link` rows to the same
transaction. One call records one attributed, item-level check and links it to an eligible
obligation when one exists: a selected routine obligation or an explicit ad hoc order
(SPEC ADH-1, [issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158)). Shared-rule extras are explicit and audited; an ordinary
check never silently satisfies multiple requirements.

[issue #76](https://github.com/JustApu/CheckYourAmbulance-archive/issues/76) adds author-only correction chains at `POST /api/v1/checks/{check_id}/corrections`.
Each accepted correction appends a replacement check and a `check_event` in the same audited
transaction. The expected tip and existing vehicle/item lock prevent concurrent forks. The
original author is retained across the chain; a supervisor or administrator may record an
independent check but cannot correct someone else's chain. Corrections have no time limit by
default, isolated in `_correction_time_limit_elapsed` for the OQ-32 owner decision. The
`PASSED` outcome and an Unable-to-Inspect to `FAILED` correction are accepted; the latter
requires existing deficiency evidence for the chain. A Failed to Passed correction acts on the
deficiencies its chain supports ([issue #81](https://github.com/JustApu/CheckYourAmbulance-archive/issues/81), [Deficiencies](deficiencies.md#corrections-and-invalidations)),
and outcomes needing other deficiency effects are refused explicitly. A verification chain follows
its own rule ([issue #155](https://github.com/JustApu/CheckYourAmbulance-archive/issues/155)): only a Pass corrected to `FAILED` or `UNABLE_TO_INSPECT` is accepted; see
[Verification checks](#verification-checks).

[issue #77](https://github.com/JustApu/CheckYourAmbulance-archive/issues/77) adds administrator invalidation of a whole chain at
`POST /api/v1/checks/{check_id}/invalidations` (SPEC §7.8, INV-1..INV-6): see
[Administrator invalidation](#administrator-invalidation).

[issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) Part A adds `POST /api/v1/obligations/{obligation_id}/unable-completions`
with `operation_id`, `check_id` and a mandatory explanation. Supervisors and
administrators can complete an open obligation only through an effective linked
Unable check. The command records an attributed `SUPERVISOR_COMPLETED` determination
and `determination_check` row atomically under the check's item lock. The obligation
displays “Completed by supervisor — unable to inspect”, never Passed. OQ-41's
approved rule allows a supervisor to complete their own check and flags that
result in projection output. An administrator can append a direct invalidation at
`POST /api/v1/obligation-determinations/{determination_id}/invalidations`; it requires
an explanation and leaves the prior determination in history.

[issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) Part B applies UTI-3 inside the check-chain transaction. Correcting the supporting
Unable check to Passed or Failed appends a `SUPERSEDED` child to each current completion;
the corrected outcome governs and the linked deficiency is unchanged. A quantity or measurement
correction from Unable needs its typed observation. Invalidating the chain appends `INVALIDATED`
children and returns the original completion IDs in `voided_determination_ids`. Completions
already superseded or directly invalidated receive no second child. Historical queries before
the chain event retain the supervisor-completed state. A fresh Unable check after chain
invalidation can be completed again. These effects share the item lock and command receipt,
are bounded to 100 current completions per chain (503 above that), and replay without new rows.
Migration `0009_unable_check_chain_effects` checks the supporting chain, causal check event,
actor, receipt, instant and child type in the database; reversal is refused while effects exist.

Both Unable-to-Inspect determination routes return the common rejection shape below. Their
`INVALID_REQUEST` code is the existing endpoint-specific name for a malformed request; the
generic check endpoint calls that case `MALFORMED_REQUEST`. An absent target and a target in
another organization have the same `404` response. A refusal writes no determination, link,
or command receipt. These two JSON bodies have a 16 KiB limit.

| HTTP | Code | When |
|---|---|---|
| 400 | `INVALID_REQUEST` | malformed JSON, body fields, UUID or explanation |
| 401 | `AUTHENTICATION_REQUIRED` | no active session |
| 403 | `FORBIDDEN` | role is not permitted |
| 404 | `NOT_FOUND` | obligation, check or determination absent or inaccessible |
| 409 | `OBLIGATION_ALREADY_DETERMINED`, `OBLIGATION_ALREADY_SATISFIED`, `NOT_CURRENT_DETERMINATION`, `OPERATION_REUSED`, `OPERATION_OWNED_BY_ANOTHER_ACTOR` | state or operation conflict |
| 422 | `INELIGIBLE_CHECK`, `INELIGIBLE_OBLIGATION`, `SELF_COMPLETION_NOT_ALLOWED` | rule refusal |
| 503 | `ITEM_LOCK_UNAVAILABLE`, `CONFIGURATION_UNAVAILABLE`, `AUDIT_UNAVAILABLE`, `RETRY_LATER` | required service or configuration is unavailable; no success acknowledgement |

An existing completion that is no longer the current determination returns `409
NOT_CURRENT_DETERMINATION`. A completion row missing its required supporting check is a
configuration failure (`503`), not a state conflict. After a transport failure or `RETRY_LATER`,
retry with the same operation ID to recover a possible committed result. `ITEM_LOCK_UNAVAILABLE`,
`CONFIGURATION_UNAVAILABLE`, and `AUDIT_UNAVAILABLE` require an operator to restore the missing
lock, configuration, or audit service; do not automatically retry them. A manual retry after
recovery uses the same operation ID.

## What a call does

Code: `cya/checks/commands.py` (`record_check`, `correct_check`), `cya/checks/api.py` (HTTP), `cya/checks/models.py`.

1. The HTTP handler needs a signed-in user (401 otherwise), a valid CSRF token, `Content-Type:
   application/json` and a body of at most 256 KiB. The actor, organization and roles come from
   the session and the database, never from the body; an `actor_user_id`, `roles` or
   `organization_id` field is refused as an unknown field.
2. Shape errors (types, canonical lowercase UUIDs, a version 4 `operation_id`, sizes) are
   rejected before any transaction.
3. `run_command` opens the one outermost transaction, re-reads and locks the actor, authorizes
   the `record_check` action (all four roles, deny by default; a disabled user is denied), and
   returns the stored result if the `operation_id` already has a receipt.
4. Otherwise it validates against the database (below), locks the vehicle/item scope, selects
   eligible obligations at the receipt instant, and writes the receipt, `check_record`, its links
   and the success audit event atomically; the handler returns `201`.

A rejected call rolls back whole: no check, no receipt, and the `operation_id` stays usable. A
transport failure is ambiguous (REC-4): the client retries with the *same* `operation_id`, and
the server returns the originally committed result, with its original correlation id, without a
second row. Replays are re-authorized, so they do not bypass a revoked user. The same id
with a different request applies nothing and answers `409 OPERATION_REUSED`; another user's id
answers `409 OPERATION_OWNED_BY_ANOTHER_ACTOR`.

## Validation

All of it runs server-side; the UI is never the enforcement point.

- The vehicle exists in the organization and is active.
- The checklist version has been published (a draft is `404`, not revealed).
- The version is the one that **currently governs** the vehicle, resolved as template version ⊕
  vehicle override at the server's instant. A superseded version answers `409
  CHECKLIST_VERSION_NOT_CURRENT`; the server never substitutes a newer version for the crew
  member's screen (contracts: "inactive or absent items are not silently substituted").
- The item is in that vehicle's resolved checklist (`422 ITEM_NOT_IN_CHECKLIST` otherwise), and
  the stored `item_def_id` is the vehicle's own definition (an override's when one applies).
- The outcome is one the definition allows. `NOT_APPLICABLE` is never accepted: SPEC §7.1 needs
  an administrator-defined NA rule and none exists yet.
- Observations match the item kind: presence/condition and special-observation items take
  neither field; a quantity item takes a whole-number `quantity` (0–1,000,000); a measurement
  item takes `{"value": "<decimal string>", "unit": "<text>"}` whose unit equals the
  definition's, with at most 10 decimals and a finite value (never rounded). Unable to Inspect
  carries no observation.
- Quantity outcomes are derived from the count (SPEC §4.4, SEAL-6). For a quantity item
  `outcome` may be omitted: a count equal to the exact target, including an active stocking
  deviation's alternate quantity, is Passed, and any other count (shortage or surplus) is
  Failed. The result echoes the stored outcome. The crew member may still state Failed for a
  matching count (a condition problem), but a mismatch can never be stated as Passed
  (`422 INVALID_OBSERVATION`): a stated outcome can only make the derived one worse. Without a
  count there is nothing to derive from, so `outcome` is required for every other item kind and
  for a quantity item without a count. The audit snapshot records whether the outcome was
  `DERIVED` or `STATED`. A derived or stated Failed carries its deficiency like any other Failed
  check ([issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80), [Deficiencies](deficiencies.md)).
- Unable to Inspect needs an `explanation`; any explanation is non-blank text of at most 4,000
  characters. `client_observed_at` is optional, timezone-aware and informational only.
- Kits: a kit is a template; a vehicle keeps zero, one or several instances of it, and each
  instance is checked on its own. A check on an item that lives in a kit therefore always names
  the instance it covers with `kit_id` (an extension of the contract's field list), even when
  only one instance exists. Omitting it is `422 KIT_REQUIRED`; a kit that is not one of the
  vehicle's own for that item, a vehicle with no such kit, or a kit given for an item outside a
  kit is `422 KIT_NOT_ON_VEHICLE`. Checks on different instances of the same item are separate
  records and each is kept.
- The audit event records ids and the outcome but **no free text**: the explanation is not copied
  into the audit snapshot.
- A check links to the earliest-due eligible obligation, with obligation ID breaking due-time
  ties. An on-time eligible obligation takes precedence over late linking. Late linking is
  considered only when no unsatisfied on-time obligation exists for the same vehicle, item and kit
  instance. Superseded or retired obligations are excluded. Repeated checks are retained; the
  earliest valid qualifying linked check satisfies the obligation. Kit checks never link across
  kit instances.
- As-of obligation projections read immutable links from the database by default and ignore links
  recorded after the requested instant. Pure-function callers may still provide an explicit check
  adapter. Shared satisfaction rules are audited administrator records; each explicit member pair
  must have the same vehicle, item and kit scope. There is no administration UI yet.

Correction requests require `operation_id`, `expected_tip_check_id`, and `outcome`; an optional
typed observation and explanation can replace the stored observation. The authenticated user is
the author; client-supplied attribution fields are refused. Replays return the stored result;
changed intent with the same operation id returns `409 OPERATION_REUSED`. A different author gets
`403 AUTHOR_REQUIRED`, a stale tip gets `409 CHAIN_CHANGED`, and an invalidated chain gets `409
CHAIN_INVALIDATED`. Corrections preserve the original check's compliance instant. As-of
projections select the latest correction recorded by the requested instant and mark prior chain
rows superseded. Recomputed obligation links use the root check's recorded instant.

## Error shape

`{"status": "REJECTED", "code": …, "correlation_id": …, "field_errors": [{"path": …, "code": …}]}`.
Paths name schema fields; they never echo user content (an unknown field is `$`/`UNKNOWN_FIELD`).

| HTTP | Code | When |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | bad JSON, content type, id, purpose, explanation or instant; unknown or missing field |
| 401 | `AUTHENTICATION_REQUIRED` | no valid active session |
| 403 | `FORBIDDEN` / `AUTHOR_REQUIRED` / `CSRF_REJECTED` | denied by policy / correction author mismatch / missing or bad CSRF token |
| 404 | `NOT_FOUND` | vehicle, published version or same-organization check absent |
| 405 | | any method but POST |
| 409 | `CHAIN_CHANGED`, `CHAIN_INVALIDATED`, `CHECK_CONFLICT`, `CHECKLIST_VERSION_NOT_CURRENT`, `CHECKLIST_UNAVAILABLE`, `CORRECTION_EXPIRED`, `OPERATION_REUSED`, `OPERATION_OWNED_BY_ANOTHER_ACTOR` | see above; see [Conflict warning](#conflict-warning-and-concurrent-checks); `CORRECTION_EXPIRED` is reserved for a future OQ-32 time-limit decision |
| 413 | `REQUEST_TOO_LARGE` | declared or streamed body over 256 KiB |
| 422 | `INVALID_LINK`, `INVALID_OUTCOME`, `INVALID_OBSERVATION`, `EXPLANATION_REQUIRED`, `ITEM_NOT_IN_CHECKLIST`, `KIT_REQUIRED`, `KIT_NOT_ON_VEHICLE`, `VEHICLE_INACTIVE`, `NOT_YET_SUPPORTED`, `DEFICIENCY_REQUIRED`, `DEFICIENCY_NOT_ALLOWED`, `DEFICIENCY_NOT_ACTIVE`, `DEFICIENCY_VEHICLE_MISMATCH`, `INVALID_SEVERITY`, `UNKNOWN_CATEGORY` | rule failures |
| 503 | `RETRY_LATER`, `AUDIT_UNAVAILABLE`, `CONFIGURATION_UNAVAILABLE` | no success acknowledgement; retry the same `operation_id` |

`CHECKLIST_VERSION_NOT_CURRENT`, `CHECKLIST_UNAVAILABLE`, `OPERATION_REUSED`, `VEHICLE_INACTIVE`,
`INVALID_OBSERVATION`, `ITEM_NOT_IN_CHECKLIST`, `KIT_REQUIRED`, `KIT_NOT_ON_VEHICLE`, `NOT_YET_SUPPORTED` and the deficiency codes (see [Deficiencies](deficiencies.md)) are additions to the
contract's list; `CSRF_REJECTED` for `/api/` paths is produced by `cya.checks.api.csrf_failure`
(HTML pages keep Django's default page). The CSRF refusal is not audited, because no actor is
established yet.

## Conflict warning and concurrent checks

[issue #75](https://github.com/JustApu/CheckYourAmbulance-archive/issues/75) (SPEC §7.4, CON-1–CON-5; AC-9, AC-10, AC-31). Crew members check different items
without any item locking (CON-1). Commits for one vehicle and item are serialized by the
`item_lock` row `record_check` holds, and the check's instant is read only after the lock is
held, so the comparison below and the insert are atomic (CON-5).

- `basis_check_id` is the newest check the client had displayed for the item;
  `acknowledges_check_id` is the newer check the crew member saw in a warning and chose to
  proceed past. Both are stored on the check and must be earlier checks of the same item (and
  kit instance), otherwise `404 NOT_FOUND` (not in this organization) or `422 INVALID_LINK`; the
  insert trigger repeats this.
- **Scope.** The newest check linked to the obligation the new check falls in (its on-time
  window, satisfied or not, so a repeat of a check someone just recorded is caught; otherwise the
  late-completion candidate); with no obligation, the newest unlinked check of the item. For a kit
  item the scope is the kit instance, so another instance's check is never a conflict.
- **Rule.** No warning when nothing newer exists, when the newest check is the caller's own, or
  when `basis_check_id` or `acknowledges_check_id` is that check. Otherwise the attempt is
  refused with `409 CHECK_CONFLICT`: `{status: "CONFLICT", code, operation_id, correlation_id,
  newest_check: {check_id, actor: {user_id, display_name}, server_recorded_at, outcome},
  obligation_id}`. The fields are those any signed-in crew member can already see.
- **A warning is not a commit.** It rolls back whole: no check and no receipt, so the
  `operation_id` stays free and an identical retry is evaluated again (REC-4). Cancelling creates
  nothing. Proceeding is a new decision: a **new** `operation_id` with `acknowledges_check_id`
  set to the check in the warning. If a further check landed in between, the server answers a
  fresh warning for it and commits nothing (AC-31). When it commits, both records are kept
  (CON-2). A replay of an already committed operation returns its stored result and never warns.
- **Every outcome.** The comparison does not depend on the outcome, so a Fail cannot be dropped
  because a Pass raced it (CON-4). A warned Failed check commits nothing, not even a deficiency;
  once it proceeds, both checks and exactly one deficiency are kept ([Deficiencies](deficiencies.md)).
- **Failure handling.** A deadlock is retried at most twice (existing `run_command` policy); a
  lock-wait timeout surfaces as `503 RETRY_LATER` with nothing written; an ambiguous write is
  never retried blindly.
- "Effective" means every check except those of an invalidated chain ([issue #77](https://github.com/JustApu/CheckYourAmbulance-archive/issues/77)); a superseded
  row is always older than its chain's tip, so it cannot be the newest on its own. The one query
  is `cya.checks.conflicts.newest_effective_check`.

## Administrator invalidation

`POST /api/v1/checks/{check_id}/invalidations` with `operation_id` and `explanation`
(`cya/checks/commands.py`, `invalidate_chain`; guard in migration `0007_chain_invalidation`).

- **Who.** Only administrators (the `invalidate_others_check` matrix row). Anyone else is denied
  and the denial is audited: `403 ADMINISTRATOR_REQUIRED` over HTTP. An administrator naming
  their own chain gets `403 SELF_INVALIDATION_FORBIDDEN` and nothing is written; they correct it
  instead. Both refusals are audited as the actor's denied action (outcome `DENIED`, with the role
  snapshot), not as anonymous failures. That is the INV-5 default for the open OQ-33, isolated in
  `_self_invalidation_forbidden` so the owner's later decision is a one-place change.
- **What.** Any row of a chain resolves to its root. An `INVALIDATION` `check_event` selecting the
  chain's current tip is appended, with a mandatory non-blank explanation (`422
  EXPLANATION_REQUIRED`, an over-long one is `400`) and the audit event in the same transaction.
  No existing row is updated or deleted (AC-15). The explanation is not copied into the audit
  snapshot.
- **Effect.** The whole chain is excluded and no earlier outcome revives (INV-2a): the as-of
  projection skips the chain from the invalidation instant, so its obligations return to
  unsatisfied (INV-2), while an as-of query before that instant still shows the chain as effective
  (REG-6). A later correction is refused (`409 CHAIN_INVALIDATED`); the author records a fresh
  check. A fresh check can satisfy the obligation again, and the conflict warning no longer
  counts the invalidated check as the newest one. A second invalidation with a new operation id is
  `409 CHAIN_INVALIDATED`; a replay of the successful operation returns its stored result.
  Invalidation is not undoable (INV-6).
- **Response.** `root_check_id`, `invalidation_event_id`, `server_recorded_at`,
  `affected_obligation_ids` (the obligations the chain's tip was linked to), and
  `reopened_deficiency_ids` (deficiencies whose Reported-in-Error status this chain reopens in
  the same transaction; otherwise empty) and `voided_determination_ids` (current supervisor
  completions voided by this chain event; otherwise empty). Notification delivery is M4.
- **Serialization.** The command holds the same vehicle/item `ItemLock` as recording and
  correcting, so a correction and an invalidation of one chain cannot interleave: either the
  correction commits first and the invalidation selects the new tip, or the correction is refused.
- **Database guard.** The `check_event` insert trigger accepts an `INVALIDATION` only when the
  selected row is the chain's tip, the explanation is present, the stored role snapshot includes
  the administrator role, the receipt is a `CHECK_INVALIDATION` for the same actor and instant, and
  the chain was not invalidated before; it also refuses a correction of an invalidated chain.
- **A retained repeat check does not take over.** A repeat check by another crew member made
  after the obligation was satisfied was never linked to it, so it does not satisfy the
  obligation when the first chain is invalidated; a fresh check is needed (INV-2a).

## Verification checks

A check with `purpose` `VERIFICATION` and `verification_for_deficiency_id` (SPEC VER-4 to VER-6,
[issue #154](https://github.com/JustApu/CheckYourAmbulance-archive/issues/154)) is an ordinary check: it is attributed, idempotent on `operation_id`, subject to the
conflict warning and linked to obligations exactly like a routine check, and the record-check
protocol is unchanged for routine checks. The two fields travel together: a verification without
its target is `422 VERIFICATION_TARGET_REQUIRED` and a routine check with a target is
`422 VERIFICATION_TARGET_NOT_ALLOWED` (both name `verification_for_deficiency_id`). The column
`check_record.verification_for_deficiency_id` is set exactly when `purpose` is `VERIFICATION`
(CHECK `check_verification_pairing`, organization-pair foreign key to `deficiency`); a correction
row repeats its chain's value. `verification_scope_revision` records the scope revision the check was
recorded against and is set and cleared with the target; the insert trigger requires the
deficiency's newest revision. A Pass counts for rows of that revision and, after a supervisor
expansion repeats them in a later revision of the same correction, for those repeated rows too
(SPEC VER-4 as amended by 1.22.0, [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)). It never carries over to the revision of a new
correction, even if a row keeps its instant.

The command refuses, before any write and leaving the operation id reusable:

| Condition | Answer |
|---|---|
| deficiency unknown or of another organization | `404 NOT_FOUND` |
| deficiency on another vehicle than the check | `422 DEFICIENCY_VEHICLE_MISMATCH` |
| deficiency not Pending Verification when the deficiency lock is held (Open, In Progress, closed, or already closed by a concurrent last item) | `409 VERIFICATION_NOT_PENDING` |
| item (and kit instance) not a row of the deficiency's newest scope revision | `422 NOT_IN_VERIFICATION_SCOPE` |
| outcome other than Passed or Failed (Unable to Inspect) | `422 INVALID_OUTCOME` |
| Failed with a `deficiency` selection other than the target, or a `create` | `422 DEFICIENCY_SELECTION_CONFLICT` |
| Passed with a `deficiency` selection | `422 DEFICIENCY_NOT_ALLOWED` |

A Failed verification may omit `deficiency` (the target is used) or repeat `{existing_id}` for the
target; its evidence always attaches to the verified deficiency. The scope match uses the item and
the physical kit instance, not the checklist definition, so a republished definition of the same
item is still the item that must be checked.

Effects, all in the check's own transaction after the item lock and under the deficiency lock
(lock order: vehicle when a session is used, item, deficiency):

- A **Pass counts** toward a scope row only when it was recorded against the row's scope revision,
  is recorded strictly after the row's
  `required_after` (the correction instant), is effective (not replaced, chain not invalidated)
  and matches the row's item and kit instance. The recorded instant is taken after the deficiency
  lock, so a check that waited behind a correction is never dated before it. A Pass that does not
  count is still recorded and the response says `counts_toward_scope: false`.
- When the Pass counts and no scope row of the newest revision remains unsatisfied, the same
  transaction appends `VERIFICATION_CLOSED` (Pending Verification to Closed Verified) naming the
  check in `verification_check_id`, and moves the projection. Exception (INV-3): when an administrator has
  invalidated a check chain that supports the deficiency, nothing is closed; the Pass still counts,
  the response says `closure_withheld: true` and the deficiency awaits an explicit closure (§8.8). Concurrent last passes serialize on
  the deficiency lock, so exactly one of them closes it; the other sees the committed Pass.
- A **Fail** adds the check as deficiency evidence and appends `VERIFICATION_FAILED` (Pending
  Verification to In Progress, VER-5). The correction and its scope rows stay in history; a later
  correction creates a new event and a new scope revision, which no earlier Pass satisfies.

The response gains a `verification` object (`deficiency_id`, `scope_revision`,
`counts_toward_scope`, `remaining_scope_rows`, `closure_withheld`, `status`) and the lifecycle event id in `event_ids`;
a routine response is unchanged. The audit snapshot adds `verification_for_deficiency_id`,
`verification_counts` and `verification_event_id`. The insert triggers of `check_record` and
`deficiency_event` repeat the pairing, pending, scope, outcome and closure-completeness rules, so a
writer that bypasses the command cannot close a deficiency with an unsatisfied row.

[issue #155](https://github.com/JustApu/CheckYourAmbulance-archive/issues/155) adds what invalidating or correcting a verification check does
([Deficiencies](deficiencies.md#invalidating-or-correcting-a-verification-check)). The author may
correct a verification Pass to `FAILED` or `UNABLE_TO_INSPECT` (with a reason; the Unable row keeps
no quantity or measurement, a Failed row keeps the Pass's observation unless a new one is given).
The corrected row repeats the chain's deficiency and scope revision, is not added as deficiency
evidence and never closes anything. Every other verification correction (a Failed verification
check, or a Pass corrected to Passed) stays refused with `422 NOT_YET_SUPPORTED`. A scope row for an item of kind `VEHICLE_READING` cannot be
satisfied by a check (readings are odometer entries), so such a deficiency stays Pending
Verification until an administrator closes it. A verified closure ends every check suspension
still in force ([issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13), [deficiencies](deficiencies.md#supervisor-check-suspension)); outbox and notification
effects arrive with [issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14).

[Issue #15](https://github.com/JustApu/CheckYourAmbulance/issues/15) exposes the stored
`verification` result through the HTTP check acknowledgement as `result.verification` when
present: `deficiency_id`, `scope_revision`, `counts_toward_scope`, `remaining_scope_rows`,
`closure_withheld` and `status`. A replay returns that original committed result even if the
deficiency has since changed. Routine acknowledgements keep their existing shape. The
[crew verification screen](deficiency-workflows.md#crew-deficiency-actions-issue-15) uses these
fields to distinguish a saved check from a verified or explicitly withheld closure.

## Not built yet, and refused

Each of these is a valid part of the contract, so the command answers `422 NOT_YET_SUPPORTED`
naming the field instead of silently ignoring it.

| Input | Enabled by |
|---|---|
| correction outcomes other than Passed or Unable-to-Failed; on a verification chain, anything but a Pass corrected to Failed or Unable | later correction/deficiency integration |
| items of kind `VEHICLE_READING` | odometer entries ([issue #79](https://github.com/JustApu/CheckYourAmbulance-archive/issues/79)) |
| `NOT_APPLICABLE` | an administrator-defined NA rule (no issue yet) |

## Where the schema completes or differs from the contract

- Columns absent for now, each added by the issue that needs it:
  `roster_snapshot_id`, `asset_id` (R3, no table yet) and
  the reserved `bulk_group_id` (Bulk Pass is deferred from R1, SPEC §2.1a, so it is simply absent).
- `root_check_id` references the first row in a chain; correction rows carry a
  `predecessor_check_id` and retain that root. The measurement is `measurement_value` (decimal
  30,10) plus `measurement_unit`.
- `session_id` ([issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) Part B) is the inspection session the check was made in, null for a
  check outside one. The caller must be a recorded participant of an **Open** session on this
  vehicle and this pinned checklist version; an unknown session and one the caller never
  joined both answer `NOT_FOUND`, a session for another vehicle or version `SESSION_MISMATCH`,
  and an ended or six-hour-idle session `SESSION_NOT_OPEN` (409: reopen it first). The check is
  persisted activity for the session's timer ([inspection sessions](inspection-sessions.md))
  and never compliance evidence. The command holds the vehicle row lock while it reads the
  session, so a check and the auto-close cannot interleave; the insert trigger repeats the
  participant, vehicle, version, open and not-yet-idle rules. A check in a session is resolved
  against the checklist the session opened with, including the vehicle override known at that
  time (SPEC CHK-4, §5.7), so a newer template version or override does not block it or change
  the item it records, even after a reopen; only an active stocking deviation is read at the
  recording instant. A check outside a session is resolved against the current checklist.
- `kit_id` is the vehicle's own kit instance (never the template reference, SPEC §4.2), always named by the client for a kit item, and null for any other item.
- The insert trigger repeats the command's checks for a writer that bypasses it: the row must
  match its command receipt (organization, actor, recorded instant, operation id and the `CHECK_RECORD` command type), name a version published by then, and carry a definition of that
  version (or of an override of it, published by then, for this vehicle) for this item, a kit item must name a kit, and that kit must be the vehicle's own in the slot the definition names. The
  organization-pair foreign keys, UUID and role checks and the append-only triggers follow
  `vehicle_status_event`. The runtime account holds `SELECT, INSERT` only.

## Obligations per kit instance

This issue records *which kit instance* each check covers. The owner decided that an obligation
for a kit item is one per kit instance (decision OQ-53, SPEC 1.8.0), so that checking one of
several identical kits leaves the others unsatisfied. Obligations now carry the kit instance
([issue #92](https://github.com/JustApu/CheckYourAmbulance-archive/issues/92), [obligations per kit instance](kit-obligations.md)); [issue #74](https://github.com/JustApu/CheckYourAmbulance-archive/issues/74) links a check only to
the obligation whose kit instance equals the check's `kit_id`. The mid-window rules and the
assignment record are settled in SPEC 1.10.0 (OQ-54) and implemented by [issue #92](https://github.com/JustApu/CheckYourAmbulance-archive/issues/92). Expiration
dates are not stored on a check at all yet (they belong to the sealed-kit workflow, SPEC §10.1).

## Assumptions to confirm

These are small, reversible choices made without a SPEC rule; none changes stored history.

- Resolving the checklist on every call is simple and correct; a per-version cache is a later
  optimization if profiling shows a need.

Decided with the owner: a superseded checklist version is a `409`, not an accepted
older-version check; and a quantity item's Pass or Fail is derived from the count, with the crew
able only to make it worse. A measurement's Pass or Fail is whatever the crew member states,
because definitions carry no thresholds.

## Reversing reference migrations on cloned test databases

[issue #145](https://github.com/JustApu/CheckYourAmbulance-archive/issues/145) fixes reversal of the check's basis, acknowledgement and correction-predecessor
references. Django clones MariaDB test databases through a dump/load. The dump writes explicit
index definitions; after a referenced column is dropped, MariaDB can retain an organization-only
index under the old composite index name. Reapplying the reference then fails with error 1061.
This follows MariaDB's documented [DROP COLUMN handling of multi-column indexes](https://mariadb.com/docs/server/reference/sql-statements/data-definition/alter/alter-table#drop-column).

The reverse SQL in check migrations 0005 and 0006 now drops each reference's supporting index
after dropping its foreign key, before Django removes the column. Forward migrations, data,
model state and runtime commands are unchanged. No new forward migration is needed on an
already-current database. Foreign-key and CHECK enforcement stay enabled; restoration recreates
the original composite keys.

The minimal synthetic regression in `cya/checks/tests/test_migrations.py` compares a table built
by ALTER TABLE with the same table recreated from SHOW CREATE TABLE. Both reverse/reapply twice;
valid references succeed and cross-organization references fail. The original kit migration
refusal test also compares every check-record key, index and append-only trigger before and
after restoring the migration leaves.

Starting with no existing worker clones and the documented disposable development database settings:

```sh
local/venv/bin/python manage.py test cya.checks.tests.test_migrations cya.obligations.tests.test_kits.MigrationTests --noinput
local/venv/bin/python manage.py test cya.checks.tests.test_migrations cya.obligations.tests.test_kits.MigrationTests --parallel 4 --keepdb --noinput
local/venv/bin/python manage.py test cya.checks.tests.test_migrations cya.obligations.tests.test_kits.MigrationTests --parallel 4 --keepdb --noinput
```

Django limits workers to the number of test classes, so this focused selection uses two workers.
Fresh and kept clones are supported. If an earlier failed run left a test schema partially
unapplied, rebuild the affected disposable test databases once by running the relevant suite
without `--keepdb` (use the full suite and the original worker count to rebuild all its clones).
This fix does not repair a previously interrupted schema in place. Migration reversal remains
schema-owner work, and the existing data-preservation refusal guards still apply.

## Verification

Offline tests in `cya/checks/tests/`: `test_commands.py` (validation matrix, authorization,
replay, a six-way same-operation race, overrides, kits), `test_guards.py` (database triggers and
constraints), `test_api.py` (HTTP codes, CSRF, body bounds, sign-in through the offline Google
stub) and `test_runtime.py` (the real least-privilege account; needs `CYA_DB_SECURITY_TESTS=1`).
There is no `--dry-run`: the command is a client-initiated write, idempotent on `operation_id`.
