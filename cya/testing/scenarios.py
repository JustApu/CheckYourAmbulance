"""Scenario-library runner skeleton (tests/README.md, issue 19 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/19) Part B).

Each scenario file in tests/scenarios/ becomes one test. A scenario that
has status "contract" is a symbolic specification, so its test is reported
as skipped ("pending"), never as passed. When a feature issue implements the
behavior, it registers a Python function for each symbolic step name with
STEPS and changes the scenario's status to "bound" (adding that status to
tools/validate_scenarios.py in the same PR). A bound scenario runs its AC
catalog steps in order (setup, actions, assert) and fails if any step has no
binding, so an unsupported step can never pass silently.

EX-* scenarios have no step list in the catalog. A bound EX-* scenario lists its
own ordered "steps" in its file (same names, same registry, same rules); one
without "steps" can only be pending, and binding it is an error. A scenario that
exercises an audited command sets "database": "commit" because such commands
need real commits, so its test runs without the usual wrapping transaction.
"""

import json
import unittest
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

SCENARIO_DIR = Path(settings.BASE_DIR) / "tests" / "scenarios"
CATALOG_PATH = Path(settings.BASE_DIR) / "tests" / "contracts" / "step-catalog.json"
STEP_PHASES = ("setup", "actions", "assert")


class ScenarioError(Exception):
    """The library is malformed or a bound scenario cannot run."""


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    criterion: str
    status: str
    steps: tuple[str, ...]  # catalog steps for AC-*; the file's own "steps" for EX-*
    path: Path
    commits: bool = False  # needs real commits (TransactionTestCase), see module docstring


class StepRegistry:
    """Maps symbolic step names to callables taking a shared context dict."""

    def __init__(self):
        self._steps = {}

    def register(self, name):
        def decorator(func):
            if name in self._steps:
                raise ScenarioError(f"step {name!r} is already bound")
            self._steps[name] = func
            return func

        return decorator

    def __contains__(self, name):
        return name in self._steps

    def __getitem__(self, name):
        return self._steps[name]


STEPS = StepRegistry()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"cannot read {path}: {exc}") from exc


def _catalog_steps(case, criterion):
    if not isinstance(case, dict):
        raise ScenarioError(f"step catalog has no case for {criterion}")
    steps = []
    for phase in STEP_PHASES:
        names = case.get(phase)
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ScenarioError(f"step catalog {criterion}.{phase} must be a list of names")
        steps.extend(names)
    return tuple(steps)


def _own_steps(data, path):
    steps = data.get("steps", [])
    if not isinstance(steps, list) or not all(isinstance(n, str) and n for n in steps):
        raise ScenarioError(f"{path}: 'steps' must be a list of nonempty strings")
    return tuple(steps)


def load_scenarios(scenario_dir=SCENARIO_DIR, catalog_path=CATALOG_PATH):
    """Return every scenario, sorted by file name. Structure is validated by
    tools/validate_scenarios.py in CI; this only checks what the runner uses."""
    catalog = _read_json(catalog_path)
    cases = catalog.get("cases") if isinstance(catalog, dict) else None
    if not isinstance(cases, dict):
        raise ScenarioError(f"{catalog_path}: 'cases' must be an object")
    scenarios = []
    for path in sorted(Path(scenario_dir).glob("*.json")):
        data = _read_json(path)
        fields = [
            data.get(k) if isinstance(data, dict) else None for k in ("id", "criterion", "status")
        ]
        if not all(isinstance(f, str) and f for f in fields):
            raise ScenarioError(f"{path}: id, criterion and status must be nonempty strings")
        scenario_id, criterion, status = fields
        if path.stem.startswith("AC-"):
            steps = _catalog_steps(cases.get(criterion), criterion)
        else:
            steps = _own_steps(data, path)
        scenarios.append(
            Scenario(scenario_id, criterion, status, steps, path, data.get("database") == "commit")
        )
    return scenarios


def run_scenario(scenario, registry=STEPS):
    """Run one scenario: skip if pending, execute if bound, otherwise fail."""
    if scenario.status == "contract":
        bound = sum(1 for step in scenario.steps if step in registry)
        raise unittest.SkipTest(
            f"pending: {scenario.scenario_id} is a symbolic contract "
            f"({bound} of {len(scenario.steps)} steps bound)"
        )
    if scenario.status != "bound":
        raise ScenarioError(f"{scenario.scenario_id}: unknown status {scenario.status!r}")
    if not scenario.steps:
        raise ScenarioError(f"{scenario.scenario_id}: no step list to run")
    missing = [step for step in scenario.steps if step not in registry]
    if missing:
        raise ScenarioError(f"{scenario.scenario_id}: unsupported steps: {', '.join(missing)}")
    # Steps may append callables to context["cleanups"] (e.g. to undo a
    # settings override); they run in reverse order even if a step fails.
    context = {"scenario": scenario, "cleanups": []}
    try:
        for step in scenario.steps:
            registry[step](context)
    finally:
        for cleanup in reversed(context["cleanups"]):
            cleanup()


@STEPS.register("m2_demo_setup")
def m2_demo_setup(context):
    """Install a deterministic clock and initialize the synthetic M2 integration fleet."""
    from datetime import datetime
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from cya.testing.fixtures import load_fixture
    from cya.testing.network import NetworkGuard
    from cya.testing.obligations import ObligationEngineScenario, ScenarioClock

    fixture = load_fixture("obligation-integration.json")
    if fixture.get("non_authoritative") is not True or fixture.get("days") != 7:
        raise ScenarioError("M2 fixture must be non-authoritative and span seven days")
    guard = NetworkGuard()
    guard.install()
    context["cleanups"].append(guard.uninstall)
    start = datetime.fromisoformat(fixture["start_date"]).replace(
        tzinfo=ZoneInfo(fixture["station_zone"])
    )
    clock = ScenarioClock(start)
    patcher = patch("django.utils.timezone.now", side_effect=clock)
    patcher.start()
    context["cleanups"].append(patcher.stop)
    context["m2"] = scenario = ObligationEngineScenario(clock, fixture)
    scenario.setup()


@STEPS.register("m2_run_week_and_verify")
def m2_run_week_and_verify(context):
    """Exercise real command interfaces, local roster/check stubs and historical projections."""
    context["m2"].run_week()


@STEPS.register("m3_inspection_setup")
def m3_inspection_setup(context):
    """Install the scenario's network guard and a thread-safe synthetic server clock."""
    from datetime import datetime
    from unittest.mock import patch

    from cya.testing.fixtures import load_fixture
    from cya.testing.inspections import InspectionClock, InspectionScenario
    from cya.testing.network import NetworkGuard

    fixture = load_fixture("inspection-integration.json")
    if fixture.get("non_authoritative") is not True:
        raise ScenarioError("M3 fixture must be non-authoritative")
    guard = NetworkGuard()
    guard.install()
    context["cleanups"].append(guard.uninstall)
    clock = InspectionClock(datetime.fromisoformat(fixture["start"]))
    patcher = patch("django.utils.timezone.now", side_effect=clock)
    patcher.start()
    context["cleanups"].append(patcher.stop)
    context["m3"] = scenario = InspectionScenario(clock, fixture)
    scenario.setup()


@STEPS.register("m3_crew_checks")
def m3_crew_checks(context):
    context["m3"].crew_checks()


@STEPS.register("m3_replay_and_race")
def m3_replay_and_race(context):
    context["m3"].replay_and_race()


@STEPS.register("m3_corrections")
def m3_corrections(context):
    context["m3"].correction_and_invalidation()


@STEPS.register("m3_unable_and_odometer")
def m3_unable_and_odometer(context):
    context["m3"].unable_and_odometer()


@STEPS.register("m3_kits_and_links")
def m3_kits_and_links(context):
    context["m3"].kits_and_links()


@STEPS.register("m3_live_delivery")
def m3_live_delivery(context):
    context["m3"].live_delivery()


@STEPS.register("m3_idle_and_history")
def m3_idle_and_history(context):
    context["m3"].idle_and_history()


@STEPS.register("m4_resolution_setup")
def m4_resolution_setup(context):
    """Install the network guard and a thread-safe clock, then build the M4 agency."""
    from datetime import datetime
    from unittest.mock import patch

    from cya.testing.fixtures import load_fixture
    from cya.testing.inspections import InspectionClock
    from cya.testing.network import NetworkGuard
    from cya.testing.resolution import ResolutionScenario

    fixture = load_fixture("resolution-integration.json")
    if fixture.get("non_authoritative") is not True:
        raise ScenarioError("M4 fixture must be non-authoritative")
    guard = NetworkGuard()
    guard.install()
    context["cleanups"].append(guard.uninstall)
    clock = InspectionClock(datetime.fromisoformat(fixture["start"]))
    patcher = patch("django.utils.timezone.now", side_effect=clock)
    patcher.start()
    context["cleanups"].append(patcher.stop)
    context["m4"] = scenario = ResolutionScenario(clock, fixture)
    scenario.setup()


@STEPS.register("m4_lifecycle")
def m4_lifecycle(context):
    context["m4"].lifecycle()


@STEPS.register("m4_verification")
def m4_verification(context):
    context["m4"].verification()


@STEPS.register("m4_reopenings")
def m4_reopenings(context):
    context["m4"].reopenings()


@STEPS.register("m4_suspensions_and_status")
def m4_suspensions_and_status(context):
    context["m4"].suspensions_and_status()


@STEPS.register("m4_dashboard_and_notifications")
def m4_dashboard_and_notifications(context):
    context["m4"].dashboard_and_notifications()


@STEPS.register("m4_history")
def m4_history(context):
    context["m4"].history()
