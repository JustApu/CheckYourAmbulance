# Item attention, checklist views and the view preference

[issue #83](https://github.com/JustApu/CheckYourAmbulance-archive/issues/83) adds the server-side projections and queries behind the crew checklist: the item
attention state (SPEC §7.5, §19.4), the Normal and Priority views and the Needs Attention Only
filter (SPEC §6.5 VW-1, VW-2), known deficiencies and the vehicle's operational status beside
the items (VW-3), and a per-crew-member preference synchronized across devices (OQ-38). Code:
`cya/attention/` (`state.py`, `facts.py`, `checklist.py`, `preferences.py`, `api.py`). The crew
screens that call these endpoints are [issue #85](https://github.com/JustApu/CheckYourAmbulance-archive/issues/85); the deficiency lifecycle (closing, suspension)
is M4.

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
  Overdue, Due or Current. The spec words "actionable due, overdue, incomplete" are read as
  the three states in which an unsatisfied obligation can be checked now; Upcoming, Satisfied,
  Exempt and Waived rows are not actionable. This reading is recorded as an assumption on the
  PR.
- **Suspension extension point (M4).** A row whose routine check is suspended arrives with
  obligation state `SUSPENDED`, which is never actionable. It drops out of the due, overdue and
  incomplete part of the filter but stays in the full checklist, and its unresolved deficiency
  still makes it red and keeps it in the filter (SPEC VW-2, SUS-1). Suspension itself is M4;
  until its evidence is supplied through the `suspension_lookup` argument no obligation is
  ever `SUSPENDED`.

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

## Not built here

The crew screens ([issue #85](https://github.com/JustApu/CheckYourAmbulance-archive/issues/85)), live-update wiring of the checklist (hints from [issue #84](https://github.com/JustApu/CheckYourAmbulance-archive/issues/84) prompt
a re-read of this endpoint), supervisor suspension and the deficiency lifecycle (M4).

[issue #86](https://github.com/JustApu/CheckYourAmbulance-archive/issues/86) adds `obligation_outcome` to checklist rows so clients can distinguish Failed satisfaction and supervisor completion from Passed; see [deficiency workflows](deficiency-workflows.md).
