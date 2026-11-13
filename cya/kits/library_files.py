"""Kit-library files: parse and render JSON and CSV (SPEC 4.7 IMP-1, IMP-4; issue #129).

Pure functions, no database access. parse() turns an uploaded file into a list of kits in one
normal form and collects every problem with its location (a JSON path or a CSV row and
column), so a preview can show them all at once. render() writes the same normal form back in
either format, so an export re-imports unchanged.

The normal form of one kit is {"key", "label", "contents": [entry, ...]}; an entry is
{"item", "kind", "expected_quantity", "unit", "allowed_outcomes", "expiration_tracked",
"instructions"}, in file order. "item" is the item's label: files never carry database ids.

CSV cells that a spreadsheet would run as a formula (starting with = + - @, tab or carriage
return) are written with a leading apostrophe (RPT-2), and that apostrophe is removed again on
import, so a round trip is exact.
"""

import csv
import io
import json

FILE_TYPE = "kit-library"
SCHEMA_VERSION = 1
MAX_BYTES = 1_000_000
MAX_KITS = 200
MAX_CONTENTS = 500
MAX_CSV_ROWS = 20_000
MAX_ERRORS = 200
DEFAULT_OUTCOMES = ["PASSED", "FAILED", "UNABLE_TO_INSPECT"]
CSV_COLUMNS = (
    "file_type",
    "kit_key",
    "kit_label",
    "item",
    "kind",
    "quantity",
    "unit",
    "outcomes",
    "expiration_tracked",
    "instructions",
)
JSON_KIT_FIELDS = frozenset({"key", "label", "contents"})
JSON_CONTENT_FIELDS = frozenset(
    {"item", "kind", "quantity", "unit", "outcomes", "expiration_tracked", "instructions"}
)
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
FORMATS = ("json", "csv")


class FileErrors(ValueError):
    """The file could not be read as a kit library; .errors lists every problem found."""

    def __init__(self, errors):
        super().__init__(f"{len(errors)} problem(s) in the file")
        self.errors = errors


class _Collector:
    def __init__(self):
        self.errors = []

    def add(self, where, message):
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(f"{where}: {message}")

    def raise_if_any(self):
        if self.errors:
            raise FileErrors(self.errors)


def _text(data):
    if not isinstance(data, bytes):
        raise FileErrors(["file: expected the uploaded bytes"])
    if not data:
        raise FileErrors(["file: the file is empty"])
    if len(data) > MAX_BYTES:
        raise FileErrors([f"file: larger than {MAX_BYTES} bytes"])
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FileErrors([f"file: not UTF-8 text (byte {error.start})"]) from None


def parse(data, fmt):
    """The kits in an uploaded file, in normal form, or FileErrors listing every problem."""
    if fmt not in FORMATS:
        raise FileErrors([f"file: format must be one of {list(FORMATS)}"])
    text = _text(data)
    return _parse_json(text) if fmt == "json" else _parse_csv(text)


# --- JSON ------------------------------------------------------------------------------------


def _json_entry(raw, where, found):
    if not isinstance(raw, dict):
        found.add(where, "must be an object")
        return None
    unknown = set(raw) - JSON_CONTENT_FIELDS
    if unknown:
        found.add(where, f"unknown fields {sorted(unknown)}")
    return {
        "item": raw.get("item"),
        "kind": raw.get("kind"),
        "expected_quantity": raw.get("quantity"),
        "unit": raw.get("unit"),
        "allowed_outcomes": raw.get("outcomes", list(DEFAULT_OUTCOMES)),
        "expiration_tracked": raw.get("expiration_tracked", False),
        "instructions": raw.get("instructions", ""),
    }


def _json_kit(raw, where, found):
    if not isinstance(raw, dict):
        found.add(where, "must be an object")
        return None
    unknown = set(raw) - JSON_KIT_FIELDS
    if unknown:
        found.add(where, f"unknown fields {sorted(unknown)}")
    contents = raw.get("contents")
    if not isinstance(contents, list) or not 1 <= len(contents) <= MAX_CONTENTS:
        found.add(f"{where}.contents", f"must be a list of 1-{MAX_CONTENTS} entries")
        contents = []
    entries = [_json_entry(c, f"{where}.contents[{i}]", found) for i, c in enumerate(contents)]
    return {"key": raw.get("key"), "label": raw.get("label"), "contents": entries}


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
    unknown = set(document) - {"file_type", "schema_version", "kits"}
    if unknown:
        found.add("file", f"unknown fields {sorted(unknown)}")
    kits = document.get("kits")
    if not isinstance(kits, list) or not 1 <= len(kits) <= MAX_KITS:
        found.add("kits", f"must be a list of 1-{MAX_KITS} kits")
        kits = []
    parsed = [_json_kit(k, f"kits[{i}]", found) for i, k in enumerate(kits)]
    found.raise_if_any()
    return parsed


# --- CSV -------------------------------------------------------------------------------------


def _unneutralize(value):
    if len(value) >= 2 and value[0] == "'" and value[1:].startswith(FORMULA_STARTS):
        return value[1:]
    return value


def _csv_rows(text, found):
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
                    f"row 1: header must be exactly {list(CSV_COLUMNS)} (missing {missing}, unknown {extra}, or repeated)"
                ]
            )
        rows = []
        for number, values in enumerate(reader, start=2):
            if number - 1 > MAX_CSV_ROWS:
                raise FileErrors([f"file: more than {MAX_CSV_ROWS} rows"])
            if not any(v.strip() for v in values):
                continue  # a blank line
            if len(values) != len(header):
                found.add(f"row {number}", f"has {len(values)} cells, expected {len(header)}")
                continue
            rows.append(
                (number, {h: _unneutralize(v) for h, v in zip(header, values, strict=True)})
            )
    except csv.Error as error:
        raise FileErrors([f"file: not valid CSV ({error})"]) from None
    return rows


def _csv_int(value, where, found):
    if value == "":
        return None
    if not value.isdigit():
        found.add(where, "must be a whole number")
        return None
    return int(value)


def _csv_bool(value, where, found):
    lowered = value.strip().lower()
    if lowered in ("", "no", "false"):
        return False
    if lowered in ("yes", "true"):
        return True
    found.add(where, "must be yes or no")
    return False


def _csv_entry(number, row, found):
    outcomes = (
        [o.strip() for o in row["outcomes"].split("|")]
        if row["outcomes"].strip()
        else list(DEFAULT_OUTCOMES)
    )
    return {
        "item": row["item"].strip(),
        "kind": row["kind"].strip(),
        "expected_quantity": _csv_int(row["quantity"].strip(), f"row {number}, quantity", found),
        "unit": row["unit"].strip() or None,
        "allowed_outcomes": outcomes,
        "expiration_tracked": _csv_bool(
            row["expiration_tracked"], f"row {number}, expiration_tracked", found
        ),
        "instructions": row["instructions"],
    }


def _parse_csv(text):
    found = _Collector()
    rows = _csv_rows(text, found)
    kits, labels = {}, {}
    for number, row in rows:
        if row["file_type"] != FILE_TYPE:
            found.add(f"row {number}, file_type", f"must be {FILE_TYPE!r}")
        key, label = row["kit_key"].strip(), row["kit_label"].strip()
        if key not in kits:
            if len(kits) == MAX_KITS:
                found.add(f"row {number}", f"more than {MAX_KITS} kits")
                continue
            kits[key], labels[key] = [], label
        elif label != labels[key]:
            found.add(f"row {number}, kit_label", f"differs from the first row of kit {key!r}")
        if len(kits[key]) == MAX_CONTENTS:
            found.add(f"row {number}", f"kit {key!r} has more than {MAX_CONTENTS} entries")
            continue
        kits[key].append(_csv_entry(number, row, found))
    if not kits:
        found.add("file", "no kit rows")
    found.raise_if_any()
    return [{"key": k, "label": labels[k], "contents": c} for k, c in kits.items()]


# --- rendering -------------------------------------------------------------------------------


def _neutralize(value):
    return "'" + value if value.startswith(FORMULA_STARTS) else value


def render(kits, fmt):
    """A kit-library file for kits in normal form, as UTF-8 bytes."""
    if fmt == "json":
        document = {
            "file_type": FILE_TYPE,
            "schema_version": SCHEMA_VERSION,
            "kits": [
                {
                    "key": kit["key"],
                    "label": kit["label"],
                    "contents": [
                        {
                            "item": e["item"],
                            "kind": e["kind"],
                            "quantity": e["expected_quantity"],
                            "unit": e["unit"],
                            "outcomes": e["allowed_outcomes"],
                            "expiration_tracked": e["expiration_tracked"],
                            "instructions": e["instructions"],
                        }
                        for e in kit["contents"]
                    ],
                }
                for kit in kits
            ],
        }
        return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if fmt != "csv":
        raise ValueError(f"format must be one of {list(FORMATS)}")
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for kit in kits:
        for e in kit["contents"]:
            cells = [
                FILE_TYPE,
                kit["key"],
                kit["label"],
                e["item"],
                e["kind"],
                "" if e["expected_quantity"] is None else str(e["expected_quantity"]),
                e["unit"] or "",
                "|".join(e["allowed_outcomes"]),
                "yes" if e["expiration_tracked"] else "no",
                e["instructions"],
            ]
            writer.writerow([_neutralize(c) for c in cells])
    return out.getvalue().encode("utf-8")


TEMPLATE_KITS = [
    {
        "key": "example-airway-kit",
        "label": "Example airway kit",
        "contents": [
            {
                "item": "Example adult masks",
                "kind": "QUANTITY",
                "expected_quantity": 2,
                "unit": None,
                "allowed_outcomes": ["PASSED", "FAILED"],
                "expiration_tracked": True,
                "instructions": "Count unopened packages; note the earliest expiration.",
            },
            {
                "item": "Example suction catheter",
                "kind": "PRESENCE_CONDITION",
                "expected_quantity": None,
                "unit": None,
                "allowed_outcomes": list(DEFAULT_OUTCOMES),
                "expiration_tracked": False,
                "instructions": "",
            },
        ],
    }
]


def template(fmt):
    """A blank kit-library file with one synthetic example kit (IMP-4)."""
    return render(TEMPLATE_KITS, fmt)
