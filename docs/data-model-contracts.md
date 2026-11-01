# R1 data model and command contracts

[issue #20](https://github.com/JustApu/CheckYourAmbulance-archive/issues/20). Design baseline: approved [SPEC 1.3.0](../SPEC.md), particularly
§3.3, §7.2, §17.7–§19 and §23. The owner approved the [issue #31](https://github.com/JustApu/CheckYourAmbulance-archive/issues/31)
clarification in SPEC 1.3.0 on 2026-10-20 (approval record: SPEC §25.6).
This document specifies interfaces to implement in subsequent issues; it is
neither application code nor a migration. SPEC wins if the documents disagree.

## 1. Physical conventions

Use MariaDB 10.11 InnoDB tables. Every organization-owned table has a non-null
`organization_id`. IDs are server-generated UUIDs stored as `CHAR(36)` with
ASCII binary collation; wire IDs use canonical lowercase UUID strings. User
identity is unique on `(organization_id, idp_issuer, idp_subject)`, never email.
Foreign keys include organization: each referenced table has a unique
`(organization_id, id)` key and children reference that pair. This prevents
accidental cross-organization joins without claiming multi-tenant support.
Use `ON DELETE RESTRICT ON UPDATE RESTRICT`; never cascade historical deletion.

Notation below: `id` is the primary key; `?` means nullable; `ref` is a foreign
key, not unvalidated text. All other columns are non-null. `A` tables are
append-only; `V` tables have immutable published revisions; `M` tables are
mutable headers or rebuildable projections. Names below are proposed physical
names, not existing tables. Django model names may differ; table names must be
mapped explicitly. No generic unvalidated entity-type/id pair substitutes for
a foreign key in business evidence.

Store instants in `DATETIME(6)` UTC; dates (content expiration) in `DATE`.
Server-assigned `recorded_at` and any separate effective instant are distinct.
Use a per-aggregate sequence for deterministic ordering when timestamps tie;
never order a correction chain by UUID. Histories order by `(recorded_at,
aggregate_sequence)`; child rows in one command have a deterministic ordinal.
An as-of instant includes the complete atomic command, never part of it.
Client observation times do not determine compliance. Decimal measurements use
`DECIMAL(18,6)` with a configured unit; quantities use nonnegative integers.
No floating point comparison determines exact stocking compliance.

Closed values use bounded `VARCHAR` plus `CHECK` membership constraints, not
free-form executable expressions. Booleans have `CHECK (value IN (0,1))`.
Nonempty reasons use both server validation and a trimmed-length DB check.
Text defaults: labels 200 characters, explanations 4000, notes 10000; explicit
validation rejects overflow rather than truncating it. JSON holds bounded,
schema-versioned snapshots only; relational keys, outcomes and event types
remain typed columns. Payload limits are implementation defaults, not product
decisions. Reject unknown fields and unsupported schema versions.

## 2. Table catalogue

Common historical columns: `id`, `organization_id`, `command_id` ref,
`recorded_at`, `actor_user_id` ref (nullable only for named system jobs), and
`actor_roles_at_time` JSON array. Jobs additionally identify a bounded action
and service identity. Event tables add `sequence BIGINT UNSIGNED`, unique per
aggregate. An event's command identifies its correlation ID and operation ID.
Every mutable change has an audit entry in the same transaction.

### Identity and fleet

| Table | Kind | Columns and keys beyond common identity |
|---|---|---|
| `organization` | M | name, station IANA zone, active; audit all configuration revisions |
| `user` | M | idp_issuer, idp_subject, email, display_name, active; identity tuple unique; no hard delete |
| `role_grant` | A | user_id ref, role (canonical: `CREW`, `SUPERVISOR`, `SUPPLY_OFFICER`, `ADMINISTRATOR`; legacy `EMT` accepted for historical evidence and read as Crew, not for new grants), action (`GRANT`, `REVOKE`), prior_grant_id? ref; revocation is a new row, never a filled-in revoked_at |
| `user_preference` | M | user_id ref unique, preferred_view, needs_attention_only; per crew member across devices |
| `vehicle` | M | display_name, number?, active; operational status is not stored here as authority |
| `compartment`, `vehicle_system` | V | stable_slot_id, vehicle_id ref, revision, label, effective_at; unique slot/revision |
| `kit_instance` | M | label, active, vehicle_id? ref, compartment_id? ref; identity retained on checks; R1 has assignment, no movement workflow |
| `kit_assignment_event` | A | kit_id ref, vehicle_id? ref, compartment_id? ref, active, effective_at, recorded_at, actor, command_id ref (SPEC amendment 1.10.0, OQ-54; implemented by [issue #92](https://github.com/JustApu/CheckYourAmbulance-archive/issues/92)); effective_at is never before recorded_at; `kit_instance`'s vehicle, compartment and active become a rebuildable projection of the latest event; `kit_movement` (R2) extends it |
| `vehicle_status_event` | A | vehicle_id ref, prior_event_id? ref, status, explanation, restrictions?; sequence unique per vehicle |
| `status_request` | A | vehicle_id ref, requested_status, reason, request_id? ref, action (`REQUEST`, `APPROVE`, `DENY`), decision_explanation? |

Role changes lock the affected user before reading grants; concurrent attempts
cannot double-grant or revoke an already-revoked grant. A revocation references
exactly one earlier grant for the same user and role; unique prior_grant_id
prevents duplicate revocations. Initial provisioning appends only CREW. Last-admin
checks serialize on the organization row before locking affected user rows in
ID order. Disabling the last active administrator is subject to the same guard.
No deletion frees an identity for a different person (AUTH-2).

### Checklists and requirements

[issue #55](https://github.com/JustApu/CheckYourAmbulance-archive/issues/55) implemented these tables; [where the physical schema completes or differs from the rows below](checklists-implementation.md#where-the-schema-completes-or-differs-from-the-contract-text) is recorded there.

| Table | Kind | Columns and keys beyond common identity |
|---|---|---|
| `checklist_template` | M | label, active; version contents stored separately |
| `checklist_version` | V | template_id ref, revision; unique template/revision; publication is identified by its referencing publication row |
| `checklist_publication` | A | checklist_version_id ref unique, effective_at, override_version_id? ref; published versions and children frozen |
| `item` | M | stable logical item/slot identity, label, active; used across versions |
| `checklist_item_def` | V | checklist_version_id ref, item_id ref, compartment_id? ref, system_id? ref, kit_id? ref, asset_id? ref, display_order, instructions, kind, expected_quantity?, unit?, allowed_outcomes, na_rule_id? ref; unique version/item; bulk_eligible reserved false |
| `vehicle_override_version` | V | vehicle_id ref, template_version_id ref, revision, publication ref?; overlay entries in `vehicle_override_item` name item, ADD/REMOVE/REPLACE and replacement definition ref |
| `schedule_rule` | V | revision, zone, closed recurrence type and typed parameters, early_seconds, grace_seconds, effective_at; nonnegative windows |
| `conditional_rule` | V | revision, type (`CARRY_OVER_FROM_PRIOR_SHIFT`, `SEAL_SUBSTITUTION`), typed inputs, effective_at |
| `na_rule` | V | revision, administrator-defined applicability parameters, effective_at; closed validated conditions |
| `shared_satisfaction_rule` | V | revision, approver, effective_at; membership rows explicitly name source and additional requirement versions |
| `shared_satisfaction_rule_member` | A | rule_id ref, source_requirement_version_id ref, additional_requirement_version_id ref; same vehicle/item/kit scope; matches the rule's command attribution |
| `requirement_version` | A | vehicle_id ref, item_id ref, item_def_id ref, publication_id ref, override_version_id? ref, schedule_rule_id? ref, conditional_rule_id? ref, na_rule_id? ref; fully resolved version; binding/schedule are null only for an explicit ad hoc command (ADH-1) |
| `stocking_deviation` | V | vehicle_id ref, item_id ref, revision, normal_quantity, alternate_quantity, valid_from, valid_until, approver ref, explanation, prior_revision_id? ref; start < end; each extension/termination appends |

Publishing validates the complete resolved item set, freezes the version and
its definitions, and creates missing item lock rows **before** any check can
use it. Old lock rows remain for reopened sessions. Published data cannot be
edited through either Django admin or generic ORM updates. Migrations implement
freeze enforcement on the parent and its children with triggers in addition to
command validation; publication and lock preparation are one atomic command.
Draft editing is allowed only before publication and is audited.

`requirement_version` pins resolved template, overlay, schedule and rule inputs.
Obligations bind the version effective at their due instant, not the session's
version. No silent migration of old obligations on publish. The SPEC §5.7
"apply to open obligations" option is not offered (owner-decided 2026-10-23,
OQ-52); this design does not enable it.

Generation retains TIME-1's default 48-hour horizon. Pre-created obligations
can therefore need replacement **before** their early window opens. Under the
vehicle/item anchors, append the old obligation's `SUPERSEDED` determination,
the replacement obligation and an `obligation_selection` event in one atomic
command. Generation and publication use the same reconciliation: resolve the
requirement effective at D and select one obligation per
`(organization_id, vehicle_id, item_id, kit_key, due_at)`. A routine obligation is
effective only when named by the latest selection; archived rows never become additional
missed inspections. As-of queries use only selections recorded by T. ADH-1 orders
are separate non-routine obligations effective from their recorded request, with
no selection row, even when the same item/kit/due anchor has a routine obligation.

`obligation_selection` has unique keys on
`(organization_id, vehicle_id, item_id, kit_key, due_at, sequence)` and on non-null
previous_selection_id. A BEFORE INSERT trigger verifies the selected row has
the same vehicle/item/kit/D, the first selection has sequence 1 and no predecessor,
and replacements reference the current tail, increment sequence by 1, and
reference the old obligation's SUPERSEDED determination from the same command
when the predecessor selected an obligation. If it was a RETIRE event, a new
selection still follows that tail but has no supersession determination.
Handler locking plus these keys/trigger checks prevent branching: concurrent
losers roll back the whole command and rerun reconciliation. The latest event
selects at most one effective obligation even though immutable history retains
multiple requirement versions. No mutable effective flag is authoritative.

Publication reconciles affected pre-created **pre-window** obligations in its
own transaction. Future-effective versions affect only D at or after their
effective instant. Generation re-reads publications after taking the same
locks, preventing an old generator from selecting an obsolete version after
publish. Already-created in-window or past obligations retain their selection
because the apply-to-open option is not offered (SPEC §5.7, OQ-52). A removed future
occurrence gets a RETIRE event (null selected obligation, required supersession
determination), not an invented replacement. Initial selections cannot retire;
the trigger checks nullability by event type. Reconciliation of an already
selected publication/slot is a no-op. This preserves §5.7 and TIME-1 together.

### Obligations, sessions and readings

[issue #59](https://github.com/JustApu/CheckYourAmbulance-archive/issues/59) implemented `requirement_version`, `obligation`, `obligation_selection` and `odometer_obligation`; [where the physical schema completes or differs from the rows below](obligation-generation.md#where-the-schema-completes-or-differs-from-the-contract-text) is recorded there, including the `requirement_binding` table that the contract does not name.

[issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) Part A implements the session header, participant and event tables
and a rebuildable projection; [the implemented command and storage boundary](inspection-sessions.md)
documents the Part A scope. Activity-based auto-close and session-linked checks
remain in Part B after [issue #73](https://github.com/JustApu/CheckYourAmbulance-archive/issues/73).

| Table | Kind | Columns and keys beyond common identity |
|---|---|---|
| `obligation` | A | vehicle_id ref, item_id ref, requirement_version_id ref, due_at, early_at, grace_at, routine flag, explanation? (required for non-routine ADH-1 orders); kit_instance_id? ref (SPEC 1.8.0, OQ-53: set for an item inside a kit, null otherwise; implemented by [issue #92](https://github.com/JustApu/CheckYourAmbulance-archive/issues/92)); unique `(vehicle_id,item_id,kit_instance_id,due_at,requirement_version_id)` with a missing kit instance compared as a value, because MariaDB `UNIQUE` treats NULLs as distinct; early_at ≤ due_at ≤ grace_at |
| `obligation_selection` | A | vehicle_id ref, item_id ref, due_at, selected_obligation_id? ref, previous_selection_id? ref, supersession_determination_id? ref, type (`SELECT`, `REPLACE`, `RETIRE`); sequence and keys as above; latest selection determines routine effectiveness; explicit non-routine orders have no selection stream |
| `obligation_determination` | A | obligation_id ref, type (waive/reinstate/supervisor-complete/invalidate/supersede), rule_id? ref, prior_determination_id? ref, explanation?, immutable evaluated-input snapshot |
| `determination_check` | A | organization_id, receipt/time and human actor attribution; determination_id and check_id refs; unique pair; UTI completion references its supporting Unable check(s) |
| `inspection_session` | M | vehicle_id ref, pinned checklist_version_id ref, opened_at; immutable header protected by trigger; cached state/activity only in separate `session_projection` |
| `session_participant` | A | session_id ref, user_id ref; unique pair |
| `session_event` | A | session_id ref, type, effective_at, persisted_activity_command_id? ref; auto-close effective_at = last activity + 6h |
| `roster_snapshot` | A | provider, fetched_at, availability, shift_start, shift_end, vehicle_id ref; normalized synthetic/stub inputs until integration is authorized |
| `roster_member` | A | snapshot_id ref, person identity ref, assignment; unique snapshot/person/assignment |
| `session_roster_correction` | A | session_id ref, snapshot_id? ref, covers_from, covers_until, resolves_correction_id? ref; member rows retain actual membership |
| `odometer_entry`, `engine_hours_entry` | A | vehicle_id ref, session_id? ref, value, unit, prior_entry_id? ref, root_entry_id ref, anomaly_confirmed, confirmation_explanation?; operation_id unique per org |
| `reading_event` | A | reading type and exactly one odometer/engine_hours FK, correction_entry FK? of same kind, type, explanation?; author/chain rules match checks |
| `odometer_obligation` | A | vehicle_id ref, scheduled inspection key, due/early/grace instants; unique vehicle/scheduled inspection key |
| `odometer_obligation_link` | A | obligation_id ref, entry_id ref; at most one *effective* link per obligation (an invalidated chain's link remains historical); same vehicle, eligible time; invalidation voids derived satisfaction |

State is always derived. Determination input references include all roster
corrections considered, the scheduled snapshot and fetched age, supporting
checks, rule version and decision reason. Disagreement between concurrent
session rosters fails closed until supervisor resolution; every reevaluation
appends. No roster is written back to Humanity. Vehicle-wide scope changes and
session auto-close use a precreated `vehicle_lock`; persisted session activity
and auto-close compare under the same lock so a delayed job cannot erase work.

### Checks and deficiencies

[issue #73](https://github.com/JustApu/CheckYourAmbulance-archive/issues/73) implemented `check_record` and the record-check command; [issue #76](https://github.com/JustApu/CheckYourAmbulance-archive/issues/76) adds its append-only author-correction chain and `check_event`. [Where the schema completes or differs from the contract](check-records.md#where-the-schema-completes-or-differs-from-the-contract) is recorded in the feature documentation.

[issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80) implemented `deficiency_category`, `deficiency_severity`, `deficiency`, `deficiency_lock`, `deficiency_projection`, `deficiency_affected_item`, `deficiency_event`, `deficiency_evidence` and `deficiency_link` and enabled Failed and Unable to Inspect checks; [where the schema completes or differs from the contract](deficiencies.md#where-the-schema-completes-or-differs-from-the-contract) is recorded in the feature documentation.

[issue #150](https://github.com/JustApu/CheckYourAmbulance-archive/issues/150) added the `EXTENSION_REQUESTED` and `EXTENSION_DECIDED` `deficiency_event` types with `decision` and `request_event_id`, and the append-only `deficiency_age_rule` table; see [Deadline extensions, overdue and age](deficiencies.md#deadline-extensions-overdue-and-age).
[issue #152](https://github.com/JustApu/CheckYourAmbulance-archive/issues/152) added the `ADMINISTRATOR_CLOSED` and `ADMINISTRATOR_REOPENED` `deficiency_event` types with a `suspensions_ended` JSON array.

[issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13) implemented `check_suspension` and the `CHECKS_SUSPENDED` `deficiency_event` type; the four closing event types (`VERIFICATION_CLOSED`, `ADMINISTRATOR_CLOSED`, `EMPTY_SCOPE_CLOSED`, `REPORTED_IN_ERROR`) list in `suspensions_ended` exactly the suspensions in force that they end, and every other event lists none; see [Supervisor check suspension](deficiencies.md#supervisor-check-suspension) and [where the schema completes or differs](deficiencies.md#where-the-schema-completes-or-differs-from-the-contract).

[issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153) adds the `EMPTY_SCOPE_CLOSED` event and `CLOSED_UNVERIFIABLE` projection status. The distinct command may be used by a Supervisor or Administrator only when the current correction-time verification scope resolves empty; it records an explanation and no correction event. The existing Administrator-only general closure remains unchanged. The Administrator reopen event may reopen either explicit-closure status.

[issue #81](https://github.com/JustApu/CheckYourAmbulance-archive/issues/81) added the `REPORTED_IN_ERROR`, `CORRECTION_NOTICE` and `REOPENED` `deficiency_event` types (each naming its check chain in `correction_ref`) and relaxed the evidence guard so one Failed check may support several deficiencies of its vehicle; see [Corrections and invalidations](deficiencies.md#corrections-and-invalidations).

[issue #123](https://github.com/JustApu/CheckYourAmbulance-archive/issues/123) added the `SEVERITY_CHANGED` `deficiency_event` type with `previous_severity_version_id` and `severity_version_id`, and `severity_version_id` on `deficiency_projection`; see [Re-grading severity](deficiencies.md#re-grading-severity).

[issue #154](https://github.com/JustApu/CheckYourAmbulance-archive/issues/154) added `check_record.verification_for_deficiency_id` and `verification_scope_revision` (both set exactly when `purpose` is `VERIFICATION`) and the `VERIFICATION_CLOSED` and `VERIFICATION_FAILED` `deficiency_event` types with `verification_check_id`; see [Verification and automatic closure](deficiencies.md#verification-and-automatic-closure) and [Verification checks](check-records.md#verification-checks).

[issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149) added the `DEPENDENCY_ENTERED`, `DEPENDENCY_REVISED`, `DEPENDENCY_ENDED` and `PROGRESS_NOTE` `deficiency_event` types with no new column (the note text is the event's `explanation`, the follow-up instant its `follow_up_at`, the status it returns to the entering event's `from_status`); see [Awaiting External Dependency and progress notes](deficiencies.md#awaiting-external-dependency-and-progress-notes).

[issue #83](https://github.com/JustApu/CheckYourAmbulance-archive/issues/83) implemented `user_preference` (one row per user: `preferred_view` NORMAL or PRIORITY, `needs_attention_only`, `updated_at`) with an audited `SAVE_VIEW_PREFERENCES` command; item attention and the checklist views are derived on demand and have no table. See [Item attention, checklist views and the view preference](attention-views.md).

[issue #79](https://github.com/JustApu/CheckYourAmbulance-archive/issues/79) implemented `odometer_entry`, `engine_hours_entry`, `odometer_obligation_link`, `reading_event` and a new `odometer_threshold` table; [where the schema completes or differs from the contract](odometer.md#where-the-schema-completes-or-differs-from-the-contract) is recorded there.

| Table | Kind | Columns and keys beyond common identity |
|---|---|---|
| `check_record` | A | operation_id, vehicle_id ref, item_id ref, item_def_id ref, checklist_version_id ref, session_id? ref, kit_id? ref, asset_id? ref, outcome, quantity?, measurement?, explanation?, client_observed_at?, roster_snapshot_id? ref, root_check_id ref, predecessor_check_id? ref, basis_check_id? ref, acknowledges_check_id? ref, purpose, verification_for_deficiency_id? ref, bulk_group_id? (must be null in R1) |
| `check_event` | A | root_check_id ref, selected_check_id ref, type (`CORRECTION`, `INVALIDATION`), replacement_check_id? ref, explanation?; correction replacement unique; invalidation unique per root |
| `check_obligation_link` | A | check_id ref, obligation_id ref, ordinal, shared_rule_id? ref; unique check/obligation and check/ordinal |
| `deficiency_category` | V | label, supply_related, revision, effective_at; category flag changes audited |
| `deficiency_severity` | V | code, revision, label, description (concise criteria), rank, active, effective_at; levels are administrator data (SPEC DEF-8) |
| `deficiency` | M | creation command/operation_id, reported_at, reporter ref, category_version_id ref, severity_version_id ref, summary, description, vehicle_id ref, item_id? ref, kit_id? ref, compartment_id? ref; immutable creation header protected by trigger; current state only in separate `deficiency_projection` |
| `deficiency_affected_item` | A | deficiency_id ref, item_id ref, definition_id ref; materialized affected scope, unique deficiency/item/definition |
| `deficiency_event` | A | deficiency_id ref, predecessor_event_id? ref, type, from_status?, to_status?, explanation?, assignee_id? ref, deadline?, proposed_deadline?, follow_up_at?, decision? (`APPROVED`, `DENIED`, `DIFFERENT_DATE`), request_event_id? ref (one decision per request), acknowledgement_until?, action_plan? (required with explanation for ESCALATION_ACKNOWLEDGED; [issue #151](https://github.com/JustApu/CheckYourAmbulance-archive/issues/151)), `suspensions_ended` (the `check_suspension` START ids a closing event ended, [issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13); empty on every other event), correction_ref?, previous_severity_version_id? ref, severity_version_id? ref (re-grade only), verification_check_id? ref (only `VERIFICATION_CLOSED` and `VERIFICATION_FAILED`, written by the verification check's own command, [issue #154](https://github.com/JustApu/CheckYourAmbulance-archive/issues/154); and `VERIFICATION_REOPENED`, naming the verification chain whose invalidation or non-Pass correction left the closing scope revision unsatisfied, [issue #155](https://github.com/JustApu/CheckYourAmbulance-archive/issues/155)); administrator close/reopen events require an explanation and have distinct event types ([issue #152](https://github.com/JustApu/CheckYourAmbulance-archive/issues/152)); `EMPTY_SCOPE_CLOSED` requires an explanation, its own receipt and Supervisor/Administrator actor ([issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153)); the assignment events `ASSIGNED` (assignee only), `DEADLINE_SET` (deadline only) and `WORK_STARTED` (neither) are written by their own command types ([issue #146](https://github.com/JustApu/CheckYourAmbulance-archive/issues/146)), and `DEPENDENCY_ENTERED`, `DEPENDENCY_REVISED` (explanation and optional follow_up_at), `DEPENDENCY_ENDED` (neither) and `PROGRESS_NOTE` (the note in explanation) likewise ([issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149)); unique deficiency/sequence and predecessor successor |
| `deficiency_age_rule` | A | revision, age_thresholds_days (up to five ascending), long_running_age_days?, long_running_extensions?, operation_id; administrator-set age-highlighting revisions, newest recorded by an instant applies, no defaults (SPEC DEF-6, AUD-4) |
| `deficiency_acknowledgement_rule` | A | revision, maximum_days, operation_id; administrator-set bound for acknowledgement periods; no default (SPEC DEF-7, [issue #151](https://github.com/JustApu/CheckYourAmbulance-archive/issues/151)) |
| `deficiency_evidence` | A | deficiency_id ref, check_id? ref, kit_inspection_item_id? ref, note?; exactly one evidence source; evidence never removed on invalidation |
| `deficiency_link` | A | two deficiency FKs in canonical ID order, type (`ADD`, `REMOVE`), prior_link_event_id? ref; no self-link; no transferred responsibility |
| `verification_scope_row` | A | deficiency_id, correction_event_id, item_id + definition_id refs, nullable kit_instance_id ref, non-null instance_key (physical kit UUID or all-zero UUID for no-kit), required_after, scope_revision; unique deficiency/scope_revision/item/instance_key; composite org-pair and definition/item FKs; correction-time kit/compartment scope or report-time vehicle scope; expansion appends, never shrinks past evidence |
| `deficiency_scope_expansion` | A | deficiency_id, event_id (SCOPE_EXPANDED or VERIFICATION_ADDED), item_id + definition_id refs, nullable kit_instance_id ref, non-null instance_key; composite org-pair FKs; items a supervisor added, included in every later correction's scope ([issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156), SPEC 1.22.0) |
| `check_suspension` | A | deficiency_id ref, vehicle_id ref, item_id ref, action (`START`, `END`), starts_at, prior_suspension_id? ref, ending_deficiency_event_id? ref; exact selected scope |

First records self-reference their root; corrections reference the same root
and the current chain tip. Enforce unique non-null predecessor_check_id so the
chain cannot fork. Corrections preserve the root's vehicle, item, author,
session and checklist identity. DB insert triggers verify root/predecessor
identity and author equality; the handler additionally rejects invalidated
chains. Invalidation requires an administrator, a nonblank reason, and a root
author different from the actor; it affects the whole chain. Do not modify
original outcome/state columns or revive predecessors.

Links have ordinal 0 for the primary earliest-due eligible obligation; every
ordinal > 0 requires shared_rule_id (DB CHECK). The handler locks item scope
and inserts ordinal 0 first; an insert trigger rejects additional links without
an existing primary and verifies rule membership and same vehicle/item scope.
Primary link rule may be null. Eligibility prioritizes on-time windows over
late linking, then earliest due (stable ID tie-break). Only a currently
selected routine obligation or an explicit non-routine ADH-1 order for the same
vehicle/item/kit may receive a new primary link;
superseded or retired rows cannot receive one and are excluded from current
missed/overdue determination. Historical links remain available as-of T.
Checks with no eligible
obligation have zero links. Correction chains recompute links together from
the root's recorded time; correcting later does not turn an early check into
a later inspection. Historical link rows remain; effectiveness is derived.

Failed/Unable writes create a deficiency or add evidence to an explicitly
selected one atomically; the server does not silently pick a suggested match.
Unable additionally requires a reason. Deficiency category scope is evaluated
on the referenced category revision, never a client-supplied supply flag.
Assignment/deadline may be supplied only by roles authorized in SPEC §3.3;
reporting does not grant crew members assignment rights. Unassigned reports remain
visible for supervisor assignment; the UI must not manufacture an assignee.

`deficiency_event` has a closed type-specific schema. Under a deficiency lock,
validate the predecessor, actor permission and SPEC §8.2 transition before
append. DB checks enforce nonblank explanations for dependency/admin close,
and typed required fields; cross-row transition checks are backed by an insert
trigger, not a CHECK containing a subquery. Assignment, extensions, dependency
details and acknowledgement revisions remain events, never overwritten notes.

Verification materializes correction-time kit/compartment membership and the
report-time vehicle-system affected-item snapshot, including supervisor
expansions. Kit rows preserve the physical instance; passing an identical item
in another kit does not satisfy the row. Only effective Passed checks with
`purpose=VERIFICATION`, the matching deficiency and time strictly after the
correction satisfy scope. Closure, suspension-end events, audit and outbox
intent commit with the last qualifying check. Locking the deficiency serializes
concurrent last-item passes and scope expansion. Invalidation of verification
reopens in the same command and notifies assignee plus supervisors. Added scope
after closure requires a new deadline and notifies only current assignee.

The approved [issue #31](https://github.com/JustApu/CheckYourAmbulance-archive/issues/31) rule reopens a Reported-in-Error deficiency when its
COR-3/COR-4 correction chain is invalidated; evidence remains marked invalid.
It neither restores the original Failed outcome nor implies a repair. This
edge follows INV-7; other transitions follow approved §8.2.

### Sealed kits

| Table | Kind | Columns and keys beyond common identity |
|---|---|---|
| `kit_seal_event` | A | kit_id ref, operation_id? (present on primary client event only), predecessor_event_id? ref, type (`BREAK`, `RESEAL`, `AUDIT_REQUEST`, `AUDIT_FULFILLED`), seal_marking?, earliest_expiration?, reason?, inspection_id? ref, audit_request_id? ref; unique kit/sequence and org/operation_id |
| `kit_inspection` | A | kit_id ref, checklist_version_id ref, break_event_id ref, fresh_entry_attestation, written_marking_attestation |
| `kit_inspection_item` | A | inspection_id ref, item_def_id ref, entered_count, entered_expiration DATE; unique inspection/item; exact complete membership required |
| `seal_substitution` | A | kit_id ref, reseal_event_id ref, operation_id, entered_marking, intact_attestation; unique org/operation_id |
| `seal_substitution_obligation` | A | substitution_id ref, obligation_id ref, conditional_rule_id ref; unique pair; explicit evidence, never a fabricated check |

One precreated `kit_lock` serializes verify/break/reseal/audit commands with
changes to deficiencies on listed kit items. It is acquired before item locks.
Reseal validates a break since the previous reseal and every current listed
item's fresh count/date. No missing, duplicate or unlisted entries; no prefilled
certified values. Dates before station-local today are rejected; today is valid.
Compute the minimum across all entries. Count mismatches create deficiencies
atomically but permit sealing. A seal cannot substitute while any affected
listed item has an open deficiency, audit is pending, seal is broken/expired,
or the particular obligation is supervisor-suspended. A matching entered crew
marking is required, but that marking is not the authenticated actor identity
and is not unique. Audit fulfillment requires break + complete inspection +
reseal and references the fulfilled request. Compare expected kit event tip:
concurrent commands based on one tip yield one success and one conflict.

### Command, audit and delivery infrastructure

| Table | Kind | Columns and keys |
|---|---|---|
| `command_receipt` | A | id, organization_id, operation_id, actor_user_id? ref, service_identity?, job_key?, command_type, request_digest, result_schema_version, response_status, response_body, recorded_at, correlation_id; unique org/operation_id; human/system CHECK below |
| `audit_event` | A | command_id? ref, request_attempt_id, actor_user_id?, roles_at_time, action, typed target refs?, recorded_at, correlation_id, outcome, reason_code, prior/new snapshot or immutable row refs; no raw requests/tokens |
| `notification_outbox` | A | command_id ref, source event ref (typed name and id), event_type, channel (`IN_APP`/`EMAIL`), recipient_user_id ref, template_key, minimal payload, same-site deep_link, mandatory flag at enqueue; unique source event/channel/recipient |
| `notification_delivery_event` | A | outbox_id ref, attempt_number, outcome, safe error_code?, recorded_at; unique outbox/attempt |
| `notification_read` | A | outbox_id ref (in-app item), user_id ref (the recipient), command_id ref, recorded_at; unique outbox; "unread" is the absence of a row |
| `attachment` | A | typed parent FK, object key, media type, byte size, digest, validation result; no uploaded bytes in DB; access derives from parent |
| `notification_pref` | M | user_id ref, event/channel, enabled; unique user/event/channel; cannot override mandatory organization policy |
| `notification_policy` | M | organization_id, event_type, channel, mandatory; unique organization/event/channel; administrator-set through an audited command; no default rows |
| `item_lock`, `kit_lock`, `vehicle_lock`, `deficiency_lock` | M | organization_id and target FK unique; no business state; prepared at publication/entity creation |
| `session_projection`, `deficiency_projection` | M | session_id/deficiency_id ref unique, cached state/activity or status/assignee/deadline/severity_version_id, last applied event ref; disposable, rebuildable, never historical authority |

The receipt reserves a client operation across **all** command kinds. A check,
reading, deficiency creation or seal command also carries the same operation_id
on its primary created row, unique per organization as §18.1 requires. Rows
generated as side effects refer to the command and ordinal; they are not
independent client creates. One receipt may describe several atomic effects.
Unknown/failed/warning operations leave no receipt. System jobs use deterministic
job keys plus business uniqueness (e.g. obligation natural key), not random IDs
on every rerun. A receipt CHECK permits exactly two cases: human actor non-null
and service_identity/job_key both null, or human actor null and both nonempty
service_identity and nonempty job_key. System role snapshots are an empty array;
service_identity is an allowlisted worker action, not a user. Business/event
actor columns and audit attribution must match the receipt; job events copy its
service identity. Anonymous denied-attempt audit rows have no receipt and remain
distinct from authenticated service commands. No fabricated user is needed.

Client and worker operation IDs occupy disjoint UUID keyspaces. Every HTTP
write requires an RFC 4122-layout **version 4** operation_id; internal job
receipts require **version 5**. Validate the parsed UUID's variant and version
without coercing its bits (never pass a version override to the UUID parser).
Reject noncanonical strings, other variants/versions and client service fields
as `400 MALFORMED_REQUEST` before receipt lookup or insertion. The receipt's
DB CHECK also requires v4 in the human case and v5 in the service case, with
canonical format/variant checks. Keep UNIQUE(organization_id, operation_id):
changing a job's version nibble to 4 produces a different key, not a collision.

Jobs derive operation_id using Python `uuid.uuid5(UUID(organization_id), name)`
where name is compact ASCII JSON of the ordered string array
`["cya-job-v1", service_identity, command_type, job_key]`, encoded with
`ensure_ascii=True, separators=(",", ":")`. job_key is itself a compact JSON
array of canonical string components (UUIDs lowercase, instants UTC RFC3339),
so concatenation cannot collide at field boundaries. It identifies a
business unit, not wake time: generation uses vehicle/item/D plus requirement
revision; reconciliation includes publication and prior selection; auto-close
uses session plus last persisted activity command; delivery uses outbox ID plus
attempt ordinal. Retries reuse the key; new activity/revision or a new delivery
attempt defines a new unit. Reject client-supplied service fields. Replay
compares complete human/service identity and command key; a UUID collision with
a different identity/key fails closed, never returns the other principal's
result, and records a separate service-failure audit plus an operator-visible
failed-job alert. Use the audit-only failure path if that audit cannot commit;
never describe a failed audit as persisted. The alert must not depend solely
on the colliding job's receipt/outbox, and must identify the blocked job safely
for recovery without deleting its competing immutable receipt.
Receipt payloads contain the stable committed acknowledgement,
not mutable current statuses. Read current status separately on replay.

## 3. Enforcement and transaction boundary

Runtime grants: `SELECT, INSERT` on **every A table** listed above; no UPDATE,
DELETE, DDL, TRIGGER, GRANT OPTION, REPLACE or upsert path. Schema owner creates
tables, constraints and BEFORE UPDATE/DELETE rejection triggers. It is never
the runtime connection. Runtime may update M headers/preferences and lock
anchors under audit; no blanket database-wide UPDATE grant. V draft tables
have narrowly scoped write grants plus publication-freeze triggers. Rebuildable
projections are separate tables, never the historical source. Keep an explicit
A/V/M table manifest in migrations for [issue #23](https://github.com/JustApu/CheckYourAmbulance-archive/issues/23) privilege enumeration tests.

M does **not** permit editing historical columns inside a header. Each M table
has a BEFORE UPDATE trigger rejecting changes to id, organization_id and any
creation command/actor/time columns it carries. Additional protected columns:

| M header | Additional immutable columns |
|---|---|
| `user` | idp_issuer, idp_subject; email/display_name/active remain audited mutable attributes |
| `inspection_session` | vehicle_id, checklist_version_id, opened_at (all header facts) |
| `deficiency` | operation_id, reported_at, reporter, category_version_id, severity_version_id, summary, description, vehicle_id, item_id, kit_id, compartment_id (all creation facts) |
| lock anchors | target IDs; locking must not retarget an anchor |

Use null-safe comparisons so null-to-value changes cannot evade a trigger.
Runtime has no DELETE on headers. Wholly immutable headers get SELECT/INSERT
only; triggers additionally protect against accidental future UPDATE grants.
Projection tables alone hold cached session/deficiency fields with restrictive
FKs to headers. Mutable attributes still require atomic audit. Django must
explicitly INSERT preallocated UUID rows (UUID-field default or
`force_insert=True`), not save via an UPDATE attempt. Published V rows **and
their children** reject UPDATE/DELETE. Test these boundaries beyond A tables.

The [spike](spikes/oq24-architecture-spike.md) demonstrated that INSERT succeeds
with UPDATE/DELETE denied on its three append-only tables, including through
Django. That is evidence for the mechanism, **not proof for these unbuilt
tables**. Every future table must pass positive append and negative mutation
tests using the actual runtime account; include malformed inserts and triggers.
DB checks do not authenticate an actor supplied by a compromised application;
server authentication and authorization are still mandatory. Schema-owner abuse
is outside runtime immutability and needs separate operational logging.

On every connection enforce UTC, strict SQL mode, foreign-key and check
enforcement, with startup refusal on mismatch. Use READ COMMITTED and explicit
row locks so reads after acquiring a lock see preceding commits. Verify actual
settings in MariaDB, not only Django configuration. All writers touching a
resource follow the same lock protocol, including jobs and corrections.

Command sequence:

1. For HTTP commands, authenticate, enforce CSRF and body/field limits, derive
   organization and actor from the session; never accept client role, service
   identity or server-time claims. Internal workers instead derive the fixed
   service identity, organization and deterministic job key from trusted job
   configuration and validate the allowed command type. They do not create a
   browser session or impersonate a role-bearing human.
2. Enter an explicit outer `transaction.atomic()` (not a surrounding
   `ATOMIC_REQUESTS` transaction). Lock actor/affected users in ID order and
   recheck active state and roles. Role changes follow the organization guard
   described above before user locks. Replays also require current access.
   Jobs have no actor user to lock; they validate their service command policy
   and then take the same resource anchors as human commands.
3. Look up the receipt and return a permitted replay if it exists. If absent,
   continue without a committed reservation; receipt-insert arbitration below
   resolves concurrent operations even when they target different resources.
4. Acquire existing anchors in order: vehicles, kits, items (vehicle/item
   order), deficiencies; sort IDs within each group. Discover all affected
   resources first and revalidate after locking. If scope grew, roll back and
   restart with the expanded set, bounded by the retry limit; never acquire a
   newly discovered earlier-order lock mid-transaction. A missing anchor is a
   configuration failure, never lazily created during a check.
   Deficiency reports and correction-time scope resolution also lock the
   organization's checklist-template rows in ID order after the vehicle and
   before deficiency rows, serializing scope snapshots with template publication.
5. Re-read the receipt after acquiring resource locks: a concurrent identical
   command may have committed while this request waited. Return that committed
   result before conflict validation. Resolve eligible obligations and chain state, compare concurrency bases,
   validate permissions, type rules, deficiency transitions and scope. Warnings
   or rejection roll back the entire command.
6. Preallocate result/event IDs and construct the complete acknowledgement.
   INSERT the complete receipt, then append business rows, related events,
   audit and notification intents referencing it in the same transaction.
   The receipt's JSON result references the allocated IDs; it has no reverse
   FK to not-yet-inserted business rows. Any later failure rolls it all back.
   Never update an append-only receipt.
7. Exit atomic successfully before acknowledging Saved. Publish live hints
   only after commit. A durable outbox preserves notification intent if the
   process dies before publication; clients reconcile authoritative queries.

**Receipt arbitration:** step 3 performs only a lookup. Step 6 INSERTs the
complete receipt under its unique key before any row references it. If two
transactions with different resource locks use the same operation ID, the
losing transaction rolls back, then reads and returns the winner after leaving
atomic. Catch `IntegrityError` outside atomic, and recognize only a confirmed
duplicate receipt key as replay; other integrity errors are failures. No update
to an A receipt and no separately committed reservation is needed. Preallocated
UUIDs from a rolled-back attempt are discarded; the winning result is returned.
Never `INSERT IGNORE`, `REPLACE`, or `ON DUPLICATE KEY UPDATE`.

Reuse of an operation ID by its original human actor or identical service/job
identity returns its original committed
result; it cannot execute a different command. Compare command type, actor and
digest to detect misuse; a different actor receives a generic conflict without
another user's result. A changed request from the same actor receives the
original acknowledgement with an operation-reuse indicator; clients must issue
a new UUID for a new decision. Canonical digest includes command/path and
validated semantic fields, not CSRF token or transport headers.

Retry a **fully rolled-back** deadlock at most three attempts with bounded
jitter; exhaustion returns `503 RETRY_LATER`. Scope rediscovery shares that
budget. Never blindly retry a timeout/disconnect with an ambiguous commit:
discard the connection and resolve using the same operation ID. All independent
commands remain possible; database critical sections are brief serialization,
not human-held inspection locks (CON-1).

### Command type insert guards

Per SPEC §17.7 item 4 and §16.1 (AUD-1, AUD-3), database triggers protect
append-only and versioned tables on `INSERT` to guarantee that every row
references a command receipt with an authorized `command_type`:

| Table | Trigger Name | Authorized `command_receipt.command_type` |
|---|---|---|
| `role_grant` | `cya_role_integrity_bi` | `'ROLE_CHANGE'`, `'INITIAL_CREW'`, `'BOOTSTRAP_ADMINISTRATOR'`, `'INITIAL_EMT'` |
| `checklist_publication` | `cya_checklist_publication_bi` | `'CHECKLIST_PUBLISH'`, `'OVERRIDE_PUBLISH'` |
| `stocking_deviation` | `cya_stocking_deviation_bi` | `'STOCKING_DEVIATION_RECORD'` |
| `status_request` | `cya_status_request_bi` | `'STATUS_REQUEST'`, `'STATUS_REQUEST_DECIDE'` |
| `vehicle_status_event` | `cya_vehicle_status_event_bi` | `'VEHICLE_STATUS_SET'`, `'STATUS_REQUEST_DECIDE'` |
| `schedule_rule` | `cya_schedule_rule_bi` | `'SCHEDULE_PUBLISH'`, `'SCHEDULE_SEED'` |
| `requirement_binding` | `cya_requirement_binding_bi` | `'REQUIREMENT_BIND'` |
| `requirement_version` | `cya_requirement_version_bi` | `'OBLIGATIONS_RECONCILE'` or `'AD_HOC_INSPECTION_REQUEST'` by binding origin |
| `obligation` | `cya_obligation_bi` | `'OBLIGATIONS_RECONCILE'` or `'AD_HOC_INSPECTION_REQUEST'` by routine flag |
| `obligation_selection` | `cya_obligation_selection_bi` | `'OBLIGATIONS_RECONCILE'` |
| `odometer_obligation` | `cya_odometer_obligation_bi` | `'OBLIGATIONS_RECONCILE'` |
| `audit_event` | `cya_audit_attribution_bi` | When `command_id` is non-null: `NEW.action = r.command_type` (or auxiliary actions `'ROSTER_CONFLICT_DETECTED'`, `'ROSTER_CONFLICT_NOTIFICATION_DELIVERED'`, `'ROSTER_CONFLICT_NOTIFICATION_FAILED'` when `r.command_type = 'RECORD_SESSION_ROSTER_CORRECTION'`) |

Attempted insertions referencing an unauthorized command type or mismatched
receipt attributes are refused at the database level with `SIGNAL SQLSTATE '45000'`.
A database-backed schema test (`inspect_command_receipt_guards` in `cya.core.audit_schema`)
dynamically enumerates all tables with a `command_id` column from `information_schema` to verify
that every such table possesses an active `BEFORE INSERT` receipt attribution guard (`IF NOT EXISTS`
check against `command_receipt` on `NEW.command_id` that raises `SIGNAL SQLSTATE '45000'`) strictly
matching organization, actor, recorded instant, authorized command type(s), and worker identity (where
applicable), with `audit_event` explicitly allowed as a specialized attribution guard.

### Failed and denied attempts

After leaving/rolling back the business transaction, open a separate atomic
audit-only transaction for `DENIED` or `FAILED`. Retain a server-generated
attempt UUID and correlation ID, safe action/target IDs only if visible, actor
and current roles if known, and a fixed error code. No rejected business rows,
untrusted body, exception SQL, token or forged actor are persisted. A missing
identity is anonymous, never a fabricated user. Conflict warnings may be
audited as attempts here; they consume no operation ID and create no business
events, activity timestamps or delivery intents.

If the audit-only write fails, return `503 AUDIT_UNAVAILABLE` (never success),
raise a sanitized operational alert, and keep business writes rolled back.
Do not claim a DB outage was successfully audited. If commit outcome is
unknown, record `OUTCOME_UNKNOWN` rather than falsely asserting rollback; replay
by operation ID resolves it. A committed business command whose response is
lost is not a failed business event. Outbox/network failure after commit does
not turn Saved into an uncommitted write. Notifications are at-least-once
delivery attempts with deduplication; no exactly-once SMTP claim is made.

## 4. Wire contracts

Proposed routes use authenticated same-origin JSON POST, `Content-Type:
application/json`. All HTTP writes include `operation_id`, a canonical lowercase
RFC 4122-layout version 4 UUID; all client endpoint schemas below inherit this
requirement. Version 5 IDs are reserved for internal jobs. The server returns
`correlation_id`. IDs below are schema fields, not literal synthetic placeholders
accepted by validation. A nullable field is sent as null or omitted; do not
coerce blank strings to IDs. `client_observed_at` is an optional RFC3339 instant.
Body cap: 256 KiB; lists cap: 1000, pagination default 50/max 200; oversize writes
reject wholly. Attachment bytes use a separately bounded validated upload path.

### Record a check

`POST /api/v1/checks`

Required: `operation_id`, `vehicle_id`, `item_id`, `checklist_version_id`,
`outcome` (`PASSED`, `FAILED`, `UNABLE_TO_INSPECT`, `NOT_APPLICABLE`),
`purpose` (`ROUTINE`, `VERIFICATION`). Optional: `session_id`,
`basis_check_id`, `acknowledges_check_id`, `quantity`, `measurement` (decimal
string plus unit), `explanation`, `client_observed_at`,
`verification_for_deficiency_id`, `deficiency`.

`deficiency` is exactly one of `{existing_id}` or `{create: {category_id,
severity, summary, description}}`; required for Failed/Unable. NA applicability
is server-derived from the published rule. Quantity/measurement must match item
kind and allowed outcomes. Verification requires its deficiency ID; routine
forbids it. Kit/asset presence at check time is resolved server-side and frozen.
Session and definition must match the selected vehicle/version; inactive or
absent items are not silently substituted with a newer version.

Success HTTP 201:

```json
{
  "status": "COMMITTED",
  "operation_id": "11223344-5566-4788-99aa-bbccddeeff00",
  "correlation_id": "aabbccdd-1122-4344-8566-778899aabbcc",
  "result": {
    "check_id": "12345678-1234-4234-8234-123456789abc",
    "server_recorded_at": "2026-10-20T12:00:00.000000Z",
    "obligation_links": [
      {
        "obligation_id": "abcdefab-cdef-4abc-8def-abcdefabcdef",
        "shared_rule_id": null,
        "timing": "ON_TIME"
      }
    ],
    "deficiency_ids": [],
    "event_ids": []
  }
}
```

Result arrays contain actual created IDs; each obligation link is
`{obligation_id, shared_rule_id, timing: ON_TIME|LATE}`. A link is written only
to the currently selected eligible obligation; additional links require an
audited shared-satisfaction rule. Empty links mean the check was recorded
without satisfying an obligation. Replay returns the stored status/body
(including original correlation ID), never an extra check.

HTTP 409 `CHECK_CONFLICT`: `{status: "CONFLICT", code, operation_id,
correlation_id, newest_check: {check_id, actor: {user_id, display_name},
server_recorded_at, outcome}, obligation_id}`. Only authorized visible fields
are returned. The comparison scope is the resolved item+obligation, not merely
the session. If no obligation is eligible, compare the unlinked item scope.
The newest effective row uses chain state and aggregate order. Same-actor
newest check is not a warning. Cancel creates nothing; proceed uses a **new**
operation UUID and `acknowledges_check_id` from this warning. If a further check
arrives, return a fresh warning; retain all committed failures and passes.

### Correct an entry

`POST /api/v1/checks/{check_id}/corrections`

Required: `operation_id`, `expected_tip_check_id`, `outcome`; optional typed
observation fields and explanation/deficiency selection as above. The target
identifies the chain; the expected tip must still be current. Author comes
from authentication. Never accept `actor_user_id` or a replacement author.
Success 201 uses the check acknowledgement plus `root_check_id`,
`supersedes_check_id`, and `deficiency_event_ids`.

Non-author: 403 `AUTHOR_REQUIRED`. Invalidated chain: 409
`CHAIN_INVALIDATED`. Stale tip: 409 `CHAIN_CHANGED` with authorized current tip.
Failed→Passed uses COR-4 atomically under all supporting deficiency locks: only
the Correct-entry flow, no other effective failed evidence and no corrective
action/verification can emit Reported in Error. Otherwise retain the deficiency
and enqueue supervisor notification. This endpoint never means repair.

[issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) implements UTI-3 for the supporting Unable chain: correction to Passed or Failed
appends a `SUPERSEDED` child to its current supervisor completions in the same transaction.
The corrected outcome governs, historical completions remain visible before the event, and
the existing linked deficiency is unchanged. Unable-to-Failed uses the chain's existing
deficiency evidence; other Failed correction paths requiring new deficiency handling remain
deferred. Whole-chain invalidation appends `INVALIDATED` children and reports their original
completion IDs in `voided_determination_ids`. Replays append no additional effects.

### Invalidate a chain

`POST /api/v1/checks/{check_id}/invalidations`

Required: `operation_id`, `explanation`. Any selected chain row resolves to its
root. Success 201 result: `root_check_id`, `invalidation_event_id`,
`affected_obligation_ids`, `reopened_deficiency_ids`, `voided_determination_ids`.
Nonadministrator: 403 `ADMINISTRATOR_REQUIRED`; own chain: 403
`SELF_INVALIDATION_FORBIDDEN`; empty reason: 422 `EXPLANATION_REQUIRED`.
An already-invalidated root with a different operation ID returns 409
`CHAIN_INVALIDATED`; replay of the successful operation still returns success.
Whole-chain exclusion, determination voiding, verification reopening, audit and
notification intents are atomic. No earlier outcome revives. The additional
Reported-in-Error reopening case follows INV-7 in approved SPEC 1.3.0.

### Report a deficiency independently

`POST /api/v1/deficiencies`

Required: `operation_id`, `vehicle_id`, `target` (exactly one of `{item_id}`,
`{kit_id}`, `{compartment_id}`, `{vehicle: true, system_ids: [...]}`),
`category_id`, `severity`, `summary`, `description`. Optional `session_id`,
`related_deficiency_ids`; assignments/deadlines only if permitted by §3.3.
Resolve target scope and category revision on the server, snapshot affected
items, and append the initial Open event. Success 201 result:
`deficiency_id`, `creation_event_id`, `related_link_event_ids`. Related links
are informational and explicitly chosen; no implicit merging or closure.

Repair uses a separate `POST /api/v1/deficiencies/{id}/corrective-actions`:
`operation_id`, `expected_event_id`, `description`, optional evidence refs.
It creates a corrective-action event and scope revision and moves to Pending
Verification along the allowed §8.2 transitions. It does not rewrite checks.
Verification uses `/checks` with the explicit purpose and deficiency reference.
Other lifecycle handlers consume the event schemas above and the same
authorization, expected-predecessor and transaction rules; new actions absent
from §3.3 are denied rather than inferred from HTTP verbs.

### Seal commands and query results

`POST /api/v1/kits/{id}/seal-verifications`, `/seal-breaks`, `/reseals`,
`/audit-requests` take operation_id and expected_event_id. Verification adds
`entered_marking`, `intact_attestation`; break adds `reason`; audit requests
require Supervisor/Administrator. Reseal adds `new_marking`, both attestations,
and `contents: [{item_def_id, count, expiration_date}]`. Success returns the
created event/substitution, deficiency IDs, computed earliest expiration and
fulfilled audit IDs. Stale kit tip gives 409 `KIT_CHANGED`; expired/open/audit/
deficiency/suspension gates give 422 `SEAL_NOT_ELIGIBLE` with safe reason codes.
No response claims that sealing resolved a deficiency.

Query DTOs expose `obligation_state`, `item_attention`, `deficiency_status` and
`operational_status` separately, with evidence IDs, server time and projection
version. They distinguish Passed, Failed, NA, carried-over, suspended, exempt,
Unable, supervisor-completed, superseded, invalidated and late completion.
Seal-substituted obligations name substitution evidence, never a Passed check.
Live hints contain vehicle ID and changed evidence IDs; query authorization
is reapplied on refresh and WebSocket subscription/revalidation.

### Common errors and authorization

| HTTP | Code | Meaning/effect |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | invalid JSON/UUID/schema or unknown field; no business write |
| 401 | `AUTHENTICATION_REQUIRED` | no valid active session; no token details exposed |
| 403 | `FORBIDDEN`, `AUTHOR_REQUIRED`, `ADMINISTRATOR_REQUIRED` | deny per matrix/ownership; audit independently |
| 403 | `CSRF_REJECTED` | origin/token validation failed; safe attempt audit only |
| 404 | `NOT_FOUND` | absent or inaccessible target; no existence leak |
| 409 | `CHECK_CONFLICT`, `CHAIN_CHANGED`, `CHAIN_INVALIDATED`, `KIT_CHANGED`, `OPERATION_OWNED_BY_ANOTHER_ACTOR` | no new business commit or receipt; the operation ID may already belong to another principal |
| 413 | `REQUEST_TOO_LARGE` | bounded parser/list rejection |
| 422 | `INVALID_OUTCOME`, `EXPLANATION_REQUIRED`, `INVALID_TRANSITION`, `INVALID_LINK`, `SEAL_NOT_ELIGIBLE` | field/rule failure, whole command rolled back |
| 503 | `RETRY_LATER`, `AUDIT_UNAVAILABLE`, `CONFIGURATION_UNAVAILABLE` | no success acknowledgement; retry same operation ID |

Errors use `{status: "REJECTED", code, correlation_id, field_errors: [...]}`;
field errors name safe schema paths and fixed codes, not raw user content.
Conflict warnings use their dedicated shape. Authorization is evaluated before
revealing target details and again while holding relevant identity/resource
locks. Union-of-roles applies, including Supply Officer conditional scope;
no roster participation prerequisite. Replays do not bypass revocation.

## 5. Later-release references

Reserve the same UUID/organization key convention and typed extension points:
`equipment_asset(id, organization_id)` registry stub for nullable check asset
FKs; `vendor`, `maintenance_rule`, `service_contract`, `reg_requirement` and
their immutable version/event identities for later releases. Only the asset
identity stub is needed in R1; no maintenance fields/workflow are implemented.
Future links point **to** immutable R1 checks, deficiencies, vehicles and audit
events using their existing IDs; do not add/update backlinks in old records.
Kit movement and asset assignment histories append effective intervals and
retain identity-at-check-time. Inventory movement, purchase orders/receipts,
maintenance and regulatory histories remain separate streams, not reused check
outcomes. Reserved bulk_group_id remains null and bulk_eligible unused in R1.
Report schedules/saved filters receive IDs later with explicit permission policy;
this issue does not implement R2–R4 or invent export privileges.

## 6. Acceptance and verification handoff

These are implementation test obligations, **not passing application tests**.
Use local synthetic fixtures and injected providers; no network in tests. The
existing [scenario library](../tests/TRACEABILITY.md) predates SPEC 1.2.0;
AC-11 is now deferred and AC-33–35 still need scenario/runner follow-up. Do not
reinterpret its symbolic steps or old permission row counts as runtime proof.

| SPEC criteria | Contract evidence and required tests |
|---|---|
| AC-1, AC-2 | independent check/obligation links; session close never creates satisfaction |
| AC-3, AC-4 | UTC windows, due-version binding, early/late precedence; DST gap/fold fixtures; pre-window replacement with one selected obligation |
| AC-5, AC-23 | immutable roster inputs/determinations; stale/missing/disagreeing fail closed for new determinations; an existing waiver is reinstated only by a newer change, unavailability, conflict or check invalidation, never by unchanged roster data aging (CND-7) |
| AC-6 | pinned session version, activity events and delayed auto-close race |
| AC-7, AC-8 | commit-only acknowledgement, lost response/replay, no partial effects |
| AC-9, AC-10, AC-31 | two actual DB connections, basis/ack race, failure survives Pass, warnings consume no UUID |
| AC-11 | deferred; reject R1 bulk writes |
| AC-12, AC-13 | author enforcement, fork refusal, COR-4 evidence/repair race under locks |
| AC-14, AC-15, AC-32 | whole-chain invalidation, explanation/admin checks, no revival, deficiency independent; [issue #31](https://github.com/JustApu/CheckYourAmbulance-archive/issues/31) edge follows INV-7 |
| AC-16, AC-17 | materialized complete scope, last-two-pass race, added-scope race, new deadline and recipient rules |
| AC-18, AC-29 | suspension and supervisor UTI events distinct from Pass; void supporting check tests |
| AC-19, AC-20 | manual status event only; OOS intervals exempt rather than Passed |
| AC-21 | one odometer obligation per inspection, anomaly confirmation, reading history |
| AC-22, AC-24 | stable identity, inactive attribution, all matrix allow/deny/scope cases, direct forbidden POSTs |
| AC-25 | enumerate all A tables: append allowed, UPDATE/DELETE/DDL denied; test immutable columns of every M header and published V rows/children, including null changes, with positive authorized mutable-field updates |
| AC-26, AC-27 | replay randomized histories and compare projection/as-of results before/after correction/invalidation |
| AC-28 | no-network providers and isolated throwaway DB; never runtime data |
| AC-30 | primary ordinal plus ruled additional links, no eligible link, rejected unruled second link |
| AC-33–35 | seal/date boundaries, all item observations, short/extra counts, open deficiencies, audit/break/reseal races |

Additional failure tests: duplicate operation UUID across command types/actors;
invalid cross-organization FKs; audit insertion failure rolls back all business
rows; failure audit survives rollback; audit-only DB failure returns 503;
deadlock retry exhaustion; disconnect after commit; process death after commit
before notification; missing anchors; role revocation concurrent with commands;
verification invalidation and reseal concurrently changing deficiency eligibility.
Add direct stale ORM/header UPDATE attempts against reporter/reported_at,
category revision, session pinned version and provider subject: each must fail
at the DB boundary without altering history. Test root self-referencing INSERT
with the author/root trigger under actual runtime grants, followed by valid
correction INSERT and rejected root/author changes.

Publish/generation tests must race two real connections with pre-created
pre-window obligations: after replacement there is one selected obligation,
the prior row has a supersession determination, replay adds nothing, and as-of
before publish shows the old selection. Include future-effective publication,
removed/reintroduced occurrences, generator starting before publish but taking
locks afterward, and in-window obligations preserved without apply-to-open.
Attempt a second selection tail/sequence directly and assert DB rejection.

Job tests cover obligation generation, inactivity auto-close and delivery with
null actor_user_id, correct service attribution and receipt linkage. Reject
missing/partial/mixed human/service attribution; verify same-key UUID/replay
across restarts, distinct revision/activity/attempt keys, no duplicated effects,
and collision with another identity refused without returning its receipt.
Compute a future job's UUIDv5 using known organization/business IDs, submit it
as a human command's operation_id, and assert `400 MALFORMED_REQUEST` with no
receipt or business rows. Then run the job and assert its v5 receipt/effects
commit normally. Include every HTTP write path, malformed/noncanonical UUIDs,
wrong variants, other versions and direct DB inserts violating human-v4 and
service-v5 CHECKs. Verify genuine v4 replay still returns its committed result.
Inject an internal same-key/different-identity collision and assert independent
service-failure audit and operator alert, without receipt deletion or disclosure.
Database tests must use MariaDB 10.11 and its real runtime grants. SQLite or
mocked ORM tests cannot establish locking or privilege behavior.

## 7. Sources and remaining gates

The [architecture spike report](spikes/oq24-architecture-spike.md) is the
repository's observed evidence for MariaDB 10.11 and the Django prototype.
Official documentation consulted 2026-10-20:

- [Django transactions](https://docs.djangoproject.com/en/6.0/topics/db/transactions/):
  atomic rollback, catch database errors outside atomic/savepoint blocks,
  post-commit callbacks. Callbacks alone are not durable delivery.
- [Django database notes](https://docs.djangoproject.com/en/6.0/ref/databases/):
  MariaDB backend, isolation and strict-mode configuration. The tooling issue
  chooses/pins the actual Django version; verify that version again at coding.
- [Django INSERT selection](https://docs.djangoproject.com/en/6.0/ref/models/instances/#how-django-knows-to-update-vs-insert):
  UUID defaults and force_insert must preserve insert-only command writes.
- [MariaDB constraints](https://mariadb.com/docs/server/reference/sql-statements/data-definition/constraint):
  primary/unique/foreign keys and CHECKs. Cross-row state rules need handler
  serialization/triggers, not an invented cross-table CHECK.
- [MariaDB grants](https://mariadb.com/docs/server/reference/sql-statements/account-management-sql-statements/grant):
  table-scoped privileges and trigger execution. Test actual accounts rather
  than assuming framework model flags enforce database immutability.
- [Python UUIDs](https://docs.python.org/3/library/uuid.html): `uuid5` derives a
  stable UUID from a namespace UUID and name string; jobs use the encoding above.
- [MariaDB trigger limitations](https://mariadb.com/docs/server/server-usage/triggers-events/triggers/trigger-limitations):
  triggers run per row, cannot return result sets or explicitly commit; the
  described guards validate rows inside the caller's transaction.

Owner gates retained: the pilot gates in SPEC §17.8 DEP-12
(operator and deputy named, restore rehearsal, trigger setting confirmed on the
production host); real checklist definitions; Workspace claim/revocation
verification before sign-in implementation; applicable BBP decisions before
production.
No production permission, migration execution or merge authorization is implied.
