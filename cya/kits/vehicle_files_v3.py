"""Vehicle files with the vehicle's item requirements (SPEC 6.0.0 IMP-7, CHK-11; issue #256 C4).

Pure functions, no database access, in the style of cya.kits.vehicle_files_v2, whose helpers
they reuse. Schema version 3 adds the vehicle's item requirements as records of their own:
  requirements: [{"id", "item", "compartment", "system", "kind", "quantity", "unit",
                  "outcomes", "expiration_tracked", "instructions"}], in file order,
and a checklist's items name them: items: [{"requirement": id}], in display order. An id is
local to the file (IMP-6). Compartment and system both None mean Not placed (CHK-9). A file
holds at most one requirement per item and place (SPEC 18.1 item 8), so two checklists that
inspect an item in one place share its requirement (AC-42 (1)).

The normal form is schema 2's, with schema_version 3, the requirements list (each in the item
dict shape of cya.kits.vehicle_files plus its "id"), and each checklist item expanded to its
requirement's item dict plus "requirement" (the id), so the import plan reads both schemas the
same way.

CSV adds a "requirement" column and record type: a requirement row carries the item columns
and its id; an item row names its checklist and requirement.
"""

import csv
import io
import json

from cya.kits import vehicle_files_v2 as v2
from cya.kits.library_files import FileErrors, _Collector, _neutralize
from cya.kits.vehicle_files import (
    FILE_TYPE,
    ITEM_FIELDS,
    KIT_FIELDS,
    MAX_CSV_ROWS,
    POSITION_FIELDS,
    _csv_rows,
    _json_item,
    _list,
    _object,
)

SCHEMA_VERSION = 3
CSV_COLUMNS = (*v2.CSV_COLUMNS, "requirement")
RECORD_COLUMNS = {
    **{k: v for k, v in v2.RECORD_COLUMNS.items() if k != "item"},
    "requirement": (v2.RECORD_COLUMNS["item"] - {"checklist"}) | {"requirement"},
    "item": {"checklist", "requirement"},
}
VEHICLE_FIELDS = v2.VEHICLE_FIELDS | {"requirements"}
REQUIREMENT_FIELDS = ITEM_FIELDS | {"id"}


def is_v3_csv(text):
    """Whether a CSV file's header is the schema 3 header (it names a requirement column)."""
    first = text.split("\n", 1)[0]
    try:
        header = next(csv.reader([first]))
    except csv.Error:
        return False
    return "requirement" in [h.strip() for h in header]


def _place(entry):
    return (entry.get("compartment") or None, entry.get("system") or None)


def expand(vehicle, found):
    """Give each checklist item its requirement's values; report ids and duplicates."""
    by_id, places = {}, {}
    for index, requirement in enumerate(vehicle["requirements"]):
        where = f"requirements[{index}]"
        local = requirement.get("id")
        if not isinstance(local, str) or not local.strip():
            found.add(f"{where}.id", "each requirement needs its own id in the file")
            continue
        if local in by_id:
            found.add(f"{where}.id", f"{local!r} is listed twice")
            continue
        key = (requirement.get("item"), _place(requirement))
        if key in places:
            found.add(
                where,
                f"{requirement.get('item')!r} already has requirement {places[key]!r} in that "
                "place; a vehicle has one item requirement per item and place",
            )
            continue
        by_id[local], places[key] = requirement, local
    for c, checklist in enumerate(vehicle["checklists"]):
        expanded = []
        for i, entry in enumerate(checklist["items"]):
            local = entry.get("requirement")
            requirement = by_id.get(local) if isinstance(local, str) else None
            if requirement is None:
                found.add(f"checklists[{c}].items[{i}].requirement", "no requirement has that id")
                continue
            expanded.append(
                {**{k: v for k, v in requirement.items() if k != "id"}, "requirement": local}
            )
        checklist["items"] = expanded
    return vehicle


# --- JSON ------------------------------------------------------------------------------------


def _json_requirement(raw, where, found):
    raw = _object(raw, REQUIREMENT_FIELDS, where, found)
    return {
        "id": raw.get("id"),
        **_json_item({k: v for k, v in raw.items() if k != "id"}, where, found),
    }


def _json_checklist(raw, where, found):
    raw = _object(raw, v2.CHECKLIST_FIELDS, where, found)
    return {
        "id": raw.get("id"),
        "label": raw.get("label"),
        "schedules": list(_list(raw.get("schedules"), f"{where}.schedules", found)),
        "kits": list(_list(raw.get("kits"), f"{where}.kits", found)),
        "items": [
            {
                "requirement": _object(entry, {"requirement"}, f"{where}.items[{i}]", found).get(
                    "requirement"
                )
            }
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
    vehicle["requirements"] = [
        _json_requirement(entry, f"vehicle.requirements[{i}]", found)
        for i, entry in enumerate(_list(raw.get("requirements"), "vehicle.requirements", found))
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
    expand(vehicle, found)
    found.raise_if_any()
    return vehicle


# --- CSV -------------------------------------------------------------------------------------


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


def parse_csv(text):
    rows, problems = _csv_rows(text, CSV_COLUMNS)
    found = _Collector()
    found.errors.extend(problems)
    if len(rows) > MAX_CSV_ROWS:
        raise FileErrors([f"file: more than {MAX_CSV_ROWS} rows"])
    vehicle, checklists, requirements = None, {}, []
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
        elif record == "requirement":
            requirements.append(
                {"id": row["requirement"].strip(), **v2._csv_item(row, where, found)}
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
                    "count": v2._csv_int(row["count"].strip(), f"{where}, count", found),
                }
            )
        else:
            checklist = v2._checklist_named(checklists, row, where, found)
            if checklist is None:
                continue
            if record == "item":
                checklist["items"].append({"requirement": row["requirement"].strip()})
            elif record == "inspects":
                checklist["kits"].append(row["kit"].strip())
            else:
                checklist["schedules"].append(row["schedule"].strip())
    if vehicle is None:
        found.add("file", "no vehicle row")
        vehicle = {}
    found.raise_if_any()
    result = {
        "schema_version": SCHEMA_VERSION,
        **vehicle,
        **sections,
        "requirements": requirements,
        "checklists": list(checklists.values()),
    }
    expand(result, found)
    found.raise_if_any()
    return result


# --- rendering -------------------------------------------------------------------------------


def _requirement_document(entry):
    return {"id": entry["id"], **v2._item_document(entry)}


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
            "requirements": [_requirement_document(r) for r in vehicle["requirements"]],
            "checklists": [
                {
                    "id": c["id"],
                    "label": c["label"],
                    "schedules": list(c["schedules"]),
                    "kits": list(c["kits"]),
                    "items": [{"requirement": e["requirement"]} for e in c["items"]],
                }
                for c in vehicle["checklists"]
            ],
            "kits": [dict(k) for k in vehicle["kits"]],
        },
    }


def render(vehicle, fmt):
    """A schema 3 vehicle file for a vehicle in normal form, as UTF-8 bytes."""
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
    for entry in vehicle["requirements"]:
        row(
            "requirement",
            requirement=entry["id"],
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
    for checklist in vehicle["checklists"]:
        local = checklist["id"]
        row("checklist", checklist=local, label=checklist["label"])
        for slot in checklist["schedules"]:
            row("assignment", checklist=local, schedule=slot)
        for kit in checklist["kits"]:
            row("inspects", checklist=local, kit=kit)
        for entry in checklist["items"]:
            row("item", checklist=local, requirement=entry["requirement"])
    for kit in vehicle["kits"]:
        row("kit", kit=kit["kit"], compartment=kit["compartment"], count=kit["count"])
    return out.getvalue().encode("utf-8")


def from_v2(vehicle, more=()):
    """Schema 2 normal form to schema 3: one requirement per item and place among the items,
    in first-listed order, plus `more` (requirement item dicts no checklist inspects).

    The caller guarantees that items of one item and place carry equal values (the export
    takes them from the requirement in force, CHK-5).
    """
    ids, requirements = {}, []

    def add(entry):
        key = (entry["item"], _place(entry))
        if key not in ids:
            ids[key] = f"requirement-{len(ids) + 1}"
            requirements.append(
                {"id": ids[key], **{k: v for k, v in entry.items() if k != "requirement"}}
            )
        return ids[key]

    checklists = [
        {**c, "items": [{**e, "requirement": add(e)} for e in c["items"]]}
        for c in vehicle["checklists"]
    ]
    for entry in more:
        add(entry)
    return {
        **vehicle,
        "schema_version": SCHEMA_VERSION,
        "requirements": requirements,
        "checklists": checklists,
    }


TEMPLATE_VEHICLE = from_v2(v2.TEMPLATE_VEHICLE)
