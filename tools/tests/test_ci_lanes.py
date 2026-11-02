"""Routing tests for tools/ci_lanes.py (issue #19).

These assert the configured patterns and the fail-closed behavior, not GitHub's path filtering.
"""

import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import ci_lanes  # noqa: E402

TEMPLATE = "cya/inspections/templates/inspections/deficiency.html"
SCRIPT = "cya/inspections/templates/inspections/workflows.js"


def lanes(*paths, **kwargs):
    decision = ci_lanes.classify(list(paths), **kwargs)
    return (
        decision["run-django"],
        decision["django-mode"],
        decision["django-labels"],
        decision["run-client-scripts"],
    )


class RoutingTests(unittest.TestCase):
    def test_ui_only_runs_client_scripts_and_focused_template_tests(self):
        run, mode, labels, client = lanes(TEMPLATE, SCRIPT)
        self.assertEqual((run, mode, client), (True, "focused", True))
        # inspections templates are included by operations and attention pages
        for label in ("cya.inspections", "cya.attention", "cya.operations"):
            self.assertIn(label, labels)
        self.assertNotIn("cya", labels)
        self.assertEqual(len(labels), len(set(labels)))

    def test_harness_only_runs_client_scripts_without_the_database(self):
        self.assertEqual(
            lanes("cya/operations/tests/status_script_harness.js"), (False, "none", (), True)
        )

    def test_server_only_runs_the_full_suite_without_client_scripts(self):
        for path in (
            "cya/deficiencies/commands.py",
            "cya/deficiencies/migrations/0019_example.py",
            "requirements/base.txt",
            "tests/test_audit_grants.py",
            "tools/validate_scenarios.py",
            "manage.py",
            "pyproject.toml",
        ):
            with self.subTest(path):
                self.assertEqual(lanes(path), (True, "full", ("cya",), False))

    def test_mixed_runs_the_full_suite_once_and_client_scripts(self):
        self.assertEqual(
            lanes(TEMPLATE, "cya/inspections/workflow_views.py"), (True, "full", ("cya",), True)
        )

    def test_workflow_and_configuration_changes_run_both_lanes(self):
        for path in (".github/workflows/checks.yml", ".github/dependabot.yml"):
            with self.subTest(path):
                self.assertEqual(lanes(path), (True, "full", ("cya",), True))

    def test_a_rename_is_routed_by_both_names(self):
        # The workflow passes filename and previous_filename; either side can require a lane.
        self.assertEqual(
            lanes("cya/inspections/views.py", TEMPLATE), (True, "full", ("cya",), True)
        )
        self.assertEqual(lanes("cya/shell/old.py", "docs/new.md"), (True, "full", ("cya",), False))

    def test_a_deletion_is_routed_by_its_path(self):
        self.assertEqual(lanes("cya/operations/templates/operations/old.html")[1], "focused")
        self.assertEqual(lanes("cya/checks/old_module.py")[1], "full")

    def test_unknown_paths_fail_closed_to_both_lanes(self):
        for path in ("Dockerfile", "cya/checks/fixtures/data.json", "scripts/run.sh", "Makefile"):
            with self.subTest(path):
                self.assertEqual(lanes(path), (True, "full", ("cya",), True))

    def test_a_template_without_focused_coverage_runs_the_full_suite(self):
        self.assertEqual(
            lanes("cya/shell/templates/shell/base.html"), (True, "full", ("cya",), True)
        )
        self.assertEqual(lanes("cya/newapp/static/newapp/app.css")[1], "full")

    def test_documentation_needs_neither_lane_except_the_contract_document(self):
        self.assertEqual(
            lanes("README.md", "docs/review.md", "docs/x.json", ".gitignore"),
            (False, "none", (), False),
        )
        self.assertEqual(lanes("docs/data-model-contracts.md")[1], "full")
        self.assertEqual(lanes("docs/deployment-runbook.md")[1], "full")

    def test_query_failure_and_empty_results_fail_closed(self):
        self.assertEqual(lanes(query_failed=True), (True, "full", ("cya",), True))
        self.assertEqual(lanes(), (True, "full", ("cya",), True))
        self.assertEqual(lanes("", "  "), (True, "full", ("cya",), True))

    def test_a_list_that_may_be_truncated_fails_closed(self):
        # The files endpoint returns at most 3,000 files; a UI-only prefix could hide a server
        # path, so the event's total count decides.
        self.assertEqual(lanes(TEMPLATE, changed_files=2999)[1], "focused")
        for count in (3000, 3001, "3000", "", "abc", "unknown", -1):
            with self.subTest(count=count):
                self.assertEqual(
                    lanes(TEMPLATE, changed_files=count), (True, "full", ("cya",), True)
                )
        self.assertEqual(lanes(TEMPLATE)[1], "focused")  # count omitted (local use)

    def test_output_never_carries_text_from_a_path(self):
        hostile = "cya/inspections/templates/$(touch x)\nrun-django=false.html"
        lines = ci_lanes.output_lines(ci_lanes.classify([hostile, "cya/x/templates/`id`.js"]))
        allowed = {label for labels in ci_lanes.UI_TESTS.values() for label in labels} | {"cya"}
        for line in lines:
            key, _, value = line.partition("=")
            self.assertIn(key, ("run-django", "django-mode", "django-labels", "run-client-scripts"))
            if key == "django-labels":
                self.assertTrue(set(value.split()) <= allowed, value)
            else:
                self.assertIn(value, ("true", "false", "full", "focused", "none"))

    def test_main_writes_github_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out"
            sys.stdin, original = io.StringIO(TEMPLATE + "\n"), sys.stdin
            try:
                with redirect_stdout(io.StringIO()):
                    ci_lanes.main(["--github-output", str(target)])
            finally:
                sys.stdin = original
            written = target.read_text().splitlines()
        self.assertIn("django-mode=focused", written)
        self.assertIn("run-client-scripts=true", written)

    def test_a_non_pull_request_event_runs_both_lanes_in_full(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out"
            with redirect_stdout(io.StringIO()):
                ci_lanes.main(["--all", "--github-output", str(target)])
            written = target.read_text().splitlines()
        for line in (
            "run-django=true",
            "django-mode=full",
            "django-labels=cya",
            "run-client-scripts=true",
        ):
            self.assertIn(line, written)


class CoverageMapTests(unittest.TestCase):
    """The focused map stays true to the repository it routes."""

    def test_every_label_names_an_existing_package_or_module(self):
        for labels in ci_lanes.UI_TESTS.values():
            for label in labels:
                path = ROOT.joinpath(*label.split("."))
                with self.subTest(label):
                    self.assertTrue(
                        path.is_dir() or path.with_suffix(".py").is_file(), f"{label} missing"
                    )

    def test_an_app_including_another_apps_templates_is_in_that_apps_focused_tests(self):
        include = re.compile(r'\{%\s*(?:include|extends)\s+"([a-z_]+)/')
        for template in sorted(ROOT.glob("cya/*/templates/**/*")):
            if not template.is_file():
                continue
            app = template.relative_to(ROOT).parts[1]
            for used in set(include.findall(template.read_text(encoding="utf-8"))):
                if used == app or used not in ci_lanes.UI_TESTS:
                    continue  # a change to an app without a focused set runs the full suite
                with self.subTest(template=str(template.relative_to(ROOT)), uses=used):
                    self.assertIn(f"cya.{app}", ci_lanes.UI_TESTS[used])

    def test_every_document_a_test_reads_is_a_server_input(self):
        # "docs" / "name.md" path joins or "docs/name.md" strings in test code.
        joined = re.compile(r'"docs"\s*/\s*"([^"/]+\.md)"')
        literal = re.compile(r'["\'](docs/[^"\']+\.md)["\']')
        sources = [
            *ROOT.glob("cya/**/tests/*.py"),
            *ROOT.glob("cya/tests/*.py"),
            *ROOT.glob("tests/**/*.py"),
        ]
        read = set()
        for source in sources:
            text = source.read_text(encoding="utf-8")
            read.update(f"docs/{name}" for name in joined.findall(text))
            read.update(literal.findall(text))
        self.assertIn("docs/deployment-runbook.md", read)  # the scan finds what it should
        self.assertLessEqual(read, ci_lanes.SERVER_DOCUMENTS)

    def test_every_harness_is_routed_to_the_client_script_lane(self):
        harnesses = sorted(ROOT.glob("cya/*/tests/*_harness.js"))
        self.assertTrue(harnesses)
        for harness in harnesses:
            with self.subTest(harness.name):
                self.assertTrue(ci_lanes.HARNESS_PATH.match(str(harness.relative_to(ROOT))))


if __name__ == "__main__":
    unittest.main()
