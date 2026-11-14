# Vehicle files

[Issue #129](https://github.com/JustApu/CheckYourAmbulance/issues/129) implements the vehicle file type of [SPEC §4.7](../SPEC.md) (IMP-1 to IMP-7, amendments 1.34.0 and 2.1.0). A vehicle file describes one vehicle: its VIN, its compartments and systems, the items it carries outside kits, its kit list and its schedule. It can be JSON or CSV, and the two hold the same information. The other file type is the [kit library](kit-library-files.md). SPEC and the [contracts](data-model-contracts.md) win if they disagree with this page.

## Where

**Administration → Vehicle files** (`/administration/vehicle-files/`).

- **Supervisors and Administrators** (policy action `export_configuration`, PERM-43) see the active vehicles with their VINs, download blank templates, and export a vehicle that has a VIN.
- **Administrators** (`import_configuration`, PERM-42) also import.

Anyone else gets 403, and the denial is audited. The Administration menu entry is Administrator-only, so Supervisors open the page by its address for now.

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

An import sets the VIN of the vehicle it creates and the keys of positions it creates. Records made before SPEC 2.1.0 have no key until an Administrator sets it once (`set_configuration_keys`, PERM-45). The commands are in `cya/kits/keys.py`; there is no screen for them yet (issue #130 adds the management screens). A vehicle without a VIN cannot be exported, and a position without a key cannot be named in a file.

## Import

1. **Preview.** Choose the file and its format. The file is read in full and nothing is written (IMP-2). The page summarizes the plan: whether the vehicle is created or updated, positions added or renamed, checklist changes, kit list changes and kits added, and the schedule assigned. If anything is wrong, it lists every problem by JSON path or CSV row instead.
2. **Import.** One audited command (`VEHICLE_IMPORT`) applies the whole preview, or nothing (IMP-3, IMP-7):
   - **Vehicle:** an unknown VIN creates the vehicle; a known VIN updates its display name and number. An inactive vehicle's VIN is refused.
   - **Compartments and systems:** a key the vehicle already has keeps that row, and a changed label adds a new revision. A key used on other vehicles adds this vehicle's row in that position. A new key creates the position.
   - **Items outside kits:** the file names the vehicle's checklist template by key, and each item is matched by exact label against the template version in force. An item already there with the same fields is unchanged. Any other item becomes an entry (add or replace) in one new **draft** override of that vehicle; entries of an existing override that the file does not change are carried into it. An unknown label creates the item. The import never writes a template version.
   - **Kit list:** a new **draft** kit list when any line is new or its count differs; lines in force that the file leaves out are kept. When fewer kits of a definition are in a compartment than the file's count, the import adds the missing kits there.
   - **Schedule:** if the vehicle has no binding to the template, the import binds it to the named schedule, or to the organization's only schedule when the file names none. An existing different binding is refused; the import never changes or removes one.
   - It never publishes: publish the override and the kit list as separate steps.
   - It never deactivates or deletes anything the file leaves out.
   - The confirmation is refused, and you preview again, if the configuration or the file has changed since the preview.

   The page carries the file back for the confirmation; the server keeps no copy between the two steps. The audit records the actor, file type, format and summary counts, never the contents (IMP-4).

Limits: 1,000,000 bytes, UTF-8 (a byte-order mark is accepted), and 20,000 CSV rows. At most 200 problems are listed.

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
       "outcomes": ["PASSED", "FAILED"], "instructions": ""}
    ],
    "kits": [{"kit": "example-airway-kit", "compartment": "example-cabinet", "count": 1}]
  }
}
```

Unknown fields are refused. Download the blank template for an exact, valid example.

## CSV

The header row must hold exactly these columns, in any order:

`file_type, record, vin, display_name, number, template, schedule, key, label, item, compartment, system, kind, quantity, unit, outcomes, instructions, kit, count`

- Every row's `file_type` is `vehicle`.
- `record` says what the row is: `vehicle` (exactly one), `compartment`, `system`, `item` or `kit`. A row leaves empty every column its record type does not use.
- `outcomes` separates values with `|`.
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
| `kit` | yes | Active kit definition key. |
| kit `compartment` | yes | Compartment position key. |
| `count` | yes | Exact required number of that kit there (KDEF-9), at least 1. A definition appears once per compartment. |

## Export and templates

- **Export:** one vehicle in normal form from the configuration in force, so re-importing it changes nothing once its drafts are published. The vehicle, its template and each of its positions need keys, and the vehicle must be bound to exactly one template; otherwise the export is refused with the reason. Each export is an audited command (`VEHICLE_EXPORT`) that writes nothing and records the actor, vehicle, format and item and kit counts.
- **Templates:** the blank templates hold one synthetic example vehicle and come from the same code the importer validates with.

## Code

- `cya/kits/vehicle_files.py`: parsing and rendering.
- `cya/kits/vehicle_import.py`: preview, import and export commands.
- `cya/kits/keys.py`: the key commands.
- `cya/kits/vehicle_views.py` and `cya/kits/templates/kits/vehicle_files.html`: the page.
- Tests in `cya/kits/tests/`: `test_keys.py`, `test_vehicle_files.py`, `test_vehicle_import.py` and `test_vehicle_http.py`.
