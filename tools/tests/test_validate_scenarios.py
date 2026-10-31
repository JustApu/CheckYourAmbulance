"""Offline tests for tools/validate_scenarios.py using a tiny synthetic library."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import validate_scenarios as vs  # noqa: E402

SPEC = """# Spec
| ID | Rule |
|---|---|
| PRN-1 | Item-level checks count. |
- **CON-4 [P]** Conflicts warn.
- **STK-B [C]** Lettered requirement IDs exist too.

## 23. Acceptance criteria
| ID | Criterion |
|---|---|
| AC-1 | First. |
| AC-2 | Second. |

## 24. Decision register
| AC-99 | Not a criterion: outside section 23. |
"""

TRACEABILITY = """| Criterion | Scenario | SPEC references | Runner status |
|---|---|---|---|
| AC-1 | [AC-01.json](scenarios/AC-01.json) | PRN-1, CON-4 | Symbolic |
| AC-2 | [AC-02.json](scenarios/AC-02.json) | §5.1 (no requirement ID) | Symbolic |
"""

CATALOG = {
    "schema_version": 1,
    "cases": {
        "AC-1": {
            "setup": ["s"],
            "actions": ["a"],
            "assert": ["t"],
            "requirements": ["PRN-1", "CON-4"],
        },
        "AC-2": {
            "setup": ["s"],
            "actions": ["a"],
            "assert": ["t"],
            "requirements": [],
            "spec_refs": ["§5.1"],
        },
    },
}


def scenario(identifier, criterion, requirements, **extra):
    data = {
        "schema_version": 1,
        "id": identifier,
        "criterion": criterion,
        "requirements": requirements,
        "fixture": "../fixtures/base.json",
        "synthetic": True,
        "given": ["g"],
        "when": ["w"],
        "then": ["t"],
        "status": "contract",
    }
    data.update(extra)
    return data


MATRIX = {
    "roles": ["EMT", "Supervisor"],
    "rows": [
        {
            "id": "PERM-01",
            "action": "record_check",
            "by_role": {"EMT": "allow", "Supervisor": "deny"},
        }
    ],
}
PERM_CASES = {
    "tests": [
        {
            "id": "P-EMT",
            "permission_row": "PERM-01",
            "actor_role": "EMT",
            "action": "record_check",
            "mode": "allow",
            "tests": [{"expected": "allow"}],
        },
        {
            "id": "P-SUP",
            "permission_row": "PERM-01",
            "actor_role": "Supervisor",
            "action": "record_check",
            "mode": "deny",
            "tests": [{"expected": "deny"}],
        },
    ]
}


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for sub in ("scenarios", "contracts", "fixtures"):
            (self.root / "tests" / sub).mkdir(parents=True)
        self.write("SPEC.md", SPEC)
        self.write("tests/TRACEABILITY.md", TRACEABILITY)
        self.write_json("tests/contracts/step-catalog.json", CATALOG)
        self.write_json("tests/contracts/permission-matrix.json", MATRIX)
        self.write_json("tests/contracts/permission-cases.json", PERM_CASES)
        self.write_json("tests/fixtures/base.json", {"synthetic": True})
        self.write_json("tests/scenarios/AC-01.json", scenario("AC-1", "AC-1", ["PRN-1", "CON-4"]))
        self.write_json(
            "tests/scenarios/AC-02.json", scenario("AC-2", "AC-2", [], spec_refs=["§5.1"])
        )
        self.write_json(
            "tests/scenarios/EX-race-case.json", scenario("EX-race-case", "AC-1", ["CON-4"])
        )

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        (self.root / rel).write_text(text, encoding="utf-8")

    def write_json(self, rel, data):
        self.write(rel, json.dumps(data))

    def problems(self):
        return vs.validate(self.root)

    def assert_one_problem(self, fragment):
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(fragment, problems[0])

    def test_valid_library_has_no_problems(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(vs.main(["--root", str(self.root)]), 0)

    def test_malformed_json_is_reported(self):
        self.write("tests/scenarios/EX-race-case.json", "{not json")
        self.assert_one_problem("cannot read JSON")

    def test_non_object_scenario_is_reported(self):
        self.write_json("tests/scenarios/EX-race-case.json", ["a list"])
        self.assert_one_problem("must be a JSON object")

    def test_missing_and_unknown_fields_are_reported(self):
        data = scenario("EX-race-case", "AC-1", ["CON-4"], surprise=1)
        del data["then"]
        self.write_json("tests/scenarios/EX-race-case.json", data)
        problems = self.problems()
        self.assertTrue(any("missing field 'then'" in p for p in problems), problems)
        self.assertTrue(any("unknown field 'surprise'" in p for p in problems), problems)

    def test_bad_values_are_reported(self):
        bad = {"synthetic": False, "status": "passing", "schema_version": 2, "given": [""]}
        for field, value in bad.items():
            with self.subTest(field=field):
                self.write_json(
                    "tests/scenarios/EX-race-case.json",
                    scenario("EX-race-case", "AC-1", ["CON-4"], **{field: value}),
                )
                self.assertEqual(len(self.problems()), 1)

    def test_bound_ex_scenario_with_its_own_steps_is_accepted(self):
        self.write_json(
            "tests/scenarios/EX-race-case.json",
            scenario(
                "EX-race-case",
                "AC-1",
                ["CON-4"],
                status="bound",
                steps=["a", "b"],
                database="commit",
            ),
        )
        self.assertEqual(self.problems(), [])

    def test_bound_ex_scenario_needs_steps(self):
        self.write_json(
            "tests/scenarios/EX-race-case.json",
            scenario("EX-race-case", "AC-1", ["CON-4"], status="bound"),
        )
        self.assert_one_problem("a bound EX-* scenario needs its own 'steps'")

    def test_bad_steps_and_database_are_reported(self):
        bad = {"steps": [], "database": "yes"}
        for field, value in bad.items():
            with self.subTest(field=field):
                extra = {"status": "bound", "steps": ["a"], field: value}
                self.write_json(
                    "tests/scenarios/EX-race-case.json",
                    scenario("EX-race-case", "AC-1", ["CON-4"], **extra),
                )
                self.assertEqual(len(self.problems()), 1)

    def test_steps_are_refused_on_ac_scenarios(self):
        data = scenario("AC-1", "AC-1", ["CON-4"], steps=["a"])
        self.write_json("tests/scenarios/AC-01.json", data)
        self.assertTrue(
            any("'steps' is only for EX-* scenarios" in p for p in self.problems()),
            self.problems(),
        )

    def test_undefined_requirement_is_reported(self):
        self.write_json(
            "tests/scenarios/EX-race-case.json", scenario("EX-race-case", "AC-1", ["ZZZ-9"])
        )
        self.assert_one_problem("'ZZZ-9' is not defined in SPEC.md")

    def test_lettered_requirement_id_is_recognized(self):
        self.write_json(
            "tests/scenarios/EX-race-case.json", scenario("EX-race-case", "AC-1", ["STK-B"])
        )
        self.assertEqual(self.problems(), [])
        self.write_json(
            "tests/scenarios/EX-race-case.json", scenario("EX-race-case", "AC-1", ["STK-Z"])
        )
        self.assert_one_problem("'STK-Z' is not defined in SPEC.md")

    def test_empty_requirements_need_spec_refs(self):
        self.write_json("tests/scenarios/EX-race-case.json", scenario("EX-race-case", "AC-1", []))
        self.assert_one_problem("needs a nonempty 'spec_refs'")

    def test_missing_fixture_is_reported(self):
        self.write_json(
            "tests/scenarios/EX-race-case.json",
            scenario("EX-race-case", "AC-1", ["CON-4"], fixture="../fixtures/none.json"),
        )
        self.assert_one_problem("does not exist")

    def test_unsynthetic_fixture_is_reported(self):
        self.write_json("tests/fixtures/base.json", {"synthetic": False})
        self.assert_one_problem("synthetic must be true")

    def test_ac_scenario_must_match_catalog(self):
        self.write_json("tests/scenarios/AC-01.json", scenario("AC-1", "AC-1", ["PRN-1"]))
        self.assert_one_problem("differ from the step catalog")

    def test_ac_scenario_id_must_match_file_name(self):
        self.write_json("tests/scenarios/AC-01.json", scenario("AC-2", "AC-1", ["PRN-1", "CON-4"]))
        self.assert_one_problem("must both be 'AC-1'")

    def test_ex_scenario_id_must_match_file_name(self):
        self.write_json(
            "tests/scenarios/EX-race-case.json", scenario("EX-other", "AC-1", ["CON-4"])
        )
        self.assert_one_problem("must equal the file name")

    def test_bad_file_name_is_reported(self):
        self.write_json("tests/scenarios/Random.json", scenario("Random", "AC-1", ["CON-4"]))
        self.assert_one_problem("file name must be")

    def test_unknown_criterion_is_reported(self):
        self.write_json(
            "tests/scenarios/EX-race-case.json", scenario("EX-race-case", "AC-7", ["CON-4"])
        )
        self.assert_one_problem("'AC-7' is not in the step catalog")

    def test_non_string_criterion_is_reported_not_raised(self):
        for bad in (["AC-1"], {"id": "AC-1"}, 1, None):
            with self.subTest(criterion=bad):
                self.write_json(
                    "tests/scenarios/EX-race-case.json", scenario("EX-race-case", bad, ["CON-4"])
                )
                self.assert_one_problem("is not in the step catalog")

    def test_missing_ac_scenario_file_is_reported(self):
        (self.root / "tests/scenarios/AC-02.json").unlink()
        self.assert_one_problem("no AC scenario file for AC-2")

    def test_spec_criterion_missing_from_catalog_is_reported(self):
        self.write(
            "SPEC.md", SPEC.replace("| AC-2 | Second. |", "| AC-2 | Second. |\n| AC-3 | Third. |")
        )
        self.assert_one_problem("criterion AC-3 has no step-catalog case")

    def test_catalog_case_not_in_spec_is_reported(self):
        self.write("SPEC.md", SPEC.replace("| AC-2 | Second. |\n", ""))
        self.assert_one_problem("AC-2 is not a SPEC.md section 23 criterion")

    def test_library_matches_spec_with_no_known_gaps(self):
        self.assertEqual(vs.KNOWN_UNCOVERED, frozenset())

    def test_known_uncovered_entry_is_exempt_until_covered(self):
        self.write(
            "SPEC.md", SPEC.replace("| AC-2 | Second. |", "| AC-2 | Second. |\n| AC-33 | Seal. |")
        )
        self.assert_one_problem("criterion AC-33 has no step-catalog case")
        with mock.patch.object(vs, "KNOWN_UNCOVERED", frozenset({"AC-33"})):
            self.assertEqual(self.problems(), [])

    @mock.patch.object(vs, "KNOWN_UNCOVERED", frozenset({"AC-33"}))
    def test_known_uncovered_entry_that_becomes_covered_is_reported(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["cases"]["AC-33"] = catalog["cases"]["AC-1"]
        self.write_json("tests/contracts/step-catalog.json", catalog)
        self.write(
            "SPEC.md", SPEC.replace("| AC-2 | Second. |", "| AC-2 | Second. |\n| AC-33 | Seal. |")
        )
        problems = self.problems()
        self.assertTrue(any("remove it from KNOWN_UNCOVERED" in p for p in problems), problems)

    def test_malformed_catalog_case_id_is_reported_not_raised(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["cases"]["bogus"] = catalog["cases"]["AC-1"]
        self.write_json("tests/contracts/step-catalog.json", catalog)
        self.assert_one_problem("case 'bogus' must be an AC-N id")

    def test_catalog_case_with_empty_steps_is_reported(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["cases"]["AC-1"]["assert"] = []
        self.write_json("tests/contracts/step-catalog.json", catalog)
        self.assert_one_problem("'assert' must be a nonempty list")

    def test_traceability_mismatch_is_reported(self):
        self.write("tests/TRACEABILITY.md", TRACEABILITY.replace("PRN-1, CON-4", "PRN-1"))
        self.assert_one_problem("AC-1 row requirements differ")

    def test_traceability_missing_row_is_reported(self):
        self.write("tests/TRACEABILITY.md", TRACEABILITY.split("| AC-2 |")[0])
        self.assert_one_problem("no row for AC-2")

    def test_traceability_wrong_link_is_reported(self):
        self.write(
            "tests/TRACEABILITY.md",
            TRACEABILITY.replace("scenarios/AC-01.json", "scenarios/AC-1.json"),
        )
        self.assert_one_problem("must link scenarios/AC-01.json")

    def test_permission_case_missing_cell_is_reported(self):
        cases = copy.deepcopy(PERM_CASES)
        cases["tests"].pop()
        self.write_json("tests/contracts/permission-cases.json", cases)
        self.assert_one_problem("no case for row PERM-01, role Supervisor")

    def test_permission_case_duplicate_and_mode_mismatch_are_reported(self):
        cases = copy.deepcopy(PERM_CASES)
        cases["tests"][1]["actor_role"] = "EMT"
        self.write_json("tests/contracts/permission-cases.json", cases)
        problems = self.problems()
        self.assertTrue(any("duplicate case" in p for p in problems), problems)
        self.assertTrue(any("action/mode differ" in p for p in problems), problems)
        self.assertTrue(
            any("no case for row PERM-01, role Supervisor" in p for p in problems), problems
        )

    def test_matrix_row_with_wrong_roles_is_reported(self):
        matrix = copy.deepcopy(MATRIX)
        del matrix["rows"][0]["by_role"]["Supervisor"]
        self.write_json("tests/contracts/permission-matrix.json", matrix)
        problems = self.problems()
        self.assertTrue(any("by_role must list exactly" in p for p in problems), problems)

    def test_duplicate_matrix_row_id_is_reported(self):
        matrix = copy.deepcopy(MATRIX)
        conflicting = copy.deepcopy(matrix["rows"][0])
        conflicting["by_role"]["Supervisor"] = "allow"
        matrix["rows"].append(conflicting)
        self.write_json("tests/contracts/permission-matrix.json", matrix)
        self.assert_one_problem("duplicate row id PERM-01")

    def test_malformed_matrix_rows_are_reported_not_raised(self):
        roles = MATRIX["rows"][0]["by_role"]
        bad_rows = [
            ("PERM-02", "each row must be an object"),
            ({"id": ["PERM-02"], "action": "x", "by_role": roles}, "must look like PERM-NN"),
            ({"id": "perm-2", "action": "x", "by_role": roles}, "must look like PERM-NN"),
            (
                {"id": "PERM-02", "action": None, "by_role": roles},
                "'action' must be a nonempty string",
            ),
        ]
        for row, fragment in bad_rows:
            with self.subTest(row=row):
                matrix = copy.deepcopy(MATRIX)
                matrix["rows"].append(row)
                self.write_json("tests/contracts/permission-matrix.json", matrix)
                self.assert_one_problem(fragment)

    def test_non_string_permission_case_key_is_reported_not_raised(self):
        cases = copy.deepcopy(PERM_CASES)
        cases["tests"].append({**cases["tests"][0], "id": "P-BAD", "permission_row": ["PERM-01"]})
        self.write_json("tests/contracts/permission-cases.json", cases)
        self.assert_one_problem("permission_row and actor_role must be strings")

    def test_missing_spec_is_reported_not_raised(self):
        (self.root / "SPEC.md").unlink()
        self.assert_one_problem("SPEC.md: cannot read")

    def test_main_exit_statuses(self):
        self.write_json(
            "tests/scenarios/EX-race-case.json", scenario("EX-race-case", "AC-1", ["ZZZ-9"])
        )
        self.assertEqual(vs.main(["--root", str(self.root)]), 1)
        self.assertEqual(vs.main(["--root", str(self.root / "tests" / "scenarios")]), 2)


if __name__ == "__main__":
    unittest.main()
