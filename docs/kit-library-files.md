# Kit-library files

[Issue #129](https://github.com/JustApu/CheckYourAmbulance/issues/129) part 3 implements the kit-library file type of [SPEC §4.7](../SPEC.md) (IMP-1 to IMP-4, amendment 1.34.0). A kit-library file lists kit definitions ([§4.6](../SPEC.md)) and their contents. It can be JSON or CSV, and the two hold the same information. Vehicle files, the second type in IMP-1, come in a later part. SPEC and the [contracts](data-model-contracts.md) win if they disagree with this page.

## Where

**Administration → Kit library** (`/administration/kits/`).

- **Supervisors and Administrators** (policy action `export_configuration`, PERM-43) see the definitions and download blank templates and the current library.
- **Administrators** (`import_configuration`, PERM-42) also import.

Anyone else gets 403, and the denial is audited. The Administration menu entry is Administrator-only, so Supervisors open the page by its address for now. Issue #130 adds the management screens.

## Import

1. **Preview.** Choose the file and its format. The file is read in full and nothing is written (IMP-2). The page lists each kit as *Create*, *New draft revision* or *Unchanged*, plus the items that would be created. If anything is wrong, it lists every problem by JSON path or CSV row and column instead.
2. **Import as drafts.** One audited command applies the whole preview, or nothing (IMP-3):
   - It creates the missing items and definitions.
   - It adds a new draft revision for each changed kit, and updates changed labels.
   - It never publishes: publishing stays a separate step (KDEF-2).
   - It never deactivates or deletes anything the file leaves out.
   - Importing a file identical to the library changes nothing.
   - The confirmation is refused, and you preview again, if the library, an item label's match, or the file has changed since the preview.

   The page carries the file back for the confirmation; the server keeps no copy between the two steps. The audit records the actor, file type, format and counts, never the contents (IMP-4).

Limits:
- 1,000,000 bytes, UTF-8 (a byte-order mark is accepted).
- 200 kits per file, 500 contents per kit, 20,000 CSV rows.
- At most 200 problems are listed.

## Items

A content entry names its item by **exact label**:
- If exactly one active item has that label, it is used.
- If no item has the label, the import creates one.
- A label shared by several active items, or held only by inactive items, is refused, naming the label.
- An item may appear only once in a kit.

## JSON

```json
{
  "file_type": "kit-library",
  "schema_version": 1,
  "kits": [
    {
      "key": "example-airway-kit",
      "label": "Example airway kit",
      "contents": [
        {"item": "Example adult masks", "kind": "QUANTITY", "quantity": 2, "unit": null,
         "outcomes": ["PASSED", "FAILED"], "expiration_tracked": true,
         "instructions": "Count unopened packages; note the earliest expiration."}
      ]
    }
  ]
}
```

Unknown fields are refused. The order of `contents` is the display order.

## CSV

The header row must hold exactly these columns, in any order:

`file_type, kit_key, kit_label, item, kind, quantity, unit, outcomes, expiration_tracked, instructions`

- There is one row per content entry, and rows with the same `kit_key` form one kit, in row order.
- Every row's `file_type` is `kit-library`, which says the file's type (IMP-1).
- Within a kit, `kit_label` must be the same on every row.
- `kit_key`, `kit_label` and `item` are trimmed, and blank lines are skipped.
- `outcomes` separates values with `|`.
- `expiration_tracked` is `yes` or `no`.
- A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is exported with a leading `'`, so a spreadsheet does not run it as a formula (RPT-2). That apostrophe is removed on import, so files round-trip exactly.

## Fields

| Field (JSON / CSV) | Required | Meaning |
|---|---|---|
| `key` / `kit_key` | yes | Stable kit key: 1 to 100 characters, a lowercase letter or digit first, then `a-z 0-9 . _ -`. It matches the existing definition and can never change. |
| `label` / `kit_label` | yes | Display name, up to 200 characters. A different label renames the definition. |
| `item` | yes | Item label, matched as described in [Items](#items). |
| `kind` | yes | `PRESENCE_CONDITION`, `QUANTITY`, `MEASUREMENT` or `SPECIAL_OBSERVATION` (a kit never holds a vehicle reading, KDEF-5). |
| `quantity` | `QUANTITY` only | Exact expected count (a whole number). |
| `unit` | `MEASUREMENT` only | Unit, 1 to 40 characters. |
| `outcomes` | no | Allowed outcomes, including `PASSED`, from `PASSED`, `FAILED` and `UNABLE_TO_INSPECT`. The default is all three. |
| `expiration_tracked` | no | Whether the item's expiration is recorded. The default is no. |
| `instructions` | no | Up to 4,000 characters. |

## Export and templates

- **Export:** the current library holds each active definition's latest revision, draft or published, so re-importing it shows every kit as *Unchanged*. Each export is an audited command that writes nothing and records the actor, format and kit count.
- **Templates:** the blank templates hold one synthetic example kit and come from the same code the importer validates with.

## Code

- `cya/kits/library_files.py`: parsing and rendering.
- `cya/kits/library_import.py`: preview, import and export commands.
- `cya/kits/views.py` and `cya/kits/templates/kits/library.html`: the page.
- Tests in `cya/kits/tests/`: `test_library_files.py`, `test_library_import.py` and `test_library_http.py`.
