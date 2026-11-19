"""Vehicle files with the vehicle's own checklists (SPEC 5.0.0 IMP-6, IMP-7; issue #220 part 5).

Pure functions, no database access, in the style of cya.kits.vehicle_files, whose helpers they
reuse. Schema version 2. A file holds exactly one vehicle and is self-contained: its positions,
its complete checklists with their schedule assignments, the kit definitions each checklist
inspects, and its kit list. Position keys and checklist ids are local to the file (IMP-6): they
name nothing on another vehicle. Schedule slots and kit definition keys name shared records.

The normal form is one dict:
  schema_version: 2, vin, display_name, number (or None),
  compartments / systems: [{"key", "label"}], in file order,
  checklists: [{"id", "label", "schedules": [slot], "kits": [kit definition key],
                "items": [item dicts as in vehicle_files, compartment and system both None
                          for a Not placed item (CHK-9)]}], in precedence order (CHK-10),
  kits: [{"kit", "compartment", "count"}] (the kit list), in file order.

CSV has one row per record, the record type in the "record" column: vehicle, compartment,
system, checklist (id and label, in precedence order), item (its checklist id), inspects (a
checklist id and kit definition key), assignment (a checklist id and schedule slot) and kit.
"""

import csv
import io
import json

from cya.kits.library_files import (
    DEFAULT_OUTCOMES,
    FileErrors,
    _Collector,
    _csv_bool,
    _csv_int,
    _neutralize,
)
from cya.kits.vehicle_files import (
    FILE_TYPE,
    KIT_FIELDS,
    MAX_CSV_ROWS,
    POSITION_FIELDS,
    _csv_rows,
    _json_item,
    _list,
    _object,
)

SCHEMA_VERSION = 2
CSV_COLUMNS = (
    "file_type",
    "record",
    "vin",
    "display_name",
    "number",
    "checklist",
    "schedule",
    "key",
    "label",
    "item",
    "compartment",
    "system",
    "kind",
    "quantity",
    "unit",
    "outcomes",
    "expiration_tracked",
    "instructions",
    "kit",
    "count",
)
RECORD_COLUMNS = {
    "vehicle": {"vin", "display_name", "number"},
    "compartment": {"key", "label"},
    "system": {"key", "label"},
    "checklist": {"checklist", "label"},
    "item": {
        "checklist",
        "item",
        "compartment",
        "system",
        "kind",
        "quantity",
        "unit",
        "outcomes",
        "expiration_tracked",
        "instructions",
    },
    "inspects": {"checklist", "kit"},
    "assignment": {"checklist", "schedule"},
    "kit": {"kit", "compartment", "count"},
}
VEHICLE_FIELDS = frozenset(
    {"vin", "display_name", "number", "compartments", "systems", "checklists", "kits"}
)
CHECKLIST_FIELDS = frozenset({"id", "label", "schedules", "kits", "items"})


def is_v2_csv(text):
    """Whether a CSV file's header is the schema 2 header (it names a checklist column)."""
    first = text.split("\n", 1)[0]
    try:
        header = next(csv.reader([first]))
    except csv.Error:
        return False
    return "checklist" in [h.strip() for h in header]


# --- JSON ------------------------------------------------------------------------------------


def _json_checklist(raw, where, found):
    raw = _object(raw, CHECKLIST_FIELDS, where, found)
    return {
        "id": raw.get("id"),
        "label": raw.get("label"),
        "schedules": list(_list(raw.get("schedules"), f"{where}.schedules", found)),
        "kits": list(_list(raw.get("kits"), f"{where}.kits", found)),
        "items": [
            _json_item(entry, f"{where}.items[{i}]", found)
            for i, entry in enumerate(_list(raw.get("items"), f"{where}.items", found))
        ],
    }


def parse_json_document(document):
    found = _Collector()
    if document.get("file_type") != FILE_TYPE:
        found.add("file_type", f"must be {FILE_TYPE!r}")
    unknown = set(document) - {"file_type", "schema_version", "vehicle"}
    if unknown:
        found.add("file", f"unknown fields {sorted(unknown)}")
    raw = _object(document.get("vehicle"), VEHICLE_FIELDS, "vehicle", found)
    vehicle = {
        "schema_version": SCHEMA_VERSION,
        "vin": raw.get("vin"),
        "display_name": raw.get("display_name"),
        "number": raw.get("number"),
    }
    for name in ("compartments", "systems"):
        vehicle[name] = [
            {"key": p.get("key"), "label": p.get("label")}
            for i, entry in enumerate(_list(raw.get(name), f"vehicle.{name}", found))
            for p in [_object(entry, POSITION_FIELDS, f"vehicle.{name}[{i}]", found)]
        ]
    vehicle["checklists"] = [
        _json_checklist(entry, f"vehicle.checklists[{i}]", found)
        for i, entry in enumerate(_list(raw.get("checklists"), "vehicle.checklists", found))
    ]
    vehicle["kits"] = [
        {"kit": k.get("kit"), "compartment": k.get("compartment"), "count": k.get("count")}
        for i, entry in enumerate(_list(raw.get("kits"), "vehicle.kits", found))
        for k in [_object(entry, KIT_FIELDS, f"vehicle.kits[{i}]", found)]
    ]
    found.raise_if_any()
    return vehicle


# --- CSV -------------------------------------------------------------------------------------


def _csv_item(row, where, found):
    outcomes = (
        [o.strip() for o in row["outcomes"].split("|")]
        if row["outcomes"].strip()
        else list(DEFAULT_OUTCOMES)
    )
    return {
        "item": row["item"].strip(),
        "compartment": row["compartment"].strip() or None,
        "system": row["system"].strip() or None,
        "kind": row["kind"].strip(),
        "expected_quantity": _csv_int(row["quantity"].strip(), f"{where}, quantity", found),
        "unit": row["unit"].strip() or None,
        "allowed_outcomes": outcomes,
        "expiration_tracked": _csv_bool(
            row["expiration_tracked"], f"{where}, expiration_tracked", found
        ),
        "instructions": row["instructions"],
    }


def _record_of(row, where, found):
    if row["file_type"] != FILE_TYPE:
        found.add(f"{where}, file_type", f"must be {FILE_TYPE!r}")
    record = row["record"].strip()
    if record not in RECORD_COLUMNS:
        found.add(f"{where}, record", f"must be one of {list(RECORD_COLUMNS)}")
        return None
    stray = sorted(
        c
        for c in CSV_COLUMNS
        if c not in ("file_type", "record") and c not in RECORD_COLUMNS[record] and row[c].strip()
    )
    if stray:
        found.add(where, f"a {record} row leaves {stray} empty")
    return record


def _checklist_named(checklists, row, where, found):
    checklist = checklists.get(row["checklist"].strip())
    if checklist is None:
        found.add(f"{where}, checklist", "no checklist row above has that id")
    return checklist


def parse_csv(text):
    rows, problems = _csv_rows(text, CSV_COLUMNS)
    found = _Collector()
    found.errors.extend(problems)
    if len(rows) > MAX_CSV_ROWS:
        raise FileErrors([f"file: more than {MAX_CSV_ROWS} rows"])
    vehicle, checklists = None, {}
    sections = {"compartments": [], "systems": [], "kits": []}
    for number, row in rows:
        where = f"row {number}"
        record = _record_of(row, where, found)
        if record is None:
            continue
        if record == "vehicle":
            if vehicle is not None:
                found.add(where, "a vehicle file has exactly one vehicle row")
                continue
            vehicle = {
                "vin": row["vin"].strip(),
                "display_name": row["display_name"].strip(),
                "number": row["number"].strip() or None,
            }
        elif record in ("compartment", "system"):
            sections[f"{record}s"].append(
                {"key": row["key"].strip(), "label": row["label"].strip()}
            )
        elif record == "checklist":
            local = row["checklist"].strip()
            if local in checklists:
                found.add(f"{where}, checklist", "that id is listed twice")
                continue
            checklists[local] = {
                "id": local,
                "label": row["label"].strip(),
                "schedules": [],
                "kits": [],
                "items": [],
            }
        elif record == "kit":
            sections["kits"].append(
                {
                    "kit": row["kit"].strip(),
                    "compartment": row["compartment"].strip(),
                    "count": _csv_int(row["count"].strip(), f"{where}, count", found),
                }
            )
        else:
            checklist = _checklist_named(checklists, row, where, found)
            if checklist is None:
                continue
            if record == "item":
                checklist["items"].append(_csv_item(row, where, found))
            elif record == "inspects":
                checklist["kits"].append(row["kit"].strip())
            else:
                checklist["schedules"].append(row["schedule"].strip())
    if vehicle is None:
        found.add("file", "no vehicle row")
        vehicle = {}
    found.raise_if_any()
    return {
        "schema_version": SCHEMA_VERSION,
        **vehicle,
        **sections,
        "checklists": list(checklists.values()),
    }


# --- rendering -------------------------------------------------------------------------------


def _item_document(entry):
    position = {}
    if entry.get("compartment"):
        position = {"compartment": entry["compartment"]}
    elif entry.get("system"):
        position = {"system": entry["system"]}
    return {
        "item": entry["item"],
        **position,
        "kind": entry["kind"],
        "quantity": entry["expected_quantity"],
        "unit": entry["unit"],
        "outcomes": entry["allowed_outcomes"],
        "expiration_tracked": entry.get("expiration_tracked", False),
        "instructions": entry["instructions"],
    }


def _json_document(vehicle):
    return {
        "file_type": FILE_TYPE,
        "schema_version": SCHEMA_VERSION,
        "vehicle": {
            "vin": vehicle["vin"],
            "display_name": vehicle["display_name"],
            "number": vehicle["number"],
            "compartments": [dict(p) for p in vehicle["compartments"]],
            "systems": [dict(p) for p in vehicle["systems"]],
            "checklists": [
                {
                    "id": c["id"],
                    "label": c["label"],
                    "schedules": list(c["schedules"]),
                    "kits": list(c["kits"]),
                    "items": [_item_document(e) for e in c["items"]],
                }
                for c in vehicle["checklists"]
            ],
            "kits": [dict(k) for k in vehicle["kits"]],
        },
    }


def render(vehicle, fmt):
    """A schema 2 vehicle file for a vehicle in normal form, as UTF-8 bytes."""
    if fmt == "json":
        document = _json_document(vehicle)
        return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if fmt != "csv":
        raise ValueError("format must be json or csv")
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)

    def row(record, **cells):
        values = {c: "" for c in CSV_COLUMNS} | {"file_type": FILE_TYPE, "record": record}
        values |= {k: "" if v is None else str(v) for k, v in cells.items()}
        writer.writerow([_neutralize(values[c]) for c in CSV_COLUMNS])

    row(
        "vehicle",
        vin=vehicle["vin"],
        display_name=vehicle["display_name"],
        number=vehicle["number"],
    )
    for record, name in (("compartment", "compartments"), ("system", "systems")):
        for position in vehicle[name]:
            row(record, key=position["key"], label=position["label"])
    for checklist in vehicle["checklists"]:
        local = checklist["id"]
        row("checklist", checklist=local, label=checklist["label"])
        for slot in checklist["schedules"]:
            row("assignment", checklist=local, schedule=slot)
        for kit in checklist["kits"]:
            row("inspects", checklist=local, kit=kit)
        for entry in checklist["items"]:
            row(
                "item",
                checklist=local,
                item=entry["item"],
                compartment=entry.get("compartment"),
                system=entry.get("system"),
                kind=entry["kind"],
                quantity=entry["expected_quantity"],
                unit=entry["unit"],
                outcomes="|".join(entry["allowed_outcomes"]),
                expiration_tracked="yes" if entry.get("expiration_tracked") else "no",
                instructions=entry["instructions"],
            )
    for kit in vehicle["kits"]:
        row("kit", kit=kit["kit"], compartment=kit["compartment"], count=kit["count"])
    return out.getvalue().encode("utf-8")


TEMPLATE_VEHICLE = {
    "schema_version": SCHEMA_VERSION,
    "vin": "1FDXE4FS8KDC00001",  # synthetic
    "display_name": "Example Unit 1",
    "number": "EX-1",
    "compartments": [
        {"key": "example-cabinet-1", "label": "Example cabinet 1"},
        {"key": "example-drawer-2", "label": "Example drawer 2"},
    ],
    "systems": [{"key": "example-lights", "label": "Example lights"}],
    "checklists": [
        {
            "id": "daily",
            "label": "Example daily check",
            "schedules": ["example-0600"],
            "kits": ["example-airway-kit"],
            "items": [
                {
                    "item": "Example suction unit",
                    "compartment": "example-cabinet-1",
                    "system": None,
                    "kind": "PRESENCE_CONDITION",
                    "expected_quantity": None,
                    "unit": None,
                    "allowed_outcomes": list(DEFAULT_OUTCOMES),
                    "expiration_tracked": False,
                    "instructions": "",
                },
                {
                    "item": "Example spare fuse",  # Not placed: still checked (CHK-9)
                    "compartment": None,
                    "system": None,
                    "kind": "PRESENCE_CONDITION",
                    "expected_quantity": None,
                    "unit": None,
                    "allowed_outcomes": list(DEFAULT_OUTCOMES),
                    "expiration_tracked": False,
                    "instructions": "",
                },
            ],
        }
    ],
    "kits": [
        {"kit": "example-airway-kit", "compartment": "example-cabinet-1", "count": 2},
    ],
}
