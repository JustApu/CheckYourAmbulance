#!/usr/bin/env python3
"""Validate the stack-neutral acceptance-scenario library under tests/.

Read-only structural and coverage checks (format defined in tests/README.md):
  * every scenario, contract and fixture file parses as JSON;
  * each scenario has exactly the documented fields, with sane values;
  * AC-* scenarios, tests/TRACEABILITY.md and the step catalog agree;
  * every cited requirement ID is defined in SPEC.md;
  * every SPEC.md section 23 criterion is in the step catalog, except the
    documented KNOWN_UNCOVERED gaps;
  * permission-matrix rows have unique PERM-NN IDs, and the permission
    cases cover every matrix row x role cell exactly once.

These checks do not exercise application behavior.

Usage: tools/validate_scenarios.py [--root DIR]
Exit status: 0 = valid, 1 = problems found, 2 = usage or I/O error.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# SPEC criteria that knowingly lack a step-catalog case (empty: none). Add an
# entry only with a tracking issue; the check fails if a listed criterion
# becomes covered, so this list cannot silently go stale.
KNOWN_UNCOVERED = frozenset()

REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "criterion",
        "requirements",
        "fixture",
        "synthetic",
        "given",
        "when",
        "then",
        "status",
    }
)
OPTIONAL_FIELDS = frozenset({"notes", "spec_refs", "steps", "database"})
CATALOG_CASE_FIELDS = frozenset({"setup", "actions", "assert", "requirements"})
AC_ID_RE = re.compile(r"^AC-([1-9][0-9]*)$")
AC_FILE_RE = re.compile(r"^AC-([0-9]{2,})$")
EX_FILE_RE = re.compile(r"^EX-[a-z0-9]+(?:-[a-z0-9]+)*$")
SPEC_AC_ROW_RE = re.compile(r"^\| (AC-[0-9]+) \|", re.M)
# Requirement IDs are numbered ("PRN-1", "INV-2a") or lettered ("STK-B"), and
# are defined as table rows ("| PRN-1 |") or bullets ("- **CON-4 [P]**").
REQ_ID = r"[A-Z]{2,5}-(?:[0-9]+[a-z]?|[A-Z])"
SPEC_REQ_DEF_RE = re.compile(rf"^(?:\| |- \*\*)({REQ_ID})\b", re.M)
TRACE_ROW_RE = re.compile(r"^\| (AC-[0-9]+) \| \[([^\]]+)\]\(([^)]+)\) \| ([^|]+) \|", re.M)
PERM_ROW_ID_RE = re.compile(r"^PERM-[0-9]{2,}$")
TRACE_REQ_RE = re.compile(rf"\b{REQ_ID}\b")


class Problems:
    """Collects validation problems so every one is reported, not just the first."""

    def __init__(self):
        self.items = []

    def add(self, where, message):
        self.items.append(f"{where}: {message}")


def load_json(path, problems, root):
    """Parse path as JSON; record a problem and return None if it fails."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        problems.add(path.relative_to(root), f"cannot read JSON: {error}")
        return None


def is_string_list(value, allow_empty=False):
    return (
        isinstance(value, list)
        and (allow_empty or len(value) > 0)
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def spec_section(spec_text, number):
    """Return the text of top-level section '## <number>.' up to the next '## '."""
    match = re.search(rf"^## {number}\..*?(?=^## |\Z)", spec_text, re.M | re.S)
    return match.group(0) if match else ""


def check_scenario(path, data, catalog, defined_reqs, problems, root):
    """Check one scenario file's fields; return its criterion or None."""
    where = path.relative_to(root)
    if not isinstance(data, dict):
        problems.add(where, "scenario must be a JSON object")
        return None
    keys = set(data)
    for missing in sorted(REQUIRED_FIELDS - keys):
        problems.add(where, f"missing field '{missing}'")
    for extra in sorted(keys - REQUIRED_FIELDS - OPTIONAL_FIELDS):
        problems.add(where, f"unknown field '{extra}'")

    if data.get("schema_version") != 1:
        problems.add(where, "schema_version must be 1")
    if data.get("synthetic") is not True:
        problems.add(where, "synthetic must be true")
    if data.get("status") not in ("contract", "bound"):
        problems.add(where, "status must be 'contract', or 'bound' once the runner binds its steps")
    for clause in ("given", "when", "then"):
        if not is_string_list(data.get(clause)):
            problems.add(where, f"'{clause}' must be a nonempty list of nonempty strings")

    requirements = data.get("requirements")
    if not is_string_list(requirements, allow_empty=True):
        problems.add(where, "'requirements' must be a list of nonempty strings")
        requirements = []
    for req in requirements:
        if req not in defined_reqs:
            problems.add(where, f"requirement '{req}' is not defined in SPEC.md")
    if "spec_refs" in data and not is_string_list(data["spec_refs"]):
        problems.add(where, "'spec_refs' must be a nonempty list of nonempty strings")
    if not requirements and "spec_refs" not in data:
        problems.add(where, "empty 'requirements' needs a nonempty 'spec_refs'")

    if "database" in data and data["database"] != "commit":
        problems.add(where, "'database' must be 'commit' when present")
    if "steps" in data:
        if not is_string_list(data["steps"]):
            problems.add(where, "'steps' must be a nonempty list of nonempty strings")
        if path.stem.startswith("AC-"):
            problems.add(
                where, "'steps' is only for EX-* scenarios; AC-* steps come from the catalog"
            )
    elif data.get("status") == "bound" and path.stem.startswith("EX-"):
        problems.add(where, "a bound EX-* scenario needs its own 'steps'")

    fixture = data.get("fixture")
    if not isinstance(fixture, str) or not (path.parent / fixture).is_file():
        problems.add(where, f"fixture '{fixture}' does not exist")

    stem = path.stem
    criterion = data.get("criterion")
    if not isinstance(criterion, str) or criterion not in catalog:
        problems.add(where, f"criterion {criterion!r} is not in the step catalog")
        criterion = None
    ac_file = AC_FILE_RE.match(stem)
    if ac_file:
        expected_id = f"AC-{int(ac_file.group(1))}"
        if data.get("id") != expected_id or criterion != expected_id:
            problems.add(where, f"id and criterion must both be '{expected_id}'")
        elif data.get("requirements") != catalog[expected_id].get("requirements"):
            problems.add(where, "requirements differ from the step catalog mapping")
    elif EX_FILE_RE.match(stem):
        if data.get("id") != stem:
            problems.add(where, f"id must equal the file name '{stem}'")
    else:
        problems.add(where, "file name must be AC-NN.json or EX-<kebab-case>.json")
    return criterion if ac_file else None


def check_catalog(catalog_path, catalog_data, defined_reqs, spec_acs, problems, root):
    """Check the step catalog; return its well-formed cases (empty on failure)."""
    where = catalog_path.relative_to(root)
    cases = catalog_data.get("cases") if isinstance(catalog_data, dict) else None
    if not isinstance(cases, dict) or not cases:
        problems.add(where, "'cases' must be a nonempty object")
        return {}
    valid = {}
    for case_id, case in cases.items():
        if not AC_ID_RE.match(case_id) or not isinstance(case, dict):
            problems.add(where, f"case '{case_id}' must be an AC-N id mapping to an object")
            continue
        valid[case_id] = case
        for missing in sorted(CATALOG_CASE_FIELDS - set(case)):
            problems.add(where, f"{case_id}: missing field '{missing}'")
        for field in ("setup", "actions", "assert"):
            if field in case and not is_string_list(case[field]):
                problems.add(where, f"{case_id}: '{field}' must be a nonempty list of strings")
        reqs = case.get("requirements", [])
        if not is_string_list(reqs, allow_empty=True):
            problems.add(where, f"{case_id}: 'requirements' must be a list of strings")
            continue
        if not reqs and not is_string_list(case.get("spec_refs")):
            problems.add(where, f"{case_id}: empty 'requirements' needs a nonempty 'spec_refs'")
        for req in reqs:
            if req not in defined_reqs:
                problems.add(where, f"{case_id}: requirement '{req}' is not defined in SPEC.md")
        if case_id not in spec_acs:
            problems.add(where, f"{case_id} is not a SPEC.md section 23 criterion")

    for ac in sorted(spec_acs - set(valid) - KNOWN_UNCOVERED, key=ac_number):
        problems.add(where, f"SPEC.md criterion {ac} has no step-catalog case")
    for ac in sorted(KNOWN_UNCOVERED & set(valid), key=ac_number):
        problems.add(
            "tools/validate_scenarios.py", f"{ac} is now covered; remove it from KNOWN_UNCOVERED"
        )
    return valid


def ac_number(ac_id):
    return int(ac_id.split("-")[1])


def check_traceability(trace_path, cases, problems, root):
    """Check that TRACEABILITY.md has one row per catalog case, matching it."""
    where = trace_path.relative_to(root)
    try:
        text = trace_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        problems.add(where, f"cannot read: {error}")
        return
    rows = {}
    for criterion, label, link, refs in TRACE_ROW_RE.findall(text):
        if criterion in rows:
            problems.add(where, f"duplicate row for {criterion}")
        rows[criterion] = (label, link, refs)
    for case_id, case in cases.items():
        if case_id not in rows:
            problems.add(where, f"no row for {case_id}")
            continue
        label, link, refs = rows[case_id]
        expected_file = f"AC-{ac_number(case_id):02d}.json"
        if label != expected_file or link != f"scenarios/{expected_file}":
            problems.add(where, f"{case_id} row must link scenarios/{expected_file}")
        if TRACE_REQ_RE.findall(refs) != list(case.get("requirements", [])):
            problems.add(where, f"{case_id} row requirements differ from the step catalog")
    for extra in sorted(set(rows) - set(cases), key=ac_number):
        problems.add(where, f"row {extra} has no step-catalog case")


def check_permissions(matrix_path, matrix, cases_path, perm_cases, problems, root):
    """Check that the expanded cases cover each matrix row x role exactly once."""
    m_where = matrix_path.relative_to(root)
    c_where = cases_path.relative_to(root)
    roles = matrix.get("roles") if isinstance(matrix, dict) else None
    rows = matrix.get("rows") if isinstance(matrix, dict) else None
    tests = perm_cases.get("tests") if isinstance(perm_cases, dict) else None
    if not is_string_list(roles) or not isinstance(rows, list) or not rows:
        problems.add(m_where, "'roles' and 'rows' must be nonempty lists")
        return
    if not isinstance(tests, list):
        problems.add(c_where, "'tests' must be a list")
        return

    expected = {}
    row_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            problems.add(m_where, f"row {row!r}: each row must be an object")
            continue
        row_id = row.get("id")
        if not isinstance(row_id, str) or not PERM_ROW_ID_RE.match(row_id):
            problems.add(m_where, f"row id {row_id!r} must look like PERM-NN")
            continue
        if row_id in row_ids:
            problems.add(m_where, f"duplicate row id {row_id}")
            continue
        row_ids.add(row_id)
        action = row.get("action")
        by_role = row.get("by_role")
        if not isinstance(action, str) or not action.strip():
            problems.add(m_where, f"row {row_id}: 'action' must be a nonempty string")
            continue
        if not isinstance(by_role, dict) or set(by_role) != set(roles):
            problems.add(m_where, f"row {row_id}: by_role must list exactly the matrix roles")
            continue
        for role, mode in by_role.items():
            expected[(row_id, role)] = (action, mode)

    seen = set()
    for test in tests:
        if not isinstance(test, dict):
            problems.add(c_where, "each test must be an object")
            continue
        key = (test.get("permission_row"), test.get("actor_role"))
        if not all(isinstance(part, str) for part in key):
            problems.add(
                c_where, f"{test.get('id')!r}: permission_row and actor_role must be strings"
            )
            continue
        if key in seen:
            problems.add(c_where, f"duplicate case for row {key[0]}, role {key[1]}")
        seen.add(key)
        if key not in expected:
            problems.add(
                c_where, f"{test.get('id')}: no matrix cell for row {key[0]}, role {key[1]}"
            )
        elif (test.get("action"), test.get("mode")) != expected[key]:
            problems.add(c_where, f"{test.get('id')}: action/mode differ from the matrix")
        if not isinstance(test.get("tests"), list) or not test["tests"]:
            problems.add(c_where, f"{test.get('id')}: needs at least one condition variant")
    for row_id, role in sorted(set(expected) - seen, key=str):
        problems.add(c_where, f"no case for row {row_id}, role {role}")


def validate(root):
    """Run every check under root; return the list of problems."""
    problems = Problems()
    tests_dir = root / "tests"
    try:
        spec_text = (root / "SPEC.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        problems.add("SPEC.md", f"cannot read: {error}")
        return problems.items
    defined_reqs = set(SPEC_REQ_DEF_RE.findall(spec_text))
    spec_acs = set(SPEC_AC_ROW_RE.findall(spec_section(spec_text, 23)))
    if not spec_acs:
        problems.add("SPEC.md", "no acceptance criteria found in section 23")

    for fixture in sorted((tests_dir / "fixtures").glob("*.json")):
        data = load_json(fixture, problems, root)
        if isinstance(data, dict) and data.get("synthetic") is not True:
            problems.add(fixture.relative_to(root), "synthetic must be true")

    catalog_path = tests_dir / "contracts" / "step-catalog.json"
    catalog_data = load_json(catalog_path, problems, root)
    cases = (
        check_catalog(catalog_path, catalog_data, defined_reqs, spec_acs, problems, root)
        if catalog_data is not None
        else {}
    )

    scenario_paths = sorted((tests_dir / "scenarios").glob("*.json"))
    if not scenario_paths:
        problems.add("tests/scenarios", "no scenario files found")
    covered = set()
    for path in scenario_paths:
        data = load_json(path, problems, root)
        if data is not None:
            criterion = check_scenario(path, data, cases, defined_reqs, problems, root)
            if criterion:
                covered.add(criterion)
    for missing in sorted(set(cases) - covered, key=ac_number):
        problems.add("tests/scenarios", f"no AC scenario file for {missing}")

    check_traceability(tests_dir / "TRACEABILITY.md", cases, problems, root)

    matrix_path = tests_dir / "contracts" / "permission-matrix.json"
    cases_path = tests_dir / "contracts" / "permission-cases.json"
    matrix = load_json(matrix_path, problems, root)
    perm_cases = load_json(cases_path, problems, root)
    if matrix is not None and perm_cases is not None:
        check_permissions(matrix_path, matrix, cases_path, perm_cases, problems, root)
    return problems.items


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / "tests").is_dir():
        print(f"error: {root / 'tests'} is not a directory", file=sys.stderr)
        return 2

    problems = validate(root)
    for problem in problems:
        print(problem)
    if KNOWN_UNCOVERED:
        print(
            f"note: criteria without scenarios yet: {', '.join(sorted(KNOWN_UNCOVERED, key=ac_number))}",
            file=sys.stderr,
        )
    print(f"scenario library: {len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
