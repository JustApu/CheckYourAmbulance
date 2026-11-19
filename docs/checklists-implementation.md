# Fleet, location slots and versioned checklists ([issue #55](https://github.com/JustApu/CheckYourAmbulance-archive/issues/55))

Implementation notes for the `cya.checklists` app. The product rules are SPEC §4.1–§4.5 and §18; the table catalogue is [data-model-contracts.md §2](data-model-contracts.md#2-table-catalogue). SPEC and the contracts win if they disagree with this page. This page records what was built, the places where the physical schema completes or differs from the contract text (each was a reversible implementation choice, not a product decision), and what is deliberately left to later issues.

## What exists

| Piece | Where |
|---|---|
| Models and explicit table names | `cya/checklists/models.py` |
| Tables, UUID/value checks, organization-pair keys, freeze triggers | migrations `0001_initial`, `0002_publication_guards` |
| Overlay resolution engine | `cya/checklists/overlay.py` |
| Definition validation (item kinds, quantities, placement) | `cya/checklists/validation.py` |
| Audited commands (draft, publish, override, stocking deviation) | `cya/checklists/commands.py` |
| Synthetic sample agency seed and `seed_synthetic_fleet` command | `cya/checklists/seed.py`, `tests/fixtures/demo-fleet.json` |
| Tests | `cya/checklists/tests/` |

Table kinds match `tools/audit_enforcement.py`: `vehicle`, `kit_instance`, `checklist_template`, `item` are mutable (M); `compartment`, `vehicle_system`, `checklist_version`, `checklist_item_def`, `vehicle_override_version`, `vehicle_override_item`, `stocking_deviation` are versioned (V); `checklist_publication` and `checklist_cutover` are append-only (A, with the standard `append-only table` triggers the schema inspector requires).

## Vehicle-owned checklists (SPEC 5.0.0, [issue #220](https://github.com/JustApu/CheckYourAmbulance/issues/220))

SPEC 5.0.0 replaces shared templates, vehicle overrides and template bindings with checklists that each vehicle owns (CHK-1 to CHK-10). The physical tables are reused, so sessions, checks, obligations and reports, which already pin these rows, need no change:

| Logical entity (SPEC §18) | Physical rows |
|---|---|
| `vehicle_checklist` | a `checklist_template` row whose `vehicle_id` is set, with its `precedence` ordinal (CHK-10) and no key |
| `vehicle_checklist_version` | `checklist_version` of that row, its `checklist_item_def` rows (a row with neither compartment nor system is **Not placed**, CHK-9) and its `checklist_kit_placement` rows; published by a `checklist_publication` row |
| `vehicle_checklist_assignment` | `requirement_binding` rows (`BIND` starts an assignment, `UNBIND` ends it) naming the checklist and its own vehicle |
| legacy `checklist_template`, `checklist_version`, `vehicle_override_version`, `requirement_binding` | the same tables, rows whose template has no vehicle |

**The cutover.** An organization moves to the new model at one recorded instant, its `checklist_cutover` row (written by the migration of SPEC §18.2, issue #220 part 2). `cya/checklists/ownership.py` holds the rule: a legacy template's bindings govern due instants before the cutover only, a vehicle checklist's assignments govern due instants from it on, and a reader bounded "as the records stood" before the cutover (`recorded_before`, as the planner's freeze instant uses) still sees the legacy model. Before the cutover nothing about the legacy model changes.

**Guards** (migrations `0008_vehicle_checklists`, `0009_vehicle_checklist_guards`, and obligations `0017_vehicle_checklist_assignment_guard`), each repeated by the commands:

- a vehicle checklist names its owner through an organization-pair key, has a precedence ≥ 1 that is unique per vehicle and no key, and its owner and precedence never change;
- vehicle checklists are created and assigned only after the cutover; after it nothing new is written to a legacy template, its versions, its publications, an override or a legacy binding, and a legacy template row cannot be updated;
- an item row of a vehicle checklist version is placed on that vehicle (compartment, system, or a kit in one of its compartments) or not placed at all; a legacy template or override row keeps a placement;
- no override overlays a vehicle checklist (CHK-6 withdrawn);
- an assignment (`requirement_binding`) of a vehicle checklist names that checklist's vehicle.

**Commands.** `create_vehicle_checklist` (`cya/operations/templates.py`, command `VEHICLE_CHECKLIST_CREATE`, policy action `draft_edit_template`) creates an active checklist for one vehicle with the vehicle's next precedence ordinal; it locks the vehicle row so concurrent creations get distinct ordinals. `save_template_draft` and `publish_template_version` serve vehicle checklists too: placements are validated against the owning vehicle, a Not placed item is accepted, and publication checks slots and retirements on the owner only. `bind_requirement` assigns a vehicle checklist to a schedule slot of its own vehicle, or ends that assignment. After the cutover, every legacy writer refuses: `create_template`, `duplicate_template`, `set_template_key`, `save_template_draft` and `publish_template_version` of a legacy template, `save_override_draft`, `publish_override_version`, and `bind_requirement` of a legacy template.

**The migration (SPEC §18.2, issue #220 part 2).** `cya/checklists/cutover.py`; operator command `migrate_vehicle_checklists` (below). `plan_cutover` reads only; `migrate_to_vehicle_checklists` applies the plan as one `CHECKLIST_CUTOVER` command (policy action `import_configuration`, Administrators), all or nothing, at the receipt's instant T:

1. Every (vehicle, stable slot) pair is kept as the vehicle-local position it already is, recorded in `legacy_position_map`. Every vehicle gets its own copy of each organization-wide position key it uses (`vehicle_position_key`). Labels, order, retirement and sealable revisions are untouched.
2. Each (vehicle, legacy template) pair with a binding or a published override becomes one vehicle checklist (`legacy_checklist_map`). Its precedence follows the template identity order within the vehicle, the order the generator used. Its versions are the vehicle's effective checklist (template version with the vehicle's override applied) at T and at each later published boundary, published with that boundary as effective instant, placements mapped to the vehicle's own rows (`legacy_version_map`, `legacy_item_def_map`). Earlier versions stay in the legacy rows and are read through the maps.
3. Each binding chain becomes an assignment chain of that checklist on the same slot: bound at T when the legacy binding is in force then, followed by every later revision at its exact instant. A template on several slots stays one checklist with several assignments; templates sharing a slot stay separate checklists.
4. Before commit, `verify_carry_over` (`cya/obligations/legacy.py`) resolves every due instant from T that already has an obligation, every one in the next 8 days, and the first one after each later published boundary, both as the records stood before T and with the migration's rows, and requires identical requirements across the maps. Any difference, an unresolvable legacy checklist (a stale override, a missing position, an empty result) or an earlier cutover refuses the whole command.

Nothing old changes: templates, overrides, bindings, position keys, obligations, checks, open sessions, kit lists, seals and audit events keep their rows and meaning. After the cutover the planner keeps an existing obligation whose legacy requirement maps unchanged onto the migrated one (`carried_over`), so the migration recomputes nothing. A later real change still replaces it under §5.7. Ending a migrated assignment previews and supersedes the obligations still pinned to its legacy binding too.

**Precedence (CHK-10).** When two checklists governing one due instant require the same item (and kit instance), the generator picks the lowest schedule-slot text (code-point order, as the `utf8mb4_bin` column compares), then the lowest checklist precedence. Legacy templates keep their old order, by `template_id`; the two models never govern the same due instant.

## Freeze rules

A template version or override version is **published exactly when a `checklist_publication` row names it**. MariaDB triggers enforce the freeze, independent of the command layer and of the Django admin or ORM:

- `BEFORE UPDATE` / `BEFORE DELETE` on `checklist_version`, `vehicle_override_version`, `checklist_item_def` and `vehicle_override_item` reject the change when the row's owning version is published (the update trigger also checks the *new* owner, so a definition cannot be moved into a published version).
- `BEFORE INSERT` on `checklist_item_def` and `vehicle_override_item` rejects adding a row to a published version.
- `compartment`, `vehicle_system` and `stocking_deviation` rows are revisions and are never updated or deleted.
- `checklist_publication` is append-only, its insert must match its command receipt (organization, actor, `recorded_at`) and name a version that has content, and `effective_at >= recorded_at` is a CHECK.
- `id` and `organization_id` cannot change on `vehicle`, `item`, `kit_instance` and `checklist_template`.
- A `checklist_template` cannot be deactivated while a vehicle is (or will be) bound to it for obligation generation; unbind it first ([issue #59](https://github.com/JustApu/CheckYourAmbulance-archive/issues/59), [obligation generation](obligation-generation.md)).

Unpublished (draft) rows are not frozen by the database, so a schema-owner session can still repair one; the runtime account holds no `UPDATE` or `DELETE` on V tables (SPEC §17.7 item 1), so application drafts change only by saving a **new draft revision** (CHK-2). The commands take `SELECT … FOR UPDATE` on the template, version, override or vehicle row first, so a publish cannot race an edit of the same version and concurrent drafts receive distinct revisions. Publication also locks the shared parent of its ordering stream (the template for template versions, the vehicle for overrides) and reads the latest published instant with a locking read, so two publishers cannot both pass the strict-ordering check for the same stream. The freeze triggers cannot see an uncommitted publication from another transaction; that lock is what closes the window.

## Commands

All commands run through `run_command`: one transaction, operation-id receipt (a replay returns the original result), success/denial/failure audit with the actor's current roles. Audit evidence targets the organization (the audit target types are fixed by the audit schema) and carries a bounded snapshot. Permissions come from the SPEC §3.3 matrix: `draft_edit_template` and `publish_template` (Supervisor, Administrator) and `approve_stock_deviation` (Supervisor, Administrator).

| Command | Effect |
|---|---|
| `save_template_draft` | Next draft revision of a template with its complete item set. Optional inputs (issue #130 part 6a): an item spec may give `new_item` (a label) in place of `item_id`, and the command creates that item; `base_revision` refuses a save started from an older revision. Each enters the request digest only when given |
| `publish_template_version` | Validates, assigns `effective_at`, writes the publication |
| `save_override_draft` | Next draft override revision for a vehicle over a **published** template version. Optional inputs (issue #130 part 6b), each in the request digest only when given: an ADD entry may give `new_item` (a label) in place of `item_id`, and the command creates the item; `base_revision`, the vehicle's latest override revision across templates, refuses a save started from an older one |
| `publish_override_version` | Same, for an override |
| `record_stocking_deviation` | First revision, or an extension / shortening / termination revision |

Publication validates the full resolved set: the version has items; `effective_at` is not in the past and is later than the template's (or vehicle's) latest published instant; and **every active vehicle has an effective slot row for every placement** at that instant. Kits do not gate publication: a vehicle may carry any number of a kit. `effective_at = None` means "at the publication instant". Time-dependent checks run against the receipt's `recorded_at`, which is assigned after validation starts.

Item kinds (`PRESENCE_CONDITION`, `QUANTITY`, `MEASUREMENT`, `SPECIAL_OBSERVATION`, `VEHICLE_READING`) are validated server-side: unknown fields are rejected; `QUANTITY` needs a nonnegative integer count and no other kind may carry one; `MEASUREMENT` and `VEHICLE_READING` need a unit; exactly one of compartment or system is given and a kit needs a compartment; `bulk_eligible` is held false by a CHECK (deferred, SPEC §7.6).

## Resolution as of an instant

`resolve_checklist(organization_id, vehicle_id, template_id, as_of)`:

1. The governing template version is the published one with the latest `effective_at <= as_of` (ties by revision); the governing override is the vehicle's published override with the latest `effective_at <= as_of`.
2. `apply_overlay` applies `ADD` (item must be absent), `REMOVE` and `REPLACE` (item must be present) — one entry per item, and any inconsistency raises `OverlayError`.
3. Slots resolve per vehicle (below). A definition's `kit_id` is a template reference that must be an active kit placed in the definition's compartment slot; it is never propagated to another vehicle. Resolution lists the vehicle's own active kits in that slot as `ResolvedItem.kits` (`(id, label)` pairs ordered by label): any number, including none; the check records which kit was present (SPEC §4.2). Then the stocking deviation whose tail revision recorded by `as_of` is open at `as_of` (`valid_from <= as_of < valid_until`) replaces the quantity of a `QUANTITY` item (SPEC §4.5). Outside the window the normal quantity applies. Revisions recorded after `as_of` are not seen, so an as-of query is reproducible.

Resolution **fails closed**: no effective version, an override that overlays a template version other than the governing one (`StaleOverrideError`), or a missing slot (`MissingSlotError`) raises instead of dropping or inventing a requirement. After publishing a future-dated template version, `stale_override_vehicle_ids` lists vehicles whose override must be republished (against the new version, with the same effective instant) before that instant.

## Where the schema completes or differs from the contract text

Reversible choices made so the contract rows can coexist; none changes SPEC behavior.

1. **Publication targets.** The contract gives `checklist_publication` a unique `checklist_version_id` and an optional `override_version_id`, and gives `vehicle_override_version` a `publication` reference. Several vehicles each need their own override publication over one template version, and the two references would be circular. Built: `checklist_version_id` and `override_version_id` are each nullable and unique, a CHECK requires exactly one, and an override's publication is found through the publication row (as for template versions). `vehicle_override_version` has no `publication` column.
2. **Override replacement definitions.** `checklist_item_def.checklist_version_id` is nullable beside a nullable `override_version_id` (a CHECK requires exactly one), because a replacement definition cannot share the template version's `(version, item)` key. Composite keys make an override entry's definition belong to the same override and name the same item.
3. **Slots across vehicles** (legacy templates; a vehicle checklist references its own vehicle's rows, see above). A central template cannot reference per-vehicle rows directly. A definition references one `compartment` / `vehicle_system` row; the slot is its `stable_slot_id`, and a vehicle's own placement is that vehicle's latest effective row with the same `stable_slot_id`. Vehicles share slot ids for the same physical position. The seed references the first ambulance's rows.
4. **Deviation revisions.** One chain per vehicle and item (a unique `prior_revision_id` stops forks; a trigger checks the chain). `valid_from` may be omitted to start at the recording instant; an active deviation keeps its start. Termination is a revision ending at the termination instant. A crew member's early-termination *request* (SPEC §4.5) is not built; it belongs with the request workflow.
5. **`NOT_APPLICABLE`.** Not accepted in `allowed_outcomes` until the administrator-defined `na_rule` exists (SPEC §7.1). `na_rule_id` and `asset_id` are omitted until their tables exist (`equipment_asset` is R3).
6. **Vehicle to template.** The checklist-specific rows have no vehicle-to-template link; the
   `requirement_binding` row and rationale are recorded under obligations (issue #168).

## Not in this issue

- `item_lock` rows ("create missing item lock rows before any check can use it") belong with the check-recording issue that adds that table; publication here does not create them.
- `requirement_version`, obligation generation and the SPEC §5.7 reconciliation of pre-created obligations on publish: done in [issue #59](https://github.com/JustApu/CheckYourAmbulance-archive/issues/59), see [obligation generation](obligation-generation.md). Schedule and conditional rules are [issue #56](https://github.com/JustApu/CheckYourAmbulance-archive/issues/56) and [issue #58](https://github.com/JustApu/CheckYourAmbulance-archive/issues/58); N/A rules remain open.
- HTTP endpoints, UI, the Django admin (must not expose these tables for editing), the deviation-expiry alert (SPEC §4.5) and the early-termination request.
- Moving a kit between vehicles (R2). `kit_instance` assignment is a slot reference only.

## Operator notes

Migrations need the schema-owner account and, on a binary-log-enabled server, `log_bin_trust_function_creators=1` (see [CONTRIBUTING.md](../CONTRIBUTING.md#python-and-django)). After migrating, re-run `manage.py audit_runtime_grants` for the runtime account: it now also grants the checklist V and M tables (`SELECT, INSERT`, and `UPDATE` on M tables only).

Moving an organization to vehicle-owned checklists (SPEC 5.0.0 §18.2), once the per-vehicle checklist screens (issue #220 part 3) are deployed, as an active Administrator:

```sh
local/venv/bin/python manage.py migrate_vehicle_checklists --organization <organization id> --actor-user-id <administrator user id> --dry-run
local/venv/bin/python manage.py migrate_vehicle_checklists --organization <organization id> --actor-user-id <administrator user id>
```

`--dry-run` reads only and prints what would be created and every problem that would refuse the run; the equivalence check needs the writes and runs only for real. A real run prints its operation id; pass it as `--operation-id` to retry an interrupted run, which then replays the first result. Any other run after a successful one is refused. A refused run writes nothing.

Seeding a development or demonstration database with the synthetic fleet (never operational checklist data; the data is labeled non-authoritative, SPEC CHK-7):

```sh
local/venv/bin/python manage.py seed_synthetic_fleet --actor-user-id <administrator user id> --dry-run
local/venv/bin/python manage.py seed_synthetic_fleet --actor-user-id <administrator user id>
```

It needs exactly one active organization and an active Administrator or Supervisor. It is idempotent: fleet rows have deterministic ids, and the checklist is drafted and published only when no publication of the seed template exists. `--dry-run` reads and writes nothing. If the draft is saved but publication fails, the next run saves a further draft revision and publishes that one; the earlier draft stays as an unpublished revision.
