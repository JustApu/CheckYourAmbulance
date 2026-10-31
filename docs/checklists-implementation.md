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

Table kinds match `tools/audit_enforcement.py`: `vehicle`, `kit_instance`, `checklist_template`, `item` are mutable (M); `compartment`, `vehicle_system`, `checklist_version`, `checklist_item_def`, `vehicle_override_version`, `vehicle_override_item`, `stocking_deviation` are versioned (V); `checklist_publication` is append-only (A, with the standard `append-only table` triggers the schema inspector requires).

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
| `save_template_draft` | Next draft revision of a template with its complete item set |
| `publish_template_version` | Validates, assigns `effective_at`, writes the publication |
| `save_override_draft` | Next draft override revision for a vehicle over a **published** template version |
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
3. **Slots across vehicles.** A central template cannot reference per-vehicle rows directly. A definition references one `compartment` / `vehicle_system` row; the slot is its `stable_slot_id`, and a vehicle's own placement is that vehicle's latest effective row with the same `stable_slot_id`. Vehicles share slot ids for the same physical position. The seed references the first ambulance's rows.
4. **Deviation revisions.** One chain per vehicle and item (a unique `prior_revision_id` stops forks; a trigger checks the chain). `valid_from` may be omitted to start at the recording instant; an active deviation keeps its start. Termination is a revision ending at the termination instant. A crew member's early-termination *request* (SPEC §4.5) is not built; it belongs with the request workflow.
5. **`NOT_APPLICABLE`.** Not accepted in `allowed_outcomes` until the administrator-defined `na_rule` exists (SPEC §7.1). `na_rule_id` and `asset_id` are omitted until their tables exist (`equipment_asset` is R3).
6. **Vehicle to template.** The contract has no vehicle-to-template link; callers pass the template id.

## Not in this issue

- `item_lock` rows ("create missing item lock rows before any check can use it") belong with the check-recording issue that adds that table; publication here does not create them.
- `requirement_version`, obligation generation and the SPEC §5.7 reconciliation of pre-created obligations on publish: done in [issue #59](https://github.com/JustApu/CheckYourAmbulance-archive/issues/59), see [obligation generation](obligation-generation.md). Schedule and conditional rules are [issue #56](https://github.com/JustApu/CheckYourAmbulance-archive/issues/56) and [issue #58](https://github.com/JustApu/CheckYourAmbulance-archive/issues/58); N/A rules remain open.
- HTTP endpoints, UI, the Django admin (must not expose these tables for editing), the deviation-expiry alert (SPEC §4.5) and the early-termination request.
- Moving a kit between vehicles (R2). `kit_instance` assignment is a slot reference only.

## Operator notes

Migrations need the schema-owner account and, on a binary-log-enabled server, `log_bin_trust_function_creators=1` (see [CONTRIBUTING.md](../CONTRIBUTING.md#python-and-django)). After migrating, re-run `manage.py audit_runtime_grants` for the runtime account: it now also grants the checklist V and M tables (`SELECT, INSERT`, and `UPDATE` on M tables only).

Seeding a development or demonstration database with the synthetic fleet (never operational checklist data; the data is labeled non-authoritative, SPEC CHK-7):

```sh
local/venv/bin/python manage.py seed_synthetic_fleet --actor-user-id <administrator user id> --dry-run
local/venv/bin/python manage.py seed_synthetic_fleet --actor-user-id <administrator user id>
```

It needs exactly one active organization and an active Administrator or Supervisor. It is idempotent: fleet rows have deterministic ids, and the checklist is drafted and published only when no publication of the seed template exists. `--dry-run` reads and writes nothing. If the draft is saved but publication fails, the next run saves a further draft revision and publishes that one; the earlier draft stays as an unpublished revision.
