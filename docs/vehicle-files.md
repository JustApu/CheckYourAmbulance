# Vehicle files

[Issue #129](https://github.com/JustApu/CheckYourAmbulance/issues/129) implements the vehicle file type of [SPEC §4.7](../SPEC.md) (IMP-1 to IMP-7, amendments 1.34.0 and 2.1.0). A vehicle file describes one vehicle: its VIN, compartments and systems, items outside kits, checklists and schedule assignments. Schemas retain kit-count rows for compatibility; import treats them as provisioning instructions for creating physical kit instances. The resulting assignment history defines what the vehicle is expected to carry. Vehicle files can be JSON or CSV. The other file type is the [kit library](kit-library-files.md). SPEC and the [contracts](data-model-contracts.md) win if they disagree with this page.

## After the cutover: schema 2 (SPEC 5.0.0, [issue #220](https://github.com/JustApu/CheckYourAmbulance/issues/220))

Once an organization has moved to vehicle-owned checklists (`migrate_vehicle_checklists`), vehicle files use **schema 2** (`cya/kits/vehicle_files_v2.py`, `cya/kits/vehicle_import_v2.py`). Everything below about schema 1 describes files before the cutover.

- **What a file holds** (IMP-7): the vehicle (VIN, name, number), its compartments and systems with their keys, its **checklists** in precedence order, each with an id local to the file, a label, the schedule slots it is assigned to, the kit definitions it inspects and its items (an item with neither compartment nor system is **Not placed**, CHK-9). Existing schemas also retain a `kits` section of definition, compartment and count rows for import compatibility. Import provisions physical units from those rows; current vehicle expectations come from each unit's assignment event, not from the count. JSON has a `checklists` list in the vehicle object; CSV has `checklist`, `item` (with its checklist id), `inspects` and `assignment` rows and a `checklist` column in place of `template`.
- **Keys** (IMP-6): position keys and checklist ids are local to the file and to the vehicle. Equal keys on two vehicles name unrelated positions. Schedule slots and kit definition keys still name shared records.
- **Export** writes each checklist's version in force (else its latest version) and the slots it is assigned to now. Physical kits and history are never exported. An export re-imports unchanged. Item rows that name a kit instance cannot be expressed in a file; an import keeps them.
- **Import**, as before all or nothing after a preview with no writes:
  - a new VIN creates the vehicle with new positions (new stable slots) and new checklists, in the file's order after any existing ones;
  - for an existing vehicle, a position is matched by the vehicle's own key, and a checklist by its label: a different one becomes one **draft** version, publication staying a separate step (IMP-3);
  - each listed slot the checklist is not assigned to is assigned when the import is recorded;
  - nothing the file omits is removed, unassigned or deactivated, and no assignment ever makes a retired position required again (CFG-2).
- **Legacy files** (schema 1): the file holds the vehicle's effective checklist for one template. Its template key is resolved in the importing organization, and a key that does not resolve makes the file ambiguous, so it is refused. The result is one independent checklist labelled like the template, inspecting the kit definitions of the template's version in force, assigned to the file's schedule (or the only schedule). No binding to the legacy template is made.
- **Before the cutover** a schema 2 file is refused.

## With item requirements: schema 3 (SPEC 6.0.0 IMP-7, CHK-11, [issue #256](https://github.com/JustApu/CheckYourAmbulance/issues/256) C4)

Once the organization has also moved to item requirements (`migrate_item_requirements`), an export is **schema 3** (`cya/kits/vehicle_files_v3.py`). The example file is schema 3 as well. Import accepts schemas 1, 2 and 3.

- **What it adds.** A `requirements` list holds the vehicle's item requirements. Each has an `id` local to the file (IMP-6), its item, its place (compartment or system key, or neither for Not placed) and its values: `kind`, `quantity`, `unit`, `outcomes`, `expiration_tracked` and `instructions`. A file holds at most one requirement per item and place. A checklist's `items` then name requirements, `[{"requirement": "requirement-1"}, ...]`, in display order. So two checklists that inspect an item in one place name the same requirement, and can never give it different values (AC-42 (1)).
- **CSV.** CSV adds a `requirement` column and record type:
  - a `requirement` row carries the item columns and its id;
  - an `item` row carries only its `checklist` and `requirement`.
- **Export.**
  - Each requirement is written at its version in force now, else its first scheduled one. That includes a requirement no checklist inspects any more.
  - Checklist rows take their requirement's values and place, not the values the row recorded at publication. Reads follow the requirement (CHK-5), so an export never carries a value edited away.
  - The same holds for a schema 2 export after the cutover to item requirements.
- **Import** still writes checklist **drafts** only and never publishes a requirement version.
  - A requirement arises when a checklist that inspects it is published, under the publication rules (a)–(d).
  - A schema 2 file whose checklists give one item and place different values is refused at preview: their drafts could never both be published.
  - For an existing vehicle, the preview's **Item requirements** notes compare each item and place with the vehicle's requirement in force: new, or changed. A change that a checklist outside the file also inspects is called out, because publishing the draft is refused until the requirement itself is changed on the vehicle's item requirements page.
  - A file requirement that no checklist in the file inspects is noted and not imported.

## Where

**Administration → Vehicle files** (`/administration/vehicle-files/`).

- **Supervisors and Administrators** (policy action `export_configuration`, PERM-43) see the active vehicles with their VINs, download blank templates, and export a vehicle that has a VIN.
- **Administrators** (`import_configuration`, PERM-42) also import.

Anyone else gets 403, and the denial is audited. Supervisors and Administrators reach the page from Administration ([issue #130](https://github.com/JustApu/CheckYourAmbulance/issues/130) part 5b).

## Keys

Files name records by stable keys, never by database ids (IMP-6):

| Record | Key | Format |
|---|---|---|
| Vehicle | its VIN | 17 letters and digits, no `I`, `O` or `Q`, stored uppercase |
| Compartment or vehicle-system position | position key, the same on every vehicle's row of that position | a lowercase letter or digit first, then `a-z 0-9 . _ -`, at most 100 characters |
| Checklist template | template key | as a position key |
| Kit definition | its definition key | as a position key |
| Schedule | its existing schedule slot name | — |

Every key is set once and never changed. A remount is a new vehicle with the new VIN: export the old vehicle's file, replace the VIN, and import it. Then move the physical kits and deactivate the old vehicle, which keeps its history.

An import sets the VIN of the vehicle it creates and the keys of positions it creates. Records made before SPEC 2.1.0 have no key until an Administrator sets it once (`set_configuration_keys`, PERM-45). The commands are in `cya/kits/keys.py`. After the cutover an Administrator sets a missing compartment or system key on the vehicle's Compartments and systems page (issue #238 F1c); other keys have no screen yet. A vehicle without a VIN cannot be exported, and a position without a key cannot be named in a file.

## Import

1. **Preview.** Choose the file; the workflow detects CSV or JSON automatically. The file is read in full and nothing is written (IMP-2). The page summarizes the plan: whether the vehicle is created or updated, positions added or renamed, checklist changes, compatibility kit provisioning and the schedule assignment. If anything is wrong, it lists every problem by JSON path or CSV row instead.
2. **Import.** One audited command (`VEHICLE_IMPORT`) applies the whole preview, or nothing (IMP-3, IMP-7):
   - **Vehicle:** an unknown VIN creates the vehicle; a known VIN updates its display name and number. An inactive vehicle's VIN is refused.
   - **Compartments and systems:** a key the vehicle already has keeps that row, and a changed label adds a new revision that keeps the compartment's sealable designation. If a later revision of that position is already scheduled, a changed label is refused until it takes effect. A key used on other vehicles adds this vehicle's row in that position. A new key creates the position. A position retired on this vehicle (SPEC 4.8 CFG-2) cannot be named in a file, whether for the position itself or for an item or kit placed there.
   - **Items outside kits:** the file names the vehicle's checklist template by key, and each item is matched by exact label against the template version in force. An item already there with the same fields is unchanged. Any other item becomes an entry (add or replace) in one new **draft** override of that vehicle; entries of an existing override that the file does not change are carried into it. An unknown label creates the item. The import never writes a template version.
   - **Compatibility kit provisioning:** when an older file requests more units of a definition in a compartment than are already present, import adds the missing physical kits there. Those units receive assignment history and become expected inventory through their assignments. Existing assignments are not removed or moved when a line is omitted or reduced; no current inspection obligation is generated from a legacy count line.
   - **Schedule:** if the vehicle has no binding to the template, the import binds it to the named schedule, or to the organization's only schedule when the file names none. An existing different binding is refused; the import never changes or removes one. A new binding is also refused when the template places items in a position retired on this vehicle.
   - It never publishes: checklist changes are published as a separate step.
   - It never deactivates or deletes anything the file leaves out.
   - The confirmation is refused, and you preview again, if the configuration or the file has changed since the preview. The import holds the organization and the vehicle while it rechecks and applies the plan, so other changes to that vehicle wait for it.

   The page carries the file back for the confirmation; the server keeps no copy between the two steps. The audit records the actor, file type, format and summary counts, never the contents (IMP-4).

Limits:
- 1,000,000 bytes, UTF-8 (a byte-order mark is accepted), and 20,000 CSV rows.
- At most 1,000 compartments, 1,000 systems, 1,000 items and 1,000 kit lines per file.
- A kit count from 1 to 2,147,483,647, and at most 1,000 physical kits added by one import.
- CSV numbers have at most 10 digits. JSON that is nested too deeply or holds a number too long to read is refused as not valid JSON.
- At most 200 problems are listed.

## JSON

```json
{
  "file_type": "vehicle",
  "schema_version": 1,
  "vehicle": {
    "vin": "1FDXE4FS8KDC00001",
    "display_name": "Example Unit 1",
    "number": "1",
    "template": "example-daily",
    "schedule": "example-0600",
    "compartments": [{"key": "example-cabinet", "label": "Example cabinet"}],
    "systems": [{"key": "example-lights", "label": "Example lights"}],
    "items": [
      {"item": "Example wrench", "compartment": "example-cabinet", "system": null,
       "kind": "PRESENCE_CONDITION", "quantity": null, "unit": null,
       "outcomes": ["PASSED", "FAILED"], "expiration_tracked": false, "instructions": ""}
    ],
    "kits": [{"kit": "example-airway-kit", "compartment": "example-cabinet", "count": 1}]
  }
}
```

Unknown fields are refused. Download the blank template for an exact, valid example.

## CSV

The header row must hold exactly these columns, in any order:

`file_type, record, vin, display_name, number, template, schedule, key, label, item, compartment, system, kind, quantity, unit, outcomes, expiration_tracked, instructions, kit, count`

- Every row's `file_type` is `vehicle`.
- `record` says what the row is: `vehicle` (exactly one), `compartment`, `system`, `item` or `kit`. A row leaves empty every column its record type does not use.
- `outcomes` separates values with `|`.
- `expiration_tracked` is `yes` or `no`; an empty cell means no. A CSV file without this column is refused, so an older file cannot silently turn tracking off. Export again to get the current columns.
- Formula-like cells are neutralized with a leading `'` exactly as in [kit-library files](kit-library-files.md#csv), so files round-trip.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `vin` | yes | The vehicle's VIN. |
| `display_name` | yes | Display name. |
| `number` | no | Unit number. |
| `template` | yes | Checklist template key. The template must have a published version in force. |
| `schedule` | no | Schedule slot name. Required when the vehicle is not yet bound and the organization has several schedules. |
| compartment and system `key`, `label` | yes | Position key and the vehicle's label for it. |
| `item` | yes | Item label, matched as in [kit-library files](kit-library-files.md#items). An item appears once per file. |
| `compartment` or `system` | exactly one | Position key of the item's place. It must be on the vehicle or in the file. |
| `kind`, `quantity`, `unit`, `outcomes`, `instructions` | as a checklist item | As in a checklist template. `outcomes` defaults to all three. |
| `expiration_tracked` | no | Whether the item's expiration is recorded, as in a checklist template ([issue #152](https://github.com/JustApu/CheckYourAmbulance/issues/152)). The default is no. Entries carried into a new draft override keep their value. |
| `kit` | yes | Active kit definition key. |
| kit `compartment` | yes | Compartment position key. |
| `count` | yes | Exact required number of that kit there (KDEF-9), at least 1. A definition appears once per compartment. |

## Export and templates

- **Export:** one vehicle in normal form from the configuration in force, so re-importing it changes nothing once its drafts are published. Positions are written in display order (unnumbered last), then by label, and retired positions are left out. The vehicle, its template and each of its positions need keys, and the vehicle must be bound to exactly one template; otherwise the export is refused with the reason. Each export is an audited command (`VEHICLE_EXPORT`) that writes nothing and records the actor, vehicle, format and item and kit counts.
- **Templates:** the blank templates hold one synthetic example vehicle and come from the same code the importer validates with.

## Code

- `cya/kits/vehicle_files.py`: parsing and rendering.
- `cya/kits/vehicle_import.py`: preview, import and export commands.
- `cya/kits/keys.py`: the key commands.
- `cya/kits/vehicle_views.py` and `cya/kits/templates/kits/vehicle_files.html`: the page.
- Tests in `cya/kits/tests/`: `test_keys.py`, `test_vehicle_files.py`, `test_vehicle_import.py` and `test_vehicle_http.py`.
