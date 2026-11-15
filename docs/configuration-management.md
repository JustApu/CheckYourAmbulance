# Configuration management design

[Issue #130](https://github.com/JustApu/CheckYourAmbulance/issues/130) part 1. This page is the schema and service design that the issue requires before any management screen is built. It implements [SPEC 2.2.0 §4.8](../SPEC.md) (CFG-1 to CFG-6, merged in PR #151) with the §3.3 rows of that amendment. It reuses the kit definitions (§4.6) and vehicle files (§4.7, [vehicle files](vehicle-files.md)) of issue #129. SPEC and the [contracts](data-model-contracts.md) win if they disagree with this page. Nothing here is implemented yet; later parts update this page as they land.

## Principles

- **Commands, not edits.** Every change is one audited command through `cya.core.audit_commands.run_command` (AUD-1). Screens and vehicle files call the same commands and validation, so the importer and the UI never diverge (issue #130, "Scope/sequence").
- **History is appended.** Versioned records get new revisions or drafts; nothing published is edited, and nothing is deleted.
- **Locks.** The vehicle row is the shared lock for a vehicle's configuration. Kits, kit lists, overrides, bindings, compartment designations and the importer already lock it first; position revisions and retirement do too.
  - A publication or binding that must check a vehicle's positions (CFG-2) first takes its own locks: the version and template, or the override, or the kit list. Then, before the check, it locks every vehicle it applies to, in id order. That includes inactive vehicles and those whose binding takes effect later.
  - Retirement locks only the vehicle and never a template, so the order (organization when needed, then the versioned record, then vehicles) has no cycle.
  - Whichever commits first, the other then reads the committed state (READ COMMITTED) and refuses. Part 3 tests both orders for template publication, override publication, kit-list publication and binding.
- **Preview, then confirm.** Consequential changes show a preview with a base token, and confirmation recomputes the plan under the locks and refuses a stale token (CFG-6). This is the pattern of the vehicle-file import.

## Permissions

New policy actions, each Supervisor and Administrator (SPEC 2.2.0 §3.3), with permission-contract rows PERM-46 to PERM-48:

| Action | Covers |
|---|---|
| `manage_vehicles` | Create a vehicle, edit its name and number, deactivate and reactivate it (CFG-1) |
| `manage_vehicle_positions` | Add, rename, reorder and retire compartments and systems (CFG-2) |
| `duplicate_configuration` | Duplicate a checklist template or a vehicle (CFG-3, CFG-4) |

Existing actions cover the rest:
- `draft_edit_template` and `publish_template`: templates;
- `manage_kit_definitions`: kit definitions;
- `manage_kit_assignment`: kit instances, including CFG-5;
- `manage_vehicle_kit_lists`: kit lists;
- `set_configuration_keys`: VINs and keys (Administrator only).

The Administration menu entry is Administrator-only today. Part 5 makes the configuration screens reachable by Supervisors too, without showing them the Administrator-only role and import screens.

## Vehicle lifecycle (CFG-1)

`vehicle.active` is a current-state flag with no history, but CFG-1 needs the instant a vehicle stopped or resumed owing obligations.

- **New table `vehicle_activity_event`** (append-only, kind A): `vehicle_id`, `sequence` (gapless per vehicle), `active`, `recorded_at` and the usual command attribution. The insert guard accepts only `VEHICLE_CREATE`, `VEHICLE_ACTIVE_SET` and `VEHICLE_DUPLICATE` receipts. **Existing vehicles** have no events.
  - A deploy-time audited command, `record_vehicle_activity_baseline`, appends one baseline event for each one. The event records the vehicle's current flag, marked as a baseline, at the command's recorded instant.
  - A baseline says only "known active, or inactive, from this instant". It never claims when an inactive vehicle was deactivated. Evaluation before the baseline instant is unchanged from today, with no inactivity exemption, and generation still skips a vehicle whose flag is false.
  - So an existing inactive vehicle reactivated later is exempt between its baseline and its reactivation, and nothing is backfilled.
  - `set_vehicle_active` refuses a vehicle that has no baseline yet, so the baseline can never come after a change.
- **Commands** (`manage_vehicles`):
  - `create_vehicle`: VIN, name and optional number. Writes the vehicle and its first event, active. The creator enters the VIN, which is then set once (SPEC 3.0.0 IMP-6, PR #155, owner-approved major amendment).
  - `update_vehicle`: name and number.
  - `set_vehicle_active`: appends an event, updates `vehicle.active` in the same command, then reconciles the vehicle after commit.
- **Obligations.** This mirrors the out-of-service exemption (§5.9), which is derived from the status stream and never retires or backfills:
  - Generation already skips inactive vehicles (`cya/obligations/generation.py`).
  - `is_exempt` in `cya/obligations/requirements.py` also treats a vehicle as exempt when it is inactive at the reference instant, so no new routine unit is selected while it is inactive.
  - State evaluation (`cya/obligations/state.py`) shows a routine obligation whose due instant falls in an inactive interval as `EXEMPT`, naming the activity event. Its earlier history is untouched.
  - Reactivation resumes from the event instant; due instants in the inactive interval stay exempt, so nothing is backfilled.
  - Ad hoc inspection requests for an inactive vehicle are refused.
- **Projection.** The current-obligation projection facts gain the activity events, so its digest changes and existing projection rows become stale. Deploying this part follows the runbook's [rebuild and comparison](deployment-runbook.md#obligation-projection-rebuild-and-comparison) with traffic closed:
  1. `rebuild_obligation_projection --organization <uuid> --dry-run`. Differences are expected.
  2. The same command without a flag. This is the write-capable rebuild. Each vehicle and due-instant group commits on its own, so a failed group rolls back and a rerun is safe.
  3. `--verify`, which must report `Different: 0`.

  Traffic reopens only after step 3 passes. If it does not, rerun step 2. If step 2 keeps failing, roll back to the previous release, whose projection rows the old digest still accepts.
- **Coordination.** Issue #131 (seal substitution, Codex) also changes obligation state and projection replay. This part is sequenced after, or reconciled with, those changes, and the precedence between `EXEMPT` (inactive or out of service), waivers and seal substitution is agreed with Codex before code.

## Compartments and systems (CFG-2)

- **Schema.** Each `compartment` and `vehicle_system` revision gains `display_order` (a positive integer, or empty) and `retired` (boolean, default false). Revision rows cannot be updated (the `cya_compartment_immutable_bu` and `cya_vehicle_system_immutable_bu` triggers), so existing rows keep an empty order and are never rewritten. Positions sort by order, with empty last, then by label, which is today's label order. The first reorder of a vehicle writes explicit orders as new revisions.
- **Migration.** The migration is `cya_checklists` 0007, after PR #150's 0005 and issue #131's 0006 (the merge order agreed with the owner).
- **Commands** (`manage_vehicle_positions`). Each runs under the vehicle lock:
  - `add_position`: either this vehicle's row in an existing position, named by its position key (IMP-6), or a new position. For a new position, the creator enters its key, which is then set once (SPEC 3.0.0 IMP-6, PR #155, owner-approved major amendment). The `position_key` insert guard then also accepts `POSITION_ADD` receipts. `set_position_key` stays the Administrator-only way to key an older keyless position.
  - `revise_position`: any of label, order and retired. It appends a revision effective when recorded and copies the compartment's `sealable` from the newest revision (CAB-1).
  - Like a designation, a revision is refused while a later revision of the slot is already scheduled.
- **Retiring is refused** while any of these still use the slot:
  - a version, in force or published for a later instant, of any template bound to the vehicle places an item there. This includes a template bound by a binding recorded to take effect later, and the vehicle's overrides;
  - the kit list in force or a published future kit list names it;
  - an active kit instance is assigned to it.

  The refusal lists each dependency. A retired slot stays readable for history. It is left out of pickers and new placements, and an un-retire is one more revision.
- **Writers refuse a retired position.** A position whose newest revision is retired cannot receive:
  - a kit instance (`add_kit_instance` and `record_kit_assignment`, through `_placement` in `cya/kits/commands.py`);
  - a kit-list line (`cya/kits/kit_lists.py`);
  - a placement in a template or override draft (`cya/checklists/validation.py`).
- **Publishing and binding refuse a retired position** (CFG-2), listing the vehicles and positions. Today such a placement only fails later, at resolution (`MissingSlotError` in `cya/checklists/overlay.py`), so these checks keep a retirement from being stranded from the other direction:
  - publishing a template version, override or kit list that would place something in a position retired on a vehicle it applies to;
  - binding a template to a vehicle when a version of it, in force or published for later, uses a position retired there. This covers `bind_requirement` (which accepts a future `effective_at`), the vehicle-file import's binding and the bindings a vehicle duplicate copies.
- **Readers.**
  - Resolution is unchanged. A retired position's rows still resolve by stable slot and effective instant, so history and in-window obligations keep their meaning, and the checks above keep new requirements away from it.
  - Vehicle files export positions by `display_order`, then label, and leave out retired positions. An import that names a retired position is refused with that reason.
  - The paper checklist and crew screens already follow placements, so they need no change.

## Duplicating a checklist template (CFG-3)

`duplicate_template` (`duplicate_configuration`, command `TEMPLATE_DUPLICATE`):
- Creates a template with the label and key its creator gives (SPEC 3.0.0).
- Creates one draft version copying the chosen version's item definitions, placements (including kit placements, KDEF-4) and settings, through the same validation as `save_template_draft`.
- Writes no publication, override, binding or obligation, and leaves the source unchanged.

## Duplicating a vehicle (CFG-4)

`duplicate_vehicle` (`duplicate_configuration`, command `VEHICLE_DUPLICATE`), with a preview. Locks: the organization (for the new identity), then the source vehicle row.

| Created for the copy | From the source |
|---|---|
| The vehicle, with the VIN, name and number its creator gives (SPEC 3.0.0), inactive, and its first activity event (inactive) | — |
| One revision-1 row per position, with a new row id and the **same `stable_slot_id`** as the source position (so copied placements, kit-list lines and bindings resolve on the copy), effective when recorded | Each position's label, order and sealable designation in effect now; retired positions are left out |
| One draft kit list | The lines in force, mapped to the copy's compartment rows |
| One draft override per bound template, over the same template version | Each override in force (none when there is none) |
| One binding per bound template and slot, effective when recorded | The source's bindings in force |
| Item locks for the bound templates' items | — |

- **Not copied:** the VIN, kit instances and equipment, checks, obligations, deficiencies, seals, operational status, odometer and engine hours, and every history.
- **Bindings:** the copy's bindings owe nothing while it is inactive (CFG-1).
- **Guard change:** the binding insert guard must accept `VEHICLE_DUPLICATE`, as it accepts `VEHICLE_IMPORT` (`cya_kits` 0012).
- **Preview:** it lists the copied rows and the exclusions above (CFG-6).

## Duplicating a kit instance (CFG-5)

The screen calls `add_kit_instance` with the source instance's definition, a new label and the chosen vehicle and compartment. No new command is needed.

## Parts

Each part is one PR, in order. Every part uses `Refs #130`; the last uses `Closes #130`.

1. This design page.
2. Vehicle lifecycle: activity events, commands, obligation exemption and projection, after agreement with issue #131.
3. Positions: the schema, the commands and the retire checks, plus the vehicle-file changes for order and retirement.
4. Duplication: the template and vehicle commands with their previews.
5. Fleet screens: vehicles (list, search, create, edit, deactivate), positions, kit lists and kit instances, and Supervisor access to the configuration section.
6. Checklist screens: templates (create, duplicate, draft edit, publish with an effective date, current, draft and future versions), overrides, and the impact preview of affected vehicles.
7. Kit definition screens: list, edit, duplicate, publish and deactivate.

The issue's acceptance criteria for responsive and accessible pages (UX issue #111), and its role, duplication, versioning, history and concurrency tests, apply to each part that adds the behaviour.
