# Configuration management design

[Issue #130](https://github.com/JustApu/CheckYourAmbulance/issues/130) part 1. This page is the schema and service design that the issue requires before any management screen is built. It implements [SPEC 2.2.0 §4.8](../SPEC.md) (CFG-1 to CFG-6, merged in PR #151) with the §3.3 rows of that amendment. It reuses the kit definitions (§4.6) and vehicle files (§4.7, [vehicle files](vehicle-files.md)) of issue #129. SPEC and the [contracts](data-model-contracts.md) win if they disagree with this page. Part 3 (positions) is implemented as described below; the other parts are design, and this page is updated as they land.

## Principles

- **Commands, not edits.** Every change is one audited command through `cya.core.audit_commands.run_command` (AUD-1). Screens and vehicle files call the same commands and validation, so the importer and the UI never diverge (issue #130, "Scope/sequence").
- **History is appended.** Versioned records get new revisions or drafts; nothing published is edited, and nothing is deleted.
- **Locks.** The vehicle row is the shared lock for a vehicle's configuration. Every command takes locks in this one order, skipping the levels it does not need:
  1. the organization row, only when a rule spans the organization (keys, imports);
  2. the one versioned record being published, when there is one: the checklist version row, the override row, or the kit-list version (read without locking, then validated under the vehicle lock);
  3. the affected vehicle rows, in id order;
  4. kit instance rows (kit moves, seal commands; designating a kit sealable takes only this level);
  5. checklist templates;
  6. kit definitions;
  7. item and deficiency lock rows.
  - **Where today's writers fit:**
    - Binding (vehicle, then template), kit commands, kit lists, override publication (override, then vehicle), compartment designations, the importer and the seal commands (vehicle, then kits, templates and definitions) already follow this order.
    - Part 3 changes template publication from version-then-template to version, then the vehicles bound to the template (including inactive ones and later-effective bindings), then template.
    - Position revisions and retirement take only level 3; `add_position` takes levels 1 and 3.
  - **Retirement against publication and binding:** both lock the same vehicle rows before checking positions (CFG-2). Whichever commits first, the other then reads the committed state (READ COMMITTED) and refuses. Part 3 tests both orders for template publication and binding, with distinct actors, and shows that each test fails without its vehicle lock.
  - **Binding against template publication** ([issue #164](https://github.com/JustApu/CheckYourAmbulance/issues/164)): publication reads its vehicles before taking any lock, so a binding can commit in between. After locking the template, publication reads the bound vehicles again. If a vehicle was added, it refuses with "publish again" instead of locking that vehicle out of order. Binding first, then publication: the publication refuses. Publication first: the binding waits on the template, then sees the published version.
- **Preview, then confirm.** Consequential changes show a preview with a base token, and confirmation recomputes the plan under the locks and refuses a stale token (CFG-6). This is the pattern of the vehicle-file import.

## Permissions

New policy actions, each Supervisor and Administrator (SPEC 2.2.0 §3.3), with permission-contract rows PERM-46 to PERM-48:

| Action | Covers |
|---|---|
| `manage_vehicles` | Create a vehicle, edit its name and number, deactivate and reactivate it (CFG-1) |
| `manage_vehicle_positions` | Add, rename, reorder and retire compartments and systems (CFG-2) |
| `duplicate_configuration` | Duplicate a checklist template or a vehicle (CFG-3, CFG-4) |

SPEC 4.1.0 adds `bind_unbind_template` (PERM-49): bind or unbind a checklist template to a vehicle (CFG-7, CFG-8), also Supervisor and Administrator.

Existing actions cover the rest:
- `draft_edit_template` and `publish_template`: templates;
- `manage_kit_definitions`: kit definitions;
- `manage_kit_assignment`: kit instances, including CFG-5;
- `set_configuration_keys`: VINs and keys (Administrator only).

Part 5b opens the Administration section to Supervisors, and part 5c to Supply Officers, who may manage kit instances (`manage_kit_assignment`). The landing page lists only the pages the user may open, and each page checks its own action on the server: role administration stays Administrator-only, and import stays with `import_configuration`. On a vehicle's kit page, when there is no active kit type or the vehicle has no compartment, the add form is replaced by a note saying what is missing and that a Supervisor or Administrator adds it, linking to Kit definitions or the vehicle's Compartments and systems page for someone who may open it (issue #235 E1).

## Vehicle lifecycle (CFG-1)

`vehicle.active` is a current-state flag with no history, but CFG-1 needs the instant a vehicle stopped or resumed owing obligations.

- **New table `vehicle_activity_event`** (append-only, kind A): `vehicle_id`, `sequence` (gapless per vehicle), `active`, `basis` (`CHANGE` or `BASELINE`), `recorded_at` and the usual command attribution. The insert guard accepts:
  - `basis = CHANGE` only under a `VEHICLE_CREATE`, `VEHICLE_IMPORT` (a vehicle created by a vehicle file), `VEHICLE_ACTIVE_SET` or `VEHICLE_DUPLICATE` receipt;
  - `basis = BASELINE` only under a `VEHICLE_ACTIVITY_BASELINE` receipt, only as sequence 1 of a vehicle that has no events, and only with `active` equal to the vehicle's current flag.
  - A deploy-time management command, `record_vehicle_activity_baseline`, runs one audited `VEHICLE_ACTIVITY_BASELINE` command. It is attributed to a service identity, as scheduled obligation generation is, with no user actor. Under the organization lock and each vehicle's lock, it appends a `BASELINE` event for every vehicle without events, recording its current flag at the command's recorded instant.
  - Rerunning it adds nothing for vehicles already baselined, and a replay of the same operation returns the original result.
  - A baseline says only "known active, or inactive, from this instant". It never claims when an inactive vehicle was deactivated. Evaluation before the baseline instant is unchanged from today, with no inactivity exemption, and generation still skips a vehicle whose flag is false.
  - So an existing inactive vehicle reactivated later is exempt between its baseline and its reactivation, and nothing is backfilled.
  - `set_vehicle_active` refuses a vehicle that has no baseline yet, so the baseline can never come after a change.
- **Commands** (`manage_vehicles`):
  - `create_vehicle`: VIN, name and optional number. Writes the vehicle and its first event, active. The creator enters the VIN, which is then set once (SPEC 4.0.0 IMP-6, merged in PR #155).
  - `update_vehicle`: name and number.
  - `set_vehicle_active`: appends an event, updates `vehicle.active` in the same command, then reconciles the vehicle after commit.
- **Obligations.** This mirrors the out-of-service exemption (§5.9), which is derived from the status stream and never retires or backfills:
  - Generation already skips inactive vehicles (`cya/obligations/generation.py`).
  - `is_exempt` in `cya/obligations/requirements.py` also treats a vehicle as exempt when it is inactive at the reference instant, so no new routine unit is selected while it is inactive.
  - State evaluation (`cya/obligations/state.py`) shows a routine obligation whose due instant falls in an inactive interval as `EXEMPT`, naming the activity event. Its earlier history is untouched.
  - Reactivation resumes from the event instant; due instants in the inactive interval stay exempt, so nothing is backfilled.
  - Ad hoc inspection requests for an inactive vehicle are refused.
- **Projection.** Activity is attached to the current-obligation projection at read time, exactly as the operational status already is (`status_timeline` and `attach_status_fact` in `cya/obligations/current.py`). Stored projection facts and their digest do not change, so deploying this part needs no projection rebuild. The rebuild command's `--verify` replays activity too, and still reports `Different: 0` after deployment.
- **Coordination.** Issue #131 (seal substitution, Codex), merged in PR #154, also changes obligation state and projection replay. This part builds on it. As agreed with Codex, the inactive-vehicle `EXEMPT` takes the same precedence as the out-of-service one, ahead of waivers, checks, seal substitution and suspensions.

## Compartments and systems (CFG-2)

- **Schema.** Each `compartment` and `vehicle_system` revision gains `display_order` (a positive integer, or empty) and `retired` (boolean, default false). Revision rows cannot be updated (the `cya_compartment_immutable_bu` and `cya_vehicle_system_immutable_bu` triggers), so existing rows keep an empty order and are never rewritten. Positions sort by order, with empty last, then by label, which is today's label order. The first reorder of a vehicle writes explicit orders as new revisions.
- **Migration.** The migration is `cya_checklists` 0007, after PR #150's 0005 and issue #131's 0006 (the merge order agreed with the owner).
- **Commands** (`manage_vehicle_positions`). Each runs under the vehicle lock:
  - `add_position`: either this vehicle's row in an existing position, named by its position key (IMP-6), or a new position. For a new position, the creator enters its key, which is then set once (SPEC 4.0.0 IMP-6, merged in PR #155). The `position_key` insert guard then also accepts `POSITION_ADD` receipts. `set_position_key` stays the Administrator-only way to key an older keyless position.
  - `revise_position`: any of label, order and retired. It appends a revision effective when recorded and copies the compartment's `sealable` from the newest revision (CAB-1).
  - Like a designation, a revision is refused while a later revision of the slot is already scheduled.
- **Retiring is refused** while any of these still use the slot:
  - a version, in force or published for a later instant, of any template bound to the vehicle places an item there. This includes a template bound by a binding recorded to take effect later, and the vehicle's overrides;
  - the kit list in force or a published future kit list names it;
  - an item requirement version (SPEC 6.0.0 CHK-11) keeps an item there: the latest version of each of the vehicle's requirements effective by now, or any version published for a later instant. A requirement blocks even when no checklist inspects it any more; move it first, by publishing a version at another place or Not placed (issue #256 C4);
  - an active kit instance is assigned to it.

  The refusal lists each dependency. A retired slot stays readable for history. It is left out of pickers and new placements, and an un-retire is one more revision.
- **Designations and seals** (added once issue #131 merged):
  - A sealable designation (`cya/kits/sealable.py`) copies the position's order and retirement into its new revision. Making a retired compartment sealable is refused.
  - Seal commands refuse a retired cabinet, and the seal subject list leaves it out. Breaking a seal still left on it stays possible.
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

## Checklist templates (part 6a)

> **SPEC 5.0.0 ([issue #220](https://github.com/JustApu/CheckYourAmbulance/issues/220)).** Checklists now belong to each vehicle. The template screens below describe the legacy shared templates, which stay usable until the organization's cutover and are read-only after it; the commands refuse every legacy write then. `create_vehicle_checklist` creates a vehicle's own checklist. See [vehicle-owned checklists](checklists-implementation.md#vehicle-owned-checklists-spec-500-issue-220).
>
> After the cutover (issue #220 part 3):
> - **Checklists page** (`/administration/checklists/`) lists each vehicle's checklists and creates a new one for a chosen vehicle, with no key. The checklist page and item editor are the template screens: the editor offers only the owning vehicle's positions and kits, plus **Not placed** (CHK-9), and the publish preview checks only that vehicle. Override and duplicate links are not shown; a legacy template's page says it is read-only history.
> - **Checklist assignments** (the vehicle's former bindings page) lists and offers only the vehicle's own checklists, with "Assign" and "End assignment" (CFG-7, CFG-8). The commands and the end-assignment preview are unchanged.
> - **Positions** (CFG-2, IMP-6): adding a position always creates a new stable slot, and its key is unique within the vehicle only (`vehicle_position_key`). An Administrator sets a missing key per vehicle with `set_vehicle_position_key`, from a Set key form in the Key column of the vehicle's Compartments and systems page (issue #238 F1c; the form is shown only to someone with `set_configuration_keys`, and the command checks it again). The organization-wide `set_position_key` refuses. The page explains each kind with an example, says what a key is for, and calls the form "Add a compartment or system" (F1, F1a, F1b).
> - **Duplicating a vehicle** (CFG-4) copies the source's own checklists as drafts of the version in force (or the first scheduled one), with their precedence, the assignments in force, and the position keys of copied positions. Copied positions keep their stable slots, which are local to each vehicle. An item placed in a retired position is copied as Not placed. Overrides and legacy bindings are no longer copied.
>   - **Item requirements (SPEC 6.0.0 CFG-4, issue #256 C4).** After the organization's item-requirement cutover, the copy also gets each of the source's item requirements: a new requirement (version-5 id) whose one version takes effect at the copy's creation, with the values of the source's version in force then (else its first scheduled one) and its place mapped by stable slot to the copy's own position. The receipt is the duplication's `VEHICLE_DUPLICATE` one, which the requirement insert guards accept from `cya_checklists` 0016. A requirement in a retired position is copied Not placed; if the item already has a Not placed requirement there (18.1 item 8), only one is kept and the preview lists the other as left out. Each copied checklist row outside a kit takes the place and values of its requirement's copy, not the values its source row recorded, so publishing a copied draft links to the copied requirement (rule (a)) and never reverts a requirement edited since. The result adds `item_requirements` (the number copied).

The screens are in `cya/operations/template_admin_views.py`, behind `draft_edit_template`. Publishing needs `publish_template`, and duplicating needs `duplicate_configuration`. Each command checks its own action.

- **Creating a template.** `create_template` (`cya/operations/templates.py`, command `TEMPLATE_CREATE`) writes an active template with the label and key its creator enters (SPEC 4.0.0 IMP-6), and no version. The template key is unique across the organization, so the command takes the organization lock first. The first version is a draft saved like every other.
- **Editing one item at a time.** Each add, change or removal saves a whole new draft through `save_template_draft`, built from the template's latest version (a draft, or the latest published version), with that version's kit placements (KDEF-4) unchanged. A form holding every item would exceed Django's form field limit (`DATA_UPLOAD_MAX_NUMBER_FIELDS`) for a checklist of about 150 items. The editor offers one place per compartment or system slot, with its position key. Retired positions are left out. Resolution maps the chosen row to each vehicle's own row by stable slot.
- **New items by name** (owner decision 2026-11-15). An item spec may name a new item by label instead of an `item_id`. `save_template_draft` creates it active in the same command, as a vehicle-file import creates an item by label (IMP-7). A label that an active item already has is refused, so that item is chosen instead. Active labels stay unique only when created this way, so a save that creates items takes the organization lock first.
- **Stale edits.** The editor sends `base_revision`, the template's latest revision when the page was shown. The command refuses the save if another version was saved since, so a concurrent change is never silently replaced.
- **Impact preview** (`publication_impact`, read only). Before the confirmation, the page shows:
  - the items added, removed or changed against the version in force at the chosen instant;
  - the vehicles the version applies to, bound now or by a later binding, active or not;
  - for each such vehicle with an override in force, that the override overlays an earlier version. Resolution fails closed for that vehicle (`StaleOverrideError`) until an override over the new version is published (part 6b);
  - whatever would make the command refuse: a retired position on a bound vehicle, a missing slot on any active vehicle, an instant in the past or not after the latest publication, an inactive or unpublished placed kit definition.

  The confirmation runs `publish_template_version`, which checks all of it again under its locks. The preview has no base token: a publication changes nothing that it shows, and every refusal is re-checked.

## Vehicle overrides (part 6b)

The screens are in `cya/operations/override_admin_views.py`, one page per template and vehicle, behind `draft_edit_template`; publishing needs `publish_template`.

- **What an override overlays.** A draft is saved over the template's latest published version (by effective instant). The page shows the vehicle's resulting checklist, marking each row as from the template, added, changed or removed.
- **One entry at a time.** Remove a template item, change one (6a's item form, prefilled with the template's definition), add one (an existing item or a new one by name), or undo an entry. Each saves a whole new draft through `save_override_draft`, from the vehicle's latest override of the template, with `base_revision`. Override revisions count per vehicle across templates, so a concurrent change to any of the vehicle's overrides refuses the save.
- **Stale overrides.** An override over an older version stops the vehicle's checklist resolving once a newer version governs (`StaleOverrideError`; 6a's preview warns about it). The page offers to carry the entries over to the latest version. It first lists what cannot be carried: a REMOVE or REPLACE of an item the version no longer has, or an ADD of an item it now has. Confirmation saves a new draft with the rest.
- **Publishing** is now or at a station-local date and time, never before the overlaid version's effective instant (the command refuses it). It shows a short preview first. By owner decision (2026-11-15), it is offered only for a vehicle bound to the template now or by a binding recorded for later; a draft may be prepared for any vehicle. Binding has its own screen (below).
- **The template page** lists vehicles for overrides, bound ones first, at most 500.

## Checklist bindings (CFG-7, CFG-8)

[Issue #177](https://github.com/JustApu/CheckYourAmbulance/issues/177), SPEC 4.1.0. The screen is `cya/operations/binding_admin_views.py`, one page per vehicle, linked from the vehicle page, behind `bind_unbind_template`. `bind_requirement` checks the same action, so a direct post by Crew or a Supply Officer is refused and audited.

- **The page** lists each (template, schedule slot) binding of the vehicle with its latest revision: bound since, starts, unbound since or ends, with the instant.
- **Binding** takes a template, a published slot and an effective time: now, or a station-local date and time (as in 6a). The command's refusals are unchanged: already bound, no published slot, a retired position, a time in the past or not after the previous revision.
- **Inactive vehicles** (CFG-7). A binding may be recorded for an inactive vehicle. Reconciliation runs only for active vehicles, and obligations due while inactive are exempt (CFG-1). So nothing is owed until activation, and then only from the activation, or from the binding's start if it is later.
- **Unbinding** is a preview, then a confirmation (CFG-8):
  - `cya/obligations/unbinding.py` lists the binding's selected routine obligations due at or after the instant;
  - those whose early window has not opened will be superseded. This matches the planner's freeze rule, so the reconciliation that follows the command retires exactly these with a `SUPERSEDED` determination. No check can have satisfied them;
  - those whose window has opened continue, shown with their state;
  - the page states that unbinding completes nothing, and that checks, deficiencies and audit history are kept.
- **Confirmation.** The confirmation posts the preview's digest, which covers the binding revision, both lists and each continuing obligation's state. With `preview_token`, `bind_requirement` unbinds at the instant it records and recomputes the preview then. If anything differs, it refuses with `PreviewChanged`, and the page shows the fresh preview. The token is part of the request digest, so a resubmitted form replays.
- **Limits.** The preview is limited to `MAX_PREVIEW_OBLIGATIONS` (2,000) obligations. Generation stops at its horizon, so a real binding is far below that; past the limit, the page refuses rather than show a partial list. An unbinding at a later instant stays a command-line operation, and so does unbinding a binding that has not started yet: unbinding now would precede its start, which the command refuses, so the page does not offer it.

## Duplicating a checklist template (CFG-3)

`duplicate_template` (`duplicate_configuration`, command `TEMPLATE_DUPLICATE`):
- Creates a template with the label and key its creator gives (SPEC 4.0.0).
- Creates one draft version copying the chosen version's item definitions, placements (including kit placements, KDEF-4) and settings, through the same validation as `save_template_draft`.
- Writes no publication, override, binding or obligation, and leaves the source unchanged.

## Duplicating a vehicle (CFG-4)

`duplicate_vehicle` (`duplicate_configuration`, command `VEHICLE_DUPLICATE`), with a preview. Locks: the organization (for the new identity), then the source vehicle row.

| Created for the copy | From the source |
|---|---|
| The vehicle, with the VIN, name and number its creator gives (SPEC 4.0.0), inactive, and its first activity event (inactive) | — |
| One revision-1 row per position, with a new row id and the **same `stable_slot_id`** as the source position (so copied placements, kit-list lines and bindings resolve on the copy), effective when recorded | Each position's label, order and sealable designation in effect now; retired positions are left out |
| One draft kit list, empty if the list in force is empty; none if no list is in force | The lines in force, mapped to the copy's compartment rows |
| One draft override per bound template, over the same template version | Each override in force (none when there is none) |
| One binding per bound template and slot, effective when recorded | The source's bindings in force |
| Item locks for the bound templates' items | — |

- **Not copied:** the VIN, kit instances and equipment, checks, obligations, deficiencies, seals, operational status, odometer and engine hours, and every history.
- **Bindings:** the copy's bindings owe nothing while it is inactive (CFG-1).
- **Guard change:** the binding insert guard must accept `VEHICLE_DUPLICATE`, as it accepts `VEHICLE_IMPORT` (`cya_kits` 0012).
- **Preview:** it lists the copied rows and the exclusions above (CFG-6).
  - `preview_vehicle_duplicate` only reads. The screen that calls it checks `duplicate_configuration`, as the vehicle-file preview's screen checks its own action.
  - It returns a `base_token`, a digest of everything the copy would take.
  - `duplicate_vehicle` recomputes the plan under the organization and source vehicle locks, and refuses if the token differs.
- **Left out, and listed in the preview:**
  - an override entry that names one of the source's kit instances (kits are not copied);
  - an override that overlays an older template version than the one in force (stale);
  - an entry or kit-list line whose position is retired on the source.
  The copy is still made without them.

## Duplicating a kit instance (CFG-5)

The screen calls `add_kit_instance` with the source instance's definition, a new label and the chosen vehicle and compartment. No new command is needed.

## Parts

Each part is one PR, except part 5, which is three (owner decision 2026-11-15, to keep each change reviewable). Every part uses `Refs #130`; the last uses `Closes #130`. Parts 3 and 4 do not change obligation state, so they need not wait for part 2; the screens (parts 5 to 7) follow the commands they use.

1. This design page. Merged in PR #153.
2. Vehicle lifecycle: activity events, commands, obligation exemption and projection, after agreement with issue #131.
3. Positions: the schema, the commands and the retire checks, plus the vehicle-file changes for order and retirement. Implemented in `cya/kits/positions.py`, with `cya_checklists` 0007 and `cya_kits` 0013.
4. Duplication: the template and vehicle commands with their previews. Implemented in `cya/operations/duplication.py`, with `cya_kits` 0014.
5. Fleet screens, as three PRs. The owner's order was 5b, 5a, 5c. 5c was built before 5a, and 5a is stacked on it because it reuses 5c's shared page helpers (`cya/kits/admin_pages.py`):
   - **5a, vehicles** (`cya/operations/vehicle_admin_views.py`, `manage_vehicles`):
     - the fleet list, searchable by name, number or VIN, showing whether each vehicle is active apart from its operational status;
     - creating a vehicle with its VIN, and editing its name and number;
     - deactivating and reactivating: the page states the effect first (CFG-6), and the confirmation is refused if the vehicle is already in the other state;
     - duplicating (`duplicate_configuration`): part 4's preview lists what is copied and what is not, and the confirmation carries its base token. A changed source refuses it and shows the new preview.
   - **5b, positions and access:**
     - the Supervisor access above;
     - per vehicle, add, rename, reorder and retire compartments and systems (`cya/kits/position_views.py`);
     - retiring previews what blocks it, or asks for confirmation (CFG-6).
   - **5c, assigned physical kits** (`cya/kits/kit_instance_views.py`):
     - a vehicle's active physical kits, their labels and compartments, and the assignment history;
     - assigning an existing stock kit to a vehicle and compartment, or returning an assigned kit to stock;
     - linking to the physical kit inventory to create a kit before assigning it. Assignment history defines what the vehicle is expected to carry; there is no current kit-list draft or publication workflow.
   - **Find and move physical kits** (`cya/kits/location_views.py`, issue #277, `manage_kit_assignment`):
     - one organization-wide list of every active physical kit, including unassigned stock, with its label, kit type and current vehicle and compartment (or "Unassigned stock"), 25 per page;
     - a search by kit label, kit type, vehicle name or number, or compartment (by its current name after a rename; the stored older name matches only a kit whose compartment can no longer be chosen), and a location filter for stock or one vehicle. An invalid filter lists nothing;
     - in each row, **Change location** opens that row alone: a choice of unassigned stock or a compartment of an active vehicle, with Save, Reset and Cancel as in role administration. Only one row is open at a time, so the page carries one option list rather than one per row, and its queries do not grow with the fleet. Saving appends one assignment event through `record_kit_assignment`, so a move from one vehicle to another needs no stock step and the old placement ends at the instant the new one begins (TIME-1a);
     - the row carries the kit's latest assignment event as shown. If the kit changed since, the save is refused, no event is appended (the refusal is audited) and the row then shows the current location. Choosing the place the kit is already in (the same vehicle and compartment slot) saves nothing. A kit on an inactive vehicle or in a retired compartment is marked so and keeps its place until moved. A refused rule keeps the row's choice. A saved move is confirmed from the command receipt, in its row when the kit is on the page and otherwise above the list.
6. Checklist screens, as two PRs (owner decision 2026-11-15):
   - **6a, templates** (described above): create, duplicate, draft edit one item at a time, publish now or at a station-local date and time after the impact preview, and the versions in force, scheduled and drafted;
   - **6b, overrides** (described above): a vehicle's override in force, scheduled and draft, edited one entry at a time with 6a's item editor, carried over when stale, and published only for a bound vehicle.
7. Kit definition screens (`cya/kits/definition_admin_views.py`, `manage_kit_definitions`): list and create, edit the draft one content item at a time (with new items by name and the stale-edit check, as in 6a), publish now or later after a preview, duplicate any version, a paged history, and deactivate or reactivate after a preview of where the definition is used (templates placing it and active physical kit instances; bounded, and refused if it changed since the preview, CFG-6). The kit library page keeps import and export and links to these screens.

The issue's acceptance criteria for responsive and accessible pages (UX issue #111), and its role, duplication, versioning, history and concurrency tests, apply to each part that adds the behaviour.
