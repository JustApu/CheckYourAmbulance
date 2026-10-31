import json
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from cya.testing.scenarios import (
    Scenario,
    ScenarioError,
    StepRegistry,
    load_scenarios,
    run_scenario,
)

CATALOG = {"cases": {"AC-1": {"setup": ["s1"], "actions": ["a1"], "assert": ["t1"]}}}


def scenario(status="bound", steps=("s1", "a1", "t1"), scenario_id="AC-1"):
    return Scenario(scenario_id, "AC-1", status, tuple(steps), Path(f"{scenario_id}.json"))


class RunScenarioTests(SimpleTestCase):
    def registry(self, calls, names=("s1", "a1", "t1")):
        registry = StepRegistry()
        for name in names:
            registry.register(name)(lambda context, name=name: calls.append(name))
        return registry

    def test_contract_scenario_is_skipped_as_pending(self):
        with self.assertRaisesRegex(unittest.SkipTest, r"pending: AC-1 .*\(1 of 3 steps bound\)"):
            run_scenario(scenario(status="contract"), self.registry([], names=("a1",)))

    def test_bound_scenario_runs_steps_in_catalog_order(self):
        calls = []
        run_scenario(scenario(), self.registry(calls))
        self.assertEqual(calls, ["s1", "a1", "t1"])

    def test_bound_scenario_with_unbound_step_fails_before_running_any(self):
        calls = []
        with self.assertRaisesRegex(ScenarioError, "unsupported steps: t1"):
            run_scenario(scenario(), self.registry(calls, names=("s1", "a1")))
        self.assertEqual(calls, [])

    def test_cleanups_run_in_reverse_even_when_a_step_fails(self):
        calls = []
        registry = StepRegistry()
        registry.register("s1")(
            lambda ctx: ctx["cleanups"].extend(
                [lambda: calls.append("first"), lambda: calls.append("second")]
            )
        )
        registry.register("a1")(lambda ctx: 1 / 0)
        registry.register("t1")(lambda ctx: calls.append("never"))
        with self.assertRaises(ZeroDivisionError):
            run_scenario(scenario(), registry)
        self.assertEqual(calls, ["second", "first"])

    def test_unknown_status_fails(self):
        with self.assertRaisesRegex(ScenarioError, "unknown status 'passed'"):
            run_scenario(scenario(status="passed"), StepRegistry())

    def test_bound_scenario_without_steps_fails(self):
        with self.assertRaisesRegex(ScenarioError, "no step list"):
            run_scenario(scenario(steps=(), scenario_id="EX-x"), StepRegistry())

    def test_step_cannot_be_bound_twice(self):
        registry = StepRegistry()
        registry.register("s1")(print)
        with self.assertRaisesRegex(ScenarioError, "already bound"):
            registry.register("s1")(print)


class LoadScenariosTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "scenarios"
        self.root.mkdir()
        self.catalog = Path(self._tmp.name) / "catalog.json"
        self.catalog.write_text(json.dumps(CATALOG), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding="utf-8")

    def load(self):
        return load_scenarios(self.root, self.catalog)

    def test_ac_scenarios_get_catalog_steps_and_ex_scenarios_none(self):
        self.write("AC-01.json", {"id": "AC-1", "criterion": "AC-1", "status": "contract"})
        self.write("EX-case.json", {"id": "EX-case", "criterion": "AC-1", "status": "contract"})
        loaded = self.load()
        self.assertEqual([s.scenario_id for s in loaded], ["AC-1", "EX-case"])
        self.assertEqual(loaded[0].steps, ("s1", "a1", "t1"))
        self.assertEqual(loaded[1].steps, ())

    def test_ex_scenario_reads_its_own_steps_and_commit_flag(self):
        self.write(
            "EX-case.json",
            {
                "id": "EX-case",
                "criterion": "AC-1",
                "status": "bound",
                "database": "commit",
                "steps": ["s1", "t1"],
            },
        )
        self.write("EX-plain.json", {"id": "EX-plain", "criterion": "AC-1", "status": "contract"})
        case, plain = self.load()
        self.assertEqual((case.steps, case.commits), (("s1", "t1"), True))
        self.assertEqual((plain.steps, plain.commits), ((), False))

    def test_malformed_ex_steps_are_an_error(self):
        for steps in ("s1", ["s1", ""], [1]):
            with self.subTest(steps=steps):
                self.write(
                    "EX-case.json",
                    {"id": "EX-case", "criterion": "AC-1", "status": "bound", "steps": steps},
                )
                with self.assertRaisesRegex(ScenarioError, "'steps' must be a list"):
                    self.load()

    def test_missing_catalog_case_is_an_error(self):
        self.write("AC-02.json", {"id": "AC-2", "criterion": "AC-2", "status": "contract"})
        with self.assertRaisesRegex(ScenarioError, "no case for AC-2"):
            self.load()

    def test_malformed_scenario_is_an_error(self):
        self.write("AC-01.json", {"id": "AC-1", "criterion": ["AC-1"], "status": "contract"})
        with self.assertRaisesRegex(ScenarioError, "must be nonempty strings"):
            self.load()

    def test_unreadable_json_is_an_error(self):
        (self.root / "AC-01.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ScenarioError, "cannot read"):
            self.load()

    def test_real_library_loads_with_only_known_statuses(self):
        loaded = load_scenarios()
        self.assertGreaterEqual(len(loaded), 35)
        self.assertLessEqual({s.status for s in loaded}, {"contract", "bound"})
        self.assertIn("AC-22", {s.scenario_id for s in loaded if s.status == "bound"})
