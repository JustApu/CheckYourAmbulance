# Obligations per kit instance

[issue #92](https://github.com/JustApu/CheckYourAmbulance-archive/issues/92) implements [SPEC §4.2, §5.1, TIME-1 and TIME-1a, SEAL-10, §18 and §18.1](../SPEC.md) as
amended by SPEC 1.8.0 and 1.10.0 (decisions OQ-53 and OQ-54). A kit is a template; a vehicle keeps
zero, one or several instances of it; **each instance owes its own obligation**, so checking one
of several identical kits never satisfies the others. SPEC and the
[contracts](data-model-contracts.md) win if they disagree with this page.

## What exists

| Piece | Where |
|---|---|
| Append-only assignment history, guards, grants | `cya/kits/models.py`, migrations `0001_initial`, `0002_kit_guards` |
| "Which instances were in this slot on this vehicle, as the records stood" | `cya/kits/history.py` |
| Audited commands to add, move and deactivate a kit instance | `cya/kits/commands.py` |
| The kit instance in the obligation anchor | `cya/obligations/models.py`, migration `0003_kit_instance_anchor` |
| One requirement per instance | `cya/obligations/requirements.py` (`desired_at`) |
| Planning, writing, reading | `planning.py`, `generation.py`, `queries.py`, `projections.py`, `state.py` |
| Tests | `cya/kits/tests/`, `cya/obligations/tests/test_kits.py` |

## The assignment record

`kit_assignment_event` (append-only) records where a kit instance was and whether it was active:
a gapless per-kit sequence, each event strictly later than the one before, vehicle and
compartment (both set while active, both null once deactivated). An assignment takes effect when
it is recorded and is never backdated; the database refuses `effective_at` before `recorded_at`.
`kit_instance`'s vehicle, compartment and active flag are only a projection of the latest event,
updated in the same transaction. Every as-of question reads the events, never the row.

**Commands.** `add_kit_instance` creates the instance and its first event;
`record_kit_assignment` moves it (a vehicle and compartment) or deactivates it. Both need the
`manage_kit_assignment` permission (Supervisor, Supply Officer, Administrator; not Crew), run
through `run_command` (audited, idempotent on the operation id), and lock the vehicles involved
in id order before the kit, the same lock order as obligation generation. After the command
commits, those vehicles' upcoming obligations are reconciled; a failure there is logged and left
to the next `generate_obligations` run, as for requirement bindings. They are server functions;
there is no HTTP route or screen yet.

## Which instances a vehicle owes

An instance is in a slot on a vehicle at an instant when its latest event effective by then is
active, on that vehicle, in a compartment of that slot (`stable_slot_id`). A checklist item names
a slot through its kit reference; the slot is the one the reference kit was **first placed in**,
so moving or deactivating the reference kit does not change what the item means.

For a due instant `D` the generator writes one obligation per instance in the slot:

- no instance, no obligation; items outside a kit are unchanged;
- an instance added later is owed only for due instants whose early window had not yet opened at
  its activation (SPEC TIME-1a): an inspection whose window is already open is not required for
  it, and an inspection already due is not backfilled, so it is owed from the next due instant;
- an instance removed, deactivated or moved stops being owed: obligations due after the removal
  are retired (even in-window), earlier ones keep their state, so a removal cannot erase a miss;
- a move is a removal plus an addition; a temporary absence is a gap with no backfill;
- a kit that leaves and returns gets a new requirement (it pins the first event of its current
  run), so its retired obligations are never revived.

The assignment history is deliberately **not** cut off at the freeze instant that protects
in-window obligations from later checklist publications (SPEC §5.7): a removal is a physical fact
and retires what is due after it. Publications still behave as before, per instance.

## The anchor

An obligation, its requirement version and its selection stream gain `kit_instance` (the real
foreign key, null outside a kit) and `kit_key`: the same id, or the all-zero UUID for no kit, as
a NOT NULL value. MariaDB `UNIQUE` treats NULLs as distinct, so two rows differing only by a
missing instance would otherwise both be allowed; with `kit_key` they cannot (SPEC §18.1 item 3).
A CHECK keeps the two columns in step. The unique keys, the three composite anchor foreign keys
and the stream trigger now include `kit_key`, and a trigger refuses a requirement for a kit item
that names no instance, or pins an event that is not that instance being active on the vehicle in
the item's slot. A non-kit requirement's id is computed exactly as before, so no existing
obligation is replaced.

## Reading, and what check linking needs

Effective obligations, the as-of projection and the state engine carry `kit_instance_id`;
`effective_selection(..., kit_instance_id=)` names an instance. [issue #74](https://github.com/JustApu/CheckYourAmbulance-archive/issues/74) links a check to the
obligation whose `kit_instance_id` equals the check's `kit_id` and to no other. State is derived
per obligation, so one instance's check cannot satisfy another's.

## Migration

`cya.kits` `0001` refuses (changing nothing) if any `kit_instance` row already exists: active kits
come only from the assignment history, an event needs a command receipt that cannot be invented
for a past action, and a kit with no history would silently owe nothing. Resolve such rows first.

`0003` adds columns with the all-zero default, so existing rows (all outside a kit) take it
from the default; no `UPDATE` runs, so the append-only triggers never fire. It drops the three anchor foreign keys and two triggers, replaces five unique keys and
recreates them against `kit_key`. **This migration refuses before changing its anchor** if any
row names a kit instance (a guard that runs first within this migration, because MariaDB DDL is
not transactional). Django may already have unapplied dependent migrations before reaching that
guard. The refusal test restores the migration leaves in a finally block and verifies the
check-record keys, indexes and append-only triggers are unchanged afterward; [issue #145](https://github.com/JustApu/CheckYourAmbulance-archive/issues/145) makes
that restoration work on dump-loaded worker databases too (see
[check-reference migration reversals](check-records.md#reversing-reference-migrations-on-cloned-test-databases)).
Without kit rows the migration reverses and re-applies cleanly, which
`cya.tests.test_audit_integrity_db` exercises for every migration.

## Not in this issue

Check-to-obligation linking and the conflict warning ([issue #74](https://github.com/JustApu/CheckYourAmbulance-archive/issues/74), [issue #75](https://github.com/JustApu/CheckYourAmbulance-archive/issues/75)); the attention state
([issue #83](https://github.com/JustApu/CheckYourAmbulance-archive/issues/83)); an HTTP route or screen to manage kits; per-instance expiration dates (the sealed-kit
workflow, SPEC §10.1); roles beyond `manage_kit_assignment`. SPEC §23 has no numbered criterion
for kit instances: `tests/scenarios/EX-kit-instance-obligations.json` is a pending contract
citing AC-1, because adding a numbered criterion needs a SPEC amendment.

## Verification

`python manage.py test cya.kits cya.obligations --noinput` covers: two identical kits owing
separately; none owing nothing; add, remove, return, move and temporary absence; publication per
instance; as-of history; idempotent regeneration; the database guards attacked directly; the
reverse-migration refusal; command authorization, validation, replay and a concurrent-move race;
and, with `CYA_DB_SECURITY_TESTS=1`, the real runtime account.

## Sealable designations

Issue #131 slice 1 records which cabinets and kits are sealed (SPEC §10.1 SEAL-1, §10.2 CAB-1). Both commands are in `cya/kits/sealable.py`. They run through `run_command` (operation-id receipt and replay, success or failure audit) under the policy action `designate_sealable` (Supervisors and Administrators; §3.3).

| Command | Writes | Rules |
|---|---|---|
| `designate_compartment_sealable(vehicle_id, compartment_id, sealable, effective_at=None)` | a new `compartment` revision of the same slot, label copied, `sealable` set | Compartment rows are immutable, so the old value stays in history. `compartment_id` may name any revision of the slot on that vehicle. The change takes effect when recorded or at a later `effective_at`, never before the slot's latest revision; a value equal to that revision is refused. |
| `designate_kit_sealable(kit_id, sealable)` | a `kit_seal_designation` row (append-only; next in the kit's sequence) | Effective when recorded. A kit with no row is not sealed. A value equal to the kit's current designation is refused. |

`kit_sealable(organization_id, kit_ids, at)` answers which kits were sealed at an instant, from `kit_seal_designation` only, for at most 2000 kits per call. A compartment's designation at an instant is the `sealable` flag of the revision `vehicle_slots` returns for that instant.

Designations do not change obligations, checks or kit assignment history. Substituting an intact seal for checking contents (SEAL-4, CAB-4), and breaking and resealing, are a later slice built on one shared seal model (OQ-66). The paper checklist already prints seal rows from the designations; see [crew inspection screens](crew-inspection-ui.md#paper-checklists-for-outages). There is no designation screen yet; setting up compartments and kits is issue #130.


## Kit definitions

[Issue #129](https://github.com/JustApu/CheckYourAmbulance/issues/129) part 2 adds the kit definitions of [SPEC §4.6](../SPEC.md) (KDEF-1 to KDEF-8, amendment 1.34.0): a kit type whose contents are versioned like a checklist template. The commands are in `cya/kits/definitions.py`. Each needs the policy action `manage_kit_definitions` (Supervisors and Administrators; §3.3, PERM-41) and runs through `run_command` (operation-id receipt and replay, success or failure audit).

| Command | Writes | Rules |
|---|---|---|
| `create_kit_definition(key, label)` | `kit_definition` | The key is permanent (the database refuses a change): 1 to 100 characters, a lowercase letter or digit first, then `a-z 0-9 . _ -`, unique in the organization. |
| `save_kit_definition_draft(definition_id, contents)` | the next `kit_definition_version` and its `kit_definition_item` rows | The complete content set, 1 to 500 entries, one per active item. A kit holds items only (KDEF-5): any kind except `VEHICLE_READING`, a quantity only for `QUANTITY`, a unit only for `MEASUREMENT`, outcomes including `PASSED`, and an `expiration_tracked` flag. Errors name `contents[i]`. Refused for an inactive definition. |
| `publish_kit_definition_version(version_id, effective_at=None)` | `kit_definition_publication` | Effective when recorded, or later; never earlier, and strictly later than the definition's latest publication. Refused for an inactive definition or a version already published. The database then freezes the version and its contents. |
| `duplicate_kit_definition(source_version_id, key, label)` | a new definition and its first draft | Copies that version's contents, with none of the original's history. |
| `set_kit_definition_active(definition_id, active)` | `kit_definition.active` | A deactivated definition cannot be drafted, published or given to a new kit; kits that have it keep it. |

`add_kit_instance` takes an optional `definition_id`. Given one, it records `kit_instance_definition` (append-only, one row per kit, fixed for the kit's life, KDEF-3) in the same command; the definition must exist and be active (the command checks under a row lock and the database insert guard checks again), and the database accepts the row only from the command that created the kit (its first `kit_assignment_event`) and only for the definition that command's receipt recorded as requested (`definition_id` in the `KIT_ADDED` body), so an existing kit can never be given a definition later, even by reusing its creation receipt. Kits added without one, and every kit made before this part, have no definition (KDEF-8).

`version_effective_at(organization_id, definition_id, at)` answers which published version is in force at an instant.

**Not yet.** Checklists still place kit contents by slot, so a kit definition changes no obligation, check or printout yet. Kit-library import and export (§4.7) is part 3: see [kit-library files](kit-library-files.md). Placing kits by definition (`checklist_kit_placement`, KDEF-4) and vehicle files are later parts of issue #129.

## Kits placed by definition and vehicle kit lists

[Issue #129](https://github.com/JustApu/CheckYourAmbulance/issues/129) part 4a implements the placement and kit list rules of [SPEC 2.0.0 §4.6](../SPEC.md) (KDEF-4, KDEF-9).

- **Placements.** A checklist template version names the kit definitions it inspects, stored in `checklist_kit_placement`: a definition and a display order, with no compartment and no count. Placements are frozen with their version once it is published, like `checklist_item_def`.
  - **Draft:** `save_template_draft(..., placements=[{definition_id, display_order}])` validates each placement (`cya/kits/placements.py`). The definition must exist in the organization and be active and it may appear only once. Omitting `placements` keeps an existing request's digest.
  - **Publish:** `publish_template_version` refuses a placed definition that is inactive or has no published version. It locks the placed definitions until the publication commits, so a concurrent deactivation waits for it.
- **Vehicle kit lists.** Each vehicle has its own kit list (`cya/kits/kit_lists.py`). A line says how many instances of a definition the vehicle should carry, exactly, in one of its compartments (at least 1; SPEC sets no upper limit).
  - **Storage:** `vehicle_kit_list_version` and `vehicle_kit_requirement` are versioned and frozen once a `vehicle_kit_list_publication` names the version. The database also refuses a line whose compartment belongs to another vehicle.
  - **Draft:** `save_vehicle_kit_list_draft(vehicle_id, lines=[{definition_id, compartment_id, required_count, display_order}])` writes the next revision with its complete lines. A definition may appear only once per compartment slot, and an empty list is allowed.
  - **Publish:** `publish_vehicle_kit_list(version_id, effective_at=None)` takes effect at or after the publication, later than the vehicle's previous list. It refuses an inactive listed definition and locks the listed definitions until it commits.
  - **Permission:** both commands need the policy action `manage_vehicle_kit_lists` (PERM-44: Supervisors and Administrators).
  - **List in force:** `kit_list_effective_at(organization, vehicle, at, recorded_before=None)` returns the version in force at `at`.
- **Resolve.** `resolve_checklist` returns `placements`, one `ResolvedPlacement` per placed definition, with:
  - `lines`: the vehicle's kit list lines for that definition in force at `as_of`, each with the vehicle's own compartment row and the required count;
  - `kits`: every instance of the definition on the vehicle at `as_of`, in any compartment, with the compartment it is in.

  Kits come from the kit assignment history (`cya/kits/history.py`, with the same `recorded_before` cutoff), never from the current kit rows, so a later move, deactivation or kit list does not change a past resolution.

## Inspections of placed kits

Issue #129 parts 4b and 4c make kits placed by definition drive inspections ([SPEC §4.6](../SPEC.md) KDEF-4, KDEF-7). Every slot-placed kit item keeps working exactly as before (KDEF-8). Each record below names exactly one content source: a `checklist_item_def`, or a placed definition's `kit_definition_item`. The database checks this.

- **Requirements and obligations** (`cya/obligations/requirements.py`, migration cya_obligations 0015):
  - For each placement at due instant `D`, every instance in the placed slot whose fixed definition is the placed one owes one requirement per content entry.
  - The content comes from the definition version in force at `D`, as the records stood.
  - As for any kit item, an instance added after the window opened waits for the next due instant (TIME-1a). A version published later never changes an obligation already created (§5.7).
  - `requirement_version` pins `kit_definition_item` and `placement` instead of `item_def`. Its insert guard checks:
    - the placement belongs to the pinned publication's version;
    - the content belongs to a published version of the placed definition, for the same item;
    - the kit has the placed definition and is active in the placed slot at its pinned assignment event.
- **Checks** (`cya/checks/commands.py`, migration cya_checks 0012):
  - A check on placed content names the kit. The command resolves the content from the placement holding that kit, using the version in force when the session opened, and stores `kit_definition_item`.
  - The insert guard requires the content to belong to a definition the check's checklist version places, and the kit to have that definition in the placed slot.
  - Corrections keep the original content source. Publishing a template with placements, or a definition version, creates the item locks a check needs.
- **Deficiencies** (cya_deficiencies 0020):
  - Affected items, verification scope rows, scope expansions and check suspensions carry the same alternative.
  - A deficiency on a kit covers all of that kit's content. Compartment, item and report-time scopes include placed content too.
  - Instances come from the assignment history, kept when their fixed definition matches.
- **Crew checklist and printouts:**
  - The checklist query lists one row per kit of a placed definition for each content entry; the row's `definition_id` is the content entry.
  - The outage sheet lists those kits.
  - The kit reference prints each placed definition's contents for its version in force.
- **Ad hoc requests:** a request naming a placed kit and one of its content items pins that content entry.


**Not yet.** Obligations, checks, the checklist screen and printouts do not read placements or kit lists, so publishing either changes no inspection (KDEF-8). Later parts of issue #129 add these:
- the presence item and kit presence obligations (KDEF-10 to KDEF-12);
- contents obligations from placed definitions;
- vehicle files that write kit lists (IMP-1, IMP-3).

The database still refuses to publish a template version with no items (cya_checklists 0002), so a kits-only template needs at least one item until then.
