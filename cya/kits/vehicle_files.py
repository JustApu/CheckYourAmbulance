"""Vehicle files: parse and render JSON and CSV (SPEC 2.1.0 4.7 IMP-1, IMP-4, IMP-6, IMP-7).

Pure functions, no database access, in the style of cya.kits.library_files (whose CSV
neutralization, size bound and error collection they reuse). A vehicle file holds exactly one
vehicle. Records are named by stable keys (IMP-6) and labels, never by database ids.

The normal form is one dict:
  vin, display_name, number (or None), template (template key), schedule (schedule slot or None),
  compartments / systems: [{"key", "label"}], in file order,
  items: [{"item" (label), "compartment" or "system" (position key), "kind",
           "expected_quantity", "unit", "allowed_outcomes", "instructions"}], in file order,
  kits:  [{"kit" (kit definition key), "compartment" (position key), "count"}], in file order.

CSV has one row per record, the record type in the "record" column (vehicle, compartment,
system, item or kit); a column a record type does not use is left empty. Every row's file_type
is "vehicle".
"""

import csv
import io
import json

from cya.kits.library_files import (
    DEFAULT_OUTCOMES,
    FORMATS,
    FileErrors,
    _Collector,
    _csv_int,
    _neutralize,
    _text,
    _unneutralize,
)

FILE_TYPE = "vehicle"
SCHEMA_VERSION = 1
MAX_CSV_ROWS = 20_000
RECORDS = ("vehicle", "compartment", "system", "item", "kit")
CSV_COLUMNS = (
    "file_type",
    "record",
    "vin",
    "display_name",
    "number",
    "template",
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
    "instructions",
    "kit",
    "count",
)
VEHICLE_FIELDS = frozenset(
    {
        "vin",
        "display_name",
        "number",
        "template",
        "schedule",
        "compartments",
        "systems",
        "items",
        "kits",
    }
)
POSITION_FIELDS = frozenset({"key", "label"})
ITEM_FIELDS = frozenset(
    {"item", "compartment", "system", "kind", "quantity", "unit", "outcomes", "instructions"}
)
KIT_FIELDS = frozenset({"kit", "compartment", "count"})


def parse(data, fmt):
    """The vehicle in an uploaded file, in normal form, or FileErrors listing every problem."""
    if fmt not in FORMATS:
        raise FileErrors([f"file: format must be one of {list(FORMATS)}"])
    text = _text(data)
    return _parse_json(text) if fmt == "json" else _parse_csv(text)


# --- JSON ------------------------------------------------------------------------------------


def _object(raw, fields, where, found):
    if not isinstance(raw, dict):
        found.add(where, "must be an object")
        return {}
    unknown = set(raw) - fields
    if unknown:
        found.add(where, f"unknown fields {sorted(unknown)}")
    return raw


def _list(raw, where, found):
    if raw is None:
        return []
    if not isinstance(raw, list):
        found.add(where, "must be a list")
        return []
    return raw


def _json_item(raw, where, found):
    raw = _object(raw, ITEM_FIELDS, where, found)
    return {
        "item": raw.get("item"),
        "compartment": raw.get("compartment"),
        "system": raw.get("system"),
        "kind": raw.get("kind"),
        "expected_quantity": raw.get("quantity"),
        "unit": raw.get("unit"),
        "allowed_outcomes": raw.get("outcomes", list(DEFAULT_OUTCOMES)),
        "instructions": raw.get("instructions", ""),
    }


def _parse_json(text):
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise FileErrors(
            [f"file: not valid JSON (line {error.lineno}, column {error.colno})"]
        ) from None
    found = _Collector()
    if not isinstance(document, dict):
        raise FileErrors(["file: the top level must be an object"])
    if document.get("file_type") != FILE_TYPE:
        found.add("file_type", f"must be {FILE_TYPE!r}")
    if document.get("schema_version") != SCHEMA_VERSION:
        found.add("schema_version", f"must be {SCHEMA_VERSION}")
    unknown = set(document) - {"file_type", "schema_version", "vehicle"}
    if unknown:
        found.add("file", f"unknown fields {sorted(unknown)}")
    raw = _object(document.get("vehicle"), VEHICLE_FIELDS, "vehicle", found)
    vehicle = {
        "vin": raw.get("vin"),
        "display_name": raw.get("display_name"),
        "number": raw.get("number"),
        "template": raw.get("template"),
        "schedule": raw.get("schedule"),
    }
    for name in ("compartments", "systems"):
        vehicle[name] = [
            {
                "key": p.get("key"),
                "label": p.get("label"),
            }
            for i, entry in enumerate(_list(raw.get(name), f"vehicle.{name}", found))
            for p in [_object(entry, POSITION_FIELDS, f"vehicle.{name}[{i}]", found)]
        ]
    vehicle["items"] = [
        _json_item(entry, f"vehicle.items[{i}]", found)
        for i, entry in enumerate(_list(raw.get("items"), "vehicle.items", found))
    ]
    vehicle["kits"] = [
        {"kit": k.get("kit"), "compartment": k.get("compartment"), "count": k.get("count")}
        for i, entry in enumerate(_list(raw.get("kits"), "vehicle.kits", found))
        for k in [_object(entry, KIT_FIELDS, f"vehicle.kits[{i}]", found)]
    ]
    found.raise_if_any()
    return vehicle


# --- CSV -------------------------------------------------------------------------------------


def _csv_rows(text):
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header is None:
            raise FileErrors(["file: the file has no header row"])
        if len(set(header)) != len(header) or set(header) != set(CSV_COLUMNS):
            missing = sorted(set(CSV_COLUMNS) - set(header))
            extra = sorted(set(header) - set(CSV_COLUMNS))
            raise FileErrors(
                [
                    f"row 1: header must be exactly {list(CSV_COLUMNS)} "
                    f"(missing {missing}, unknown {extra}, or repeated)"
                ]
            )
        rows, problems = [], []
        for number, values in enumerate(reader, start=2):
            if number - 1 > MAX_CSV_ROWS:
                raise FileErrors([f"file: more than {MAX_CSV_ROWS} rows"])
            if not any(v.strip() for v in values):
                continue  # a blank line
            if len(values) != len(header):
                problems.append(f"row {number}: has {len(values)} cells, expected {len(header)}")
                continue
            rows.append(
                (number, {h: _unneutralize(v) for h, v in zip(header, values, strict=True)})
            )
    except csv.Error as error:
        raise FileErrors([f"file: not valid CSV ({error})"]) from None
    return rows, problems


# Which columns each record type uses; any other non-empty cell is an error.
RECORD_COLUMNS = {
    "vehicle": {"vin", "display_name", "number", "template", "schedule"},
    "compartment": {"key", "label"},
    "system": {"key", "label"},
    "item": {
        "item",
        "compartment",
        "system",
        "kind",
        "quantity",
        "unit",
        "outcomes",
        "instructions",
    },
    "kit": {"kit", "compartment", "count"},
}


def _parse_csv(text):
    found = _Collector()
    rows, problems = _csv_rows(text)
    for problem in problems:
        found.errors.append(problem)
    vehicle = None
    sections = {"compartments": [], "systems": [], "items": [], "kits": []}
    for number, row in rows:
        where = f"row {number}"
        if row["file_type"] != FILE_TYPE:
            found.add(f"{where}, file_type", f"must be {FILE_TYPE!r}")
        record = row["record"].strip()
        if record not in RECORDS:
            found.add(f"{where}, record", f"must be one of {list(RECORDS)}")
            continue
        stray = sorted(
            c
            for c in CSV_COLUMNS
            if c not in ("file_type", "record")
            and c not in RECORD_COLUMNS[record]
            and row[c].strip()
        )
        if stray:
            found.add(where, f"a {record} row leaves {stray} empty")
        if record == "vehicle":
            if vehicle is not None:
                found.add(where, "a vehicle file has exactly one vehicle row")
                continue
            vehicle = {
                "vin": row["vin"].strip(),
                "display_name": row["display_name"].strip(),
                "number": row["number"].strip() or None,
                "template": row["template"].strip(),
                "schedule": row["schedule"].strip() or None,
            }
        elif record in ("compartment", "system"):
            sections[f"{record}s"].append(
                {"key": row["key"].strip(), "label": row["label"].strip()}
            )
        elif record == "item":
            outcomes = (
                [o.strip() for o in row["outcomes"].split("|")]
                if row["outcomes"].strip()
                else list(DEFAULT_OUTCOMES)
            )
            sections["items"].append(
                {
                    "item": row["item"].strip(),
                    "compartment": row["compartment"].strip() or None,
                    "system": row["system"].strip() or None,
                    "kind": row["kind"].strip(),
                    "expected_quantity": _csv_int(
                        row["quantity"].strip(), f"{where}, quantity", found
                    ),
                    "unit": row["unit"].strip() or None,
                    "allowed_outcomes": outcomes,
                    "instructions": row["instructions"],
                }
            )
        else:
            sections["kits"].append(
                {
                    "kit": row["kit"].strip(),
                    "compartment": row["compartment"].strip(),
                    "count": _csv_int(row["count"].strip(), f"{where}, count", found),
                }
            )
    if vehicle is None:
        found.add("file", "no vehicle row")
        vehicle = {}
    found.raise_if_any()
    return {**vehicle, **sections}


# --- rendering -------------------------------------------------------------------------------


def _json_document(vehicle):
    def item(entry):
        position = (
            {"compartment": entry["compartment"]}
            if entry.get("compartment")
            else {"system": entry["system"]}
        )
        return {
            "item": entry["item"],
            **position,
            "kind": entry["kind"],
            "quantity": entry["expected_quantity"],
            "unit": entry["unit"],
            "outcomes": entry["allowed_outcomes"],
            "instructions": entry["instructions"],
        }

    return {
        "file_type": FILE_TYPE,
        "schema_version": SCHEMA_VERSION,
        "vehicle": {
            "vin": vehicle["vin"],
            "display_name": vehicle["display_name"],
            "number": vehicle["number"],
            "template": vehicle["template"],
            "schedule": vehicle["schedule"],
            "compartments": [dict(p) for p in vehicle["compartments"]],
            "systems": [dict(p) for p in vehicle["systems"]],
            "items": [item(e) for e in vehicle["items"]],
            "kits": [dict(k) for k in vehicle["kits"]],
        },
    }


def render(vehicle, fmt):
    """A vehicle file for a vehicle in normal form, as UTF-8 bytes."""
    if fmt == "json":
        document = _json_document(vehicle)
        return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if fmt != "csv":
        raise ValueError(f"format must be one of {list(FORMATS)}")
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
        template=vehicle["template"],
        schedule=vehicle["schedule"],
    )
    for record, name in (("compartment", "compartments"), ("system", "systems")):
        for position in vehicle[name]:
            row(record, key=position["key"], label=position["label"])
    for entry in vehicle["items"]:
        row(
            "item",
            item=entry["item"],
            compartment=entry.get("compartment"),
            system=entry.get("system"),
            kind=entry["kind"],
            quantity=entry["expected_quantity"],
            unit=entry["unit"],
            outcomes="|".join(entry["allowed_outcomes"]),
            instructions=entry["instructions"],
        )
    for kit in vehicle["kits"]:
        row("kit", kit=kit["kit"], compartment=kit["compartment"], count=kit["count"])
    return out.getvalue().encode("utf-8")


TEMPLATE_VEHICLE = {
    "vin": "1FDXE4FS8KDC00001",  # synthetic
    "display_name": "Example Unit 1",
    "number": "EX-1",
    "template": "example-daily-check",
    "schedule": None,
    "compartments": [
        {"key": "example-cabinet-1", "label": "Example cabinet 1"},
        {"key": "example-drawer-2", "label": "Example drawer 2"},
    ],
    "systems": [{"key": "example-lights", "label": "Example lights"}],
    "items": [
        {
            "item": "Example suction unit",
            "compartment": "example-cabinet-1",
            "system": None,
            "kind": "PRESENCE_CONDITION",
            "expected_quantity": None,
            "unit": None,
            "allowed_outcomes": list(DEFAULT_OUTCOMES),
            "instructions": "",
        },
        {
            "item": "Example warning lights",
            "compartment": None,
            "system": "example-lights",
            "kind": "PRESENCE_CONDITION",
            "expected_quantity": None,
            "unit": None,
            "allowed_outcomes": list(DEFAULT_OUTCOMES),
            "instructions": "Switch on and walk around the vehicle.",
        },
    ],
    "kits": [
        {"kit": "example-airway-kit", "compartment": "example-cabinet-1", "count": 2},
    ],
}


def template(fmt):
    """A blank vehicle file with one synthetic example vehicle (IMP-4)."""
    return render(TEMPLATE_VEHICLE, fmt)
