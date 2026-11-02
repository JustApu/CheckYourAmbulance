# Item attention, checklist views and the view preference

[issue #83](https://github.com/JustApu/CheckYourAmbulance-archive/issues/83) adds the server-side projections and queries behind the crew checklist: the item
attention state (SPEC §7.5, §19.4), the Normal and Priority views and the Needs Attention Only
filter (SPEC §6.5 VW-1, VW-2), known deficiencies and the vehicle's operational status beside
the items (VW-3), and a per-crew-member preference synchronized across devices (OQ-38). Code:
`cya/attention/` (`state.py`, `facts.py`, `checklist.py`, `preferences.py`, `api.py`). The crew
screens that call these endpoints are [issue #85](https://github.com/JustApu/CheckYourAmbulance-archive/issues/85); the deficiency lifecycle (closing, suspension)
is M4, and suspended checks are described [below](#the-checklist-query).

## Item attention state

An item is **red** (`ATTENTION`) when an unresolved deficiency applies to it, or when its latest
effective check is Failed or Unable to Inspect; otherwise it is `CLEAR`. The two causes are
independent, so a later Pass clears the check cause but never an unresolved deficiency.

- **Unresolved** means Open, In Progress, Awaiting External Dependency or Pending Verification.
  Closed — Verified, Closed — Administrator and Reported in Error do not count.
- **Applies** means the item is in the deficiency's scope snapshot (`deficiency_affected_item`).
  A deficiency that names a kit instance applies to that instance only; one that names none
  (an independent report on an item that sits in several kits) applies to every instance, so
  the state errs towards red.
- **Latest effective check** is the effective outcome of the correction chain whose effective
  row was recorded most recently, ignoring invalidated chains (SPEC §19.1). Ties break on the
  check id. A Pass corrected into a Fail after a newer Pass therefore becomes the latest; a
  Fail is never hidden by an older row.
- **Per kit instance** (SPEC §19.4 as amended, OQ-53): the key is `(item, kit instance)`, with
  the all-zero `NO_KIT` key for an item outside a kit. A failure in one of two identical kits
  is red for that kit only.
- **Rebuildable, nothing stored.** The state is derived on demand from the append-only check,
  correction and deficiency events (SPEC §17.4). `item_attention(..., as_of)` replays the
  events recorded by an instant, so a past answer never changes. Without an instant it reads
  the live `deficiency_projection` for speed. `verify_attention` is the recompute check: it
  compares the live result with the event replay and lists every disagreement (the projection
  is a disposable cache; the events win).

## The checklist query

`vehicle_checklist(organization_id, vehicle_id, view=, needs_attention_only=, as_of=)` returns
one row per checklist item, and one per kit instance for an item inside a kit. The items are
the union of the published templates **bound to the vehicle** (a `requirement_binding` BIND in
force at the instant), resolved at the instant, first template by id winning an item two
define; a template published for another vehicle, or unbound, adds nothing. The kit instances
are read from the append-only assignment history as it stood at the instant, never from the
mutable `kit_instance` row, so a past checklist keeps a kit that was later moved or deactivated
and omits one added later (none when the vehicle has no such kit, which is also what the
obligation generator owes). Publications, bindings and assignments recorded after the instant
are ignored. Each row carries the obligation state (the most urgent
of the obligations in the standard window: Overdue, Due, Current, Suspended, Exempt, Waived,
Satisfied, Upcoming), the due instant, the attention state with its causes, `needs_action`,
and the unresolved deficiencies that apply (id, status, summary, severity code and label,
reported instant). The result also has the vehicle's operational status at the instant
(`IN_SERVICE` when no status was ever recorded) with its restrictions.

- **Normal**: template order, then display order, then item id.
- **Priority**: red rows first, then rows that need action, then the rest, each in Normal order.
- **Needs Attention Only**, in either view: keeps a row that is red, or whose obligation is
  Overdue, Due or Current, except a row shown as Suspended (below). The spec words "actionable due, overdue, incomplete" are read as
  the three states in which an unsatisfied obligation can be checked now; Upcoming, Satisfied,
  Exempt and Waived rows are not actionable. This reading is recorded as an assumption on the
  PR.
- **Suspended checks** ([issue #13](https://github.com/JustApu/CheckYourAmbulance/issues/13), SPEC §8.9, VW-2, AC-18). A row whose routine check a
  supervisor suspended for a known deficiency ([deficiencies](deficiencies.md#supervisor-check-suspension))
  shows obligation state `SUSPENDED`, which the crew screen labels "Known Deficiency — Check
  Suspended". It is never actionable and is left out of Needs Attention Only even though its
  unresolved deficiency keeps it red; it stays in the full checklist, in either view, with the
  deficiency listed beside it. A row still shows Overdue, Due or Current, and stays in the filter,
  when another of its obligations in the window needs action, such as one missed before the
  suspension started. Once the suspension ends, an obligation that stays Suspended in history
  ranks after every other state, so the row shows its current obligation again and is no longer
  left out of the filter. Suspensions are read from storage; the `suspension_lookup` argument replaces
  that evidence when supplied. Reading AC-18 as excluding a suspended row even when it is red from a
  different cause (another deficiency, or a Failed check recorded anyway) is an implementation
  assumption recorded on the PR.

Each row also carries what the crew screen needs to record a check ([issue #85](https://github.com/JustApu/CheckYourAmbulance-archive/issues/85)): `checklist_version_id` (the version that defines the item), `expected_quantity`, `unit`, `allowed_outcomes`, and the newest effective check whatever its outcome (`latest_check_id`, `latest_check_outcome`, `latest_check_at`, `latest_check_by`, the author's display name, which the conflict warning already shows every crew member). `attention_check_id` is still set only for a red check; `latest_check_id` is the basis a client sends as `basis_check_id` (SPEC CON-3).

Errors: `INVALID_VIEW`, `INVALID_FILTER`, `INVALID_AS_OF` (400), `NOT_FOUND` (404, also for
another organization's vehicle), `CHECKLIST_UNAVAILABLE` (409, a vehicle slot a definition
needs is missing).

## View preference

`user_preference` (an M table, one row per user, `preferred_view` NORMAL or PRIORITY and
`needs_attention_only`) belongs to the signed-in user and follows them across devices. The
only writer is `cya.attention.preferences.save_preferences`, an audited command
(`SAVE_VIEW_PREFERENCES`): one transaction, an operation-id receipt that replays the original
result, and an audit event with the prior and new values. A user with no row has the defaults
Normal view, filter off. The first save of two devices at once serializes on the user row.

**Authorization assumption.** A user may read and change only their own preference. The
identity is the authenticated actor, never a request field, so no SPEC §3.3 role permission
applies: the actor must be an active user of an active organization, any role. If the owner
wants this governed by the matrix, it is a SPEC amendment.

Schema guards (migration 0002): UUID check, an organization-pair foreign key to the user, and a
trigger rejecting any change to `id`, `organization_id` or `user_id`. The runtime account gets
`SELECT, INSERT, UPDATE` on the table from `manage.py audit_runtime_grants` like the other M
tables.

## HTTP

| Route | Purpose |
|---|---|
| `GET /api/v1/vehicles/{id}/checklist?view=&needs_attention_only=` | The query above at the present instant; the user's saved view and filter apply unless the query names them |
| `GET /api/v1/me/view-preferences` | The saved (or default) view and filter |
| `POST /api/v1/me/view-preferences` | `{operation_id (a version 4 UUID, else 400), preferred_view, needs_attention_only}`; `200` with the stored values; a replay returns the stored result |

The checklist endpoint takes no `as_of`: historical reconstruction for people is [issue #87](https://github.com/JustApu/CheckYourAmbulance-archive/issues/87). The
query function is as-of capable and tested for it. Reads need a signed-in active user; errors
use the record-check shape.

## Fleet readiness (issue #17)

[issue #17](https://github.com/JustApu/CheckYourAmbulance/issues/17) adds the supervisor fleet dashboard of SPEC §14 RPT-5 and RPT-7
(`cya/attention/readiness.py`, `readiness_views.py`). It is read-only and adds no business rule:
every fact comes from an existing reader, replayed at the instant answered for.

| Route | Purpose |
|---|---|
| `GET /api/v1/fleet-readiness[?as_of=]` | The picture as JSON |
| `GET /fleet/readiness/[?as_of=]` | The screen, linked from the deficiencies index for Supervisors and Administrators |
| `GET /fleet/readiness/panel/` | The screen's live part, which the open page re-reads |

**Who.** Supervisors and Administrators. Crew and Supply Officers get 403 from the endpoint and the
audited section refusal from the screen; roles combine. No row of the SPEC §3.3 matrix names this
view, so the check names the two roles the issue gives.

**Per vehicle:**
- operational status and restrictions;
- every obligation due from 7 days before to 48 hours after the instant (the checklist's
  standard window), counted by state;
- inspection progress, the recorded checks out of the checks owed (satisfied plus Current, Due
  and Overdue);
- the deficiencies being worked at the instant, each with severity, status, governing deadline,
  Overdue, age in days, Long-running (only when an age rule is configured, issue #150), any
  external dependency with its follow-up, and an escalation acknowledgement still in force;
- counts by severity and status;
- the check suspensions in force.

**RPT-7.** States are counted separately and never folded into a "compliant" flag:
- **From the obligation evaluator:** Passed, Failed, Not Applicable (rule), Carried over (a
  waiver by a carry-over rule), Waived (another rule), Suspended, Exempt (out of service), Completed
  by supervisor — unable to inspect, and the open states Overdue, Due, Current and Upcoming.
- **From the check and determination histories,** following the planning review on the issue
  rather than the checklist's one-row-per-item summary:
  - Unable to inspect: an obligation still owed with an effective Unable check;
  - Invalidated: a withdrawn check chain, or an invalidated determination;
  - Superseded: each entry a correction replaced, and each superseded determination;
  - Completed late: a satisfied obligation completed after its grace deadline, counted in addition
    to its outcome.

**As of.** `as_of` must be an offset-qualified instant not in the future (400 otherwise). The answer
uses only what was recorded by then (AC-27): obligations, checks, statuses, deficiency events,
suspensions and acknowledgements. A past picture lists every vehicle of the organization, including
ones deactivated since, because a vehicle's active flag has no history. The screen then says it is
a past picture and does not update.

**Live.** The screen re-reads its panel:
- on a vehicle hint from the existing live channel, for up to 3 vehicles (one fewer than the default
  per-user socket limit, leaving room for another tab);
- on reconnect and when the tab is shown again;
- every 60 seconds, which also covers changes no event announces, such as a deadline passing or an
  acknowledgement expiring.

It says how many vehicles are live and shows a notice when a re-read fails. When sign-in ends it
stops promising live updates. It adds no hint producer.

**Bounds.** Owner decision (2026-11-01): start with a small fleet. At most 25 vehicles and
100 deficiencies per vehicle, each with an explicit "only the first … are shown"; the database
reads at most one row more than is shown. Per vehicle:
- obligation state through the existing loader (about 13 queries);
- the status;
- the bounded deficiency list.

For the whole page, in one bounded read: the listed deficiencies' events, replayed in memory with
the same rules as the per-deficiency readers (`deadlines.deadline_state`,
`service.replay_dependency`, `acknowledgement_status`, the newest re-grade). The number of queries
therefore does not grow with the number of deficiencies. More than 10,000 events is refused (503),
never cut. The age rule and the suspensions are each read once.
[issue #33](https://github.com/JustApu/CheckYourAmbulance/issues/33) replaces the per-vehicle
obligation reads with a batch loader before a larger fleet.

`cya/attention/tests/test_readiness.py` covers:
- each state counted apart (real checks for Passed, Failed, Unable, Invalidated, Superseded,
  Suspended and Exempt; injected states for the rule-based and late ones);
- the deficiency facts, including an acknowledgement that expires;
- both limits (the deficiency query carries the limit), a query count that does not grow with the
  number of deficiencies, and bulk facts equal to the per-deficiency readers;
- as-of reproduction after later events;
- refusal of future and naive instants;
- the HTTP roles and the screen.

`readiness_script_harness.js` runs the screen script against fake sockets, timers and fetch:
hints, reconnect, the timer, the stale notice, sign-in end and one read at a time.

## Not built here

The crew screens ([issue #85](https://github.com/JustApu/CheckYourAmbulance-archive/issues/85)), and live-update wiring of the checklist (hints from [issue #84](https://github.com/JustApu/CheckYourAmbulance-archive/issues/84) prompt
a re-read of this endpoint). The supervisor screens for the deficiency lifecycle and suspension are
[issue #16](https://github.com/JustApu/CheckYourAmbulance/issues/16) ([deficiency workflows](deficiency-workflows.md#supervisor-and-administrator-actions-issue-16)).

[issue #86](https://github.com/JustApu/CheckYourAmbulance-archive/issues/86) adds `obligation_outcome` to checklist rows so clients can distinguish Failed satisfaction and supervisor completion from Passed; see [deficiency workflows](deficiency-workflows.md).
