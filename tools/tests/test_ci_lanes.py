"""Routing tests for tools/ci_lanes.py (issue #19).

These assert the configured patterns and the fail-closed behavior, not GitHub's path filtering.
"""

import ast
import io
import json
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


def browser_smoke(*paths, **kwargs):
    return ci_lanes.classify(list(paths), **kwargs)["run-browser-smoke"]


class RoutingTests(unittest.TestCase):
    def test_seal_page_changes_run_its_template_tests_and_script_harness(self):
        template = ci_lanes.classify(["cya/seals/templates/seals/vehicle.html"])
        self.assertEqual(template["django-mode"], "focused")
        self.assertIn("cya.seals", template["django-labels"])
        script = ci_lanes.classify(["cya/seals/templates/seals/vehicle.js"])
        self.assertIn("cya.seals", script["django-labels"])
        self.assertTrue(script["run-client-scripts"])

    def test_ui_only_runs_client_scripts_and_focused_template_tests(self):
        run, mode, labels, client = lanes(TEMPLATE, SCRIPT)
        self.assertEqual((run, mode, client), (True, "focused", True))
        # inspections templates are included by operations and attention pages
        for label in ("cya.inspections", "cya.attention", "cya.operations"):
            self.assertIn(label, labels)
        self.assertNotIn("cya", labels)
        self.assertEqual(len(labels), len(set(labels)))

    def test_html_templates_keep_focused_django_tests_and_add_browser_smoke(self):
        self.assertEqual(
            lanes(TEMPLATE),
            (
                True,
                "focused",
                (
                    "cya.inspections",
                    "cya.attention",
                    "cya.operations",
                    "cya.seals",
                    "cya.shell",
                    "cya.tests.test_background_refresh",
                    "cya.tests.test_home",
                ),
                False,
            ),
        )
        self.assertTrue(browser_smoke(TEMPLATE))

    def test_shell_leaf_templates_use_the_existing_focused_suite(self):
        for path in ci_lanes.SHELL_FOCUSED_TEMPLATES:
            with self.subTest(path=path):
                decision = ci_lanes.classify([path])
                self.assertEqual(decision["django-mode"], "focused")
                self.assertEqual(
                    decision["django-labels"], ("cya.tests.test_shell", "cya.tests.test_home")
                )
                self.assertTrue(decision["run-browser-smoke"])

    def test_shared_shell_templates_keep_the_full_suite(self):
        for path in ci_lanes.SHELL_FULL_TEMPLATES:
            with self.subTest(path=path):
                decision = ci_lanes.classify([path])
                self.assertEqual(decision["django-mode"], "full")
                self.assertTrue(decision["run-browser-smoke"])

    def test_covered_css_uses_browser_smoke_without_database_or_client_harnesses(self):
        for path in ci_lanes.BROWSER_CSS:
            with self.subTest(path=path):
                self.assertEqual(lanes(path), (False, "none", (), False))
                self.assertTrue(browser_smoke(path))

    def test_only_mapped_context_scripts_use_focused_tests_and_node_harnesses(self):
        for path in ci_lanes.SCRIPT_HARNESSES:
            with self.subTest(path=path):
                decision = ci_lanes.classify([path])
                self.assertEqual(decision["django-mode"], "focused")
                self.assertTrue(decision["run-client-scripts"])
                self.assertFalse(decision["run-browser-smoke"])

    def test_shared_shell_script_uses_shell_tests_and_browser(self):
        decision = ci_lanes.classify(["cya/shell/templates/shell/shell.js"])
        self.assertEqual(decision["django-labels"], ("cya.tests.test_shell", "cya.tests.test_home"))
        self.assertTrue(decision["run-browser-smoke"])

    def test_role_matrix_script_uses_authorization_tests_and_browser_smoke(self):
        decision = ci_lanes.classify(["cya/authorization/templates/authorization/roles.js"])
        self.assertEqual(
            decision["django-labels"],
            (
                "cya.authorization",
                "cya.tests.test_role_administration_http",
                "cya.tests.test_role_transition_integration",
            ),
        )
        self.assertTrue(decision["run-browser-smoke"])
        self.assertFalse(decision["run-client-scripts"])

    def test_browser_test_source_change_runs_full_server_and_browser_checks(self):
        for path in ci_lanes.BROWSER_TEST_PATHS:
            with self.subTest(path=path):
                decision = ci_lanes.classify([path])
                self.assertEqual(decision["django-mode"], "full")
                self.assertTrue(decision["run-browser-smoke"])

    def test_unknown_ui_paths_run_every_lane(self):
        decision = ci_lanes.classify(["cya/newapp/static/newapp/app.css"])
        self.assertEqual(decision["django-mode"], "full")
        self.assertTrue(decision["run-client-scripts"])
        self.assertTrue(decision["run-browser-smoke"])

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

    def test_mixed_server_template_runs_full_suite_and_browser_smoke(self):
        self.assertEqual(
            lanes(TEMPLATE, "cya/inspections/workflow_views.py"), (True, "full", ("cya",), False)
        )
        self.assertTrue(browser_smoke(TEMPLATE, "cya/inspections/workflow_views.py"))

    def test_workflow_and_configuration_changes_run_both_lanes(self):
        for path in (".github/workflows/checks.yml", ".github/dependabot.yml"):
            with self.subTest(path):
                self.assertEqual(lanes(path), (True, "full", ("cya",), True))

    def test_a_rename_is_routed_by_both_names(self):
        # The workflow passes filename and previous_filename; either side can require a lane.
        self.assertEqual(
            lanes("cya/inspections/views.py", TEMPLATE), (True, "full", ("cya",), False)
        )
        self.assertTrue(browser_smoke("cya/inspections/views.py", TEMPLATE))
        self.assertEqual(lanes("cya/shell/old.py", "docs/new.md"), (True, "full", ("cya",), False))

    def test_a_deletion_is_routed_by_its_path(self):
        self.assertEqual(lanes("cya/operations/templates/operations/old.html")[1], "focused")
        self.assertEqual(lanes("cya/checks/old_module.py")[1], "full")

    def test_unknown_paths_fail_closed_to_both_lanes(self):
        for path in ("Dockerfile", "cya/checks/fixtures/data.json", "scripts/run.sh", "Makefile"):
            with self.subTest(path):
                self.assertEqual(lanes(path), (True, "full", ("cya",), True))
                self.assertTrue(browser_smoke(path))

    def test_deployment_and_test_host_configuration_fail_closed(self):
        for path in ("deploy/rocky9/cya.service", "deploy/backup/cya-backup-full", "Dockerfile"):
            with self.subTest(path):
                decision = ci_lanes.classify([path])
                self.assertEqual(decision["django-mode"], "full")
                self.assertTrue(decision["run-client-scripts"])
                self.assertTrue(decision["run-browser-smoke"])

    def test_a_template_without_focused_coverage_runs_the_full_suite(self):
        self.assertEqual(
            lanes("cya/shell/templates/shell/unknown.dat"), (True, "full", ("cya",), True)
        )
        self.assertEqual(
            lanes("cya/shell/templates/shell/new-page.html"),
            (True, "full", ("cya",), True),
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
        self.assertTrue(browser_smoke(query_failed=True))
        self.assertTrue(browser_smoke())

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
        self.assertTrue(browser_smoke(TEMPLATE, changed_files=3000))

    def test_output_never_carries_text_from_a_path(self):
        hostile = "cya/inspections/templates/$(touch x)\nrun-django=false.html"
        lines = ci_lanes.output_lines(ci_lanes.classify([hostile, "cya/x/templates/`id`.js"]))
        allowed = {label for labels in ci_lanes.UI_TESTS.values() for label in labels} | {"cya"}
        for line in lines:
            key, _, value = line.partition("=")
            self.assertIn(
                key,
                (
                    "run-django",
                    "django-mode",
                    "django-labels",
                    "django-exclude-tags",
                    "django-shard-count",
                    "django-shards",
                    "run-client-scripts",
                    "run-browser-smoke",
                ),
            )
            if key == "django-labels":
                self.assertTrue(set(value.split()) <= allowed, value)
            elif key == "django-exclude-tags":
                self.assertIn(value, ("", ci_lanes.MIGRATION_TAG))
            elif key == "django-shard-count":
                self.assertIn(value, ("1", str(ci_lanes.DJANGO_SHARDS)))
            elif key == "django-shards":
                self.assertRegex(value, r"^\[[1-9][0-9]*(?:,[1-9][0-9]*)*\]$")
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
        self.assertIn("run-browser-smoke=true", written)

    def test_a_non_pull_request_event_runs_both_lanes_in_full(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out"
            with redirect_stdout(io.StringIO()):
                ci_lanes.main(["--all", "--github-output", str(target)])
            written = target.read_text().splitlines()
        shards = ",".join(str(index) for index in range(1, ci_lanes.DJANGO_SHARDS + 1))
        for line in (
            "run-django=true",
            "django-mode=full",
            "django-labels=cya",
            "django-exclude-tags=",
            f"django-shard-count={ci_lanes.DJANGO_SHARDS}",
            f"django-shards=[{shards}]",
            "run-client-scripts=true",
            "run-browser-smoke=true",
        ):
            self.assertIn(line, written)


def excluded(*paths, **kwargs):
    return ci_lanes.classify(list(paths), **kwargs)["django-exclude-tags"]


class MigrationRoutingTests(unittest.TestCase):
    """Issue #158 item 4, option 2: when a full PR run may leave out the migration tests."""

    TAG = (ci_lanes.MIGRATION_TAG,)

    def test_a_server_change_without_migration_inputs_excludes_the_tagged_tests(self):
        for path in (
            "cya/deficiencies/commands.py",
            "cya/operations/tests/test_status.py",
            "tests/test_audit_grants.py",
            "tools/validate_scenarios.py",
            "docs/data-model-contracts.md",
            "tools/browser_smoke.mjs",
            "cya/shell/templates/shell/base.html",
        ):
            with self.subTest(path):
                decision = ci_lanes.classify([path])
                self.assertEqual(decision["django-mode"], "full")
                self.assertEqual(decision["django-exclude-tags"], self.TAG)

    def test_a_pr_changing_migrations_runs_the_tagged_tests(self):
        for path in (
            "cya/deficiencies/migrations/0019_example.py",
            "cya/newapp/migrations/0001_initial.py",
            "cya/obligations/migrations/__init__.py",
            "cya/core/migrations/sql/trigger.sql",  # not Python: also unclassified, all lanes
        ):
            with self.subTest(path):
                decision = ci_lanes.classify([path, "cya/deficiencies/commands.py"])
                self.assertEqual(decision["django-mode"], "full")
                self.assertEqual(decision["django-exclude-tags"], ())

    def test_migration_imports_tests_and_tooling_run_the_tagged_tests(self):
        paths = sorted(ci_lanes.MIGRATION_IMPORTS | ci_lanes.MIGRATION_TEST_INPUTS) + [
            "cya/testing/runner.py",
            "cya/testing/sharding.py",
            "cya/settings.py",
            "cya/core/db.py",
            "manage.py",
            "pyproject.toml",
            "requirements/base.txt",
            "tools/migration_test_tags.py",
            "tools/tests/test_migration_test_tags.py",
            "tools/ci_lanes.py",
            "tools/tests/test_ci_lanes.py",
        ]
        for path in paths:
            with self.subTest(path):
                self.assertEqual(excluded(path), ())

    def test_a_rename_or_deletion_of_a_migration_input_runs_the_tagged_tests(self):
        # The workflow passes both names of a rename, and a deleted file's path.
        self.assertEqual(
            excluded("cya/deficiencies/tests/test_correction.py", "cya/deficiencies/tests/x.py"),
            (),
        )
        self.assertEqual(excluded("cya/kits/migrations/0002_old.py"), ())

    def test_every_fail_closed_route_runs_the_tagged_tests(self):
        self.assertEqual(excluded(query_failed=True), ())
        self.assertEqual(excluded(), ())
        self.assertEqual(excluded("cya/x.py", changed_files=3000), ())
        self.assertEqual(excluded("cya/x.py", changed_files="unknown"), ())
        self.assertEqual(excluded(".github/workflows/checks.yml"), ())
        self.assertEqual(excluded("Dockerfile", "cya/x.py"), ())
        self.assertEqual(excluded("cya/shell/templates/shell/unknown.dat"), ())

    def test_a_non_full_run_never_excludes_and_uses_one_job(self):
        for paths in ((TEMPLATE,), (SCRIPT,), ("README.md",), (sorted(ci_lanes.BROWSER_CSS)[0],)):
            with self.subTest(paths):
                decision = ci_lanes.classify(list(paths))
                self.assertNotEqual(decision["django-mode"], "full")
                self.assertEqual(decision["django-exclude-tags"], ())
                self.assertEqual(decision["django-shard-count"], 1)

    def test_full_runs_are_sharded_and_list_every_shard(self):
        self.assertGreaterEqual(ci_lanes.DJANGO_SHARDS, 1)
        for paths in (("cya/x.py",), ("cya/kits/migrations/0002_x.py",)):
            lines = ci_lanes.output_lines(ci_lanes.classify(list(paths)))
            count = ci_lanes.DJANGO_SHARDS
            self.assertIn(f"django-shard-count={count}", lines)
            shards = next(line for line in lines if line.startswith("django-shards="))
            self.assertEqual(json.loads(shards.partition("=")[2]), list(range(1, count + 1)))

    def test_main_reports_the_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out"
            sys.stdin, original = io.StringIO("cya/deficiencies/commands.py\n"), sys.stdin
            try:
                with redirect_stdout(io.StringIO()) as printed:
                    ci_lanes.main(["--github-output", str(target)])
            finally:
                sys.stdin = original
            written = target.read_text().splitlines()
        self.assertIn(f"django-exclude-tags={ci_lanes.MIGRATION_TAG}", written)
        self.assertIn("tests excluded", printed.getvalue())


def _module_path(name):
    """The repository file for a dotted cya module name, or None."""
    if not name or not name.startswith("cya"):
        return None
    base = ROOT.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate.relative_to(ROOT).as_posix()
    return None


def _imported_files(path):
    """Repository files that a module imports by absolute or relative cya name."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    package = ".".join(Path(path).with_suffix("").parts[:-1])
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            names = [module] + [f"{module}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            resolved = _module_path(name)
            if resolved:
                found.add(resolved)
    return found


def _is_test_support(path):
    return "/tests/" in path or path.startswith("cya/testing/")


class MigrationInputMapTests(unittest.TestCase):
    """The migration-input lists stay true to the repository (issue #158)."""

    def tagged_modules(self):
        marker = f'@tag("{ci_lanes.MIGRATION_TAG}")'
        return {
            path.relative_to(ROOT).as_posix()
            for path in ROOT.glob("cya/**/tests/**/test_*.py")
            if marker in path.read_text(encoding="utf-8")
        }

    def test_every_tagged_module_is_a_migration_input(self):
        tagged = self.tagged_modules()
        self.assertIn("cya/tests/test_audit_integrity_db.py", tagged)  # the scan finds them
        self.assertLessEqual(tagged, ci_lanes.MIGRATION_TEST_INPUTS)

    def test_test_support_imported_by_tagged_modules_is_a_migration_input(self):
        pending = sorted(self.tagged_modules())
        seen = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            pending.extend(p for p in _imported_files(path) if _is_test_support(p))
        # cya/testing/ is covered by MIGRATION_TOOLING; package __init__ modules hold no tests.
        support = {
            p for p in seen if not p.startswith("cya/testing/") and not p.endswith("/__init__.py")
        }
        self.assertIn("cya/deficiencies/tests/support.py", support)
        self.assertEqual(support, set(ci_lanes.MIGRATION_TEST_INPUTS))

    def test_every_application_module_a_migration_imports_is_listed(self):
        imported = set()
        for migration in ROOT.glob("cya/*/migrations/*.py"):
            for path in _imported_files(migration.relative_to(ROOT).as_posix()):
                if not ci_lanes.MIGRATION_PATH.match(path):
                    imported.add(path)
        # Package __init__ modules run on import but hold no migration logic here.
        imported = {path for path in imported if not path.endswith("/__init__.py")}
        self.assertIn("cya/accounts/fields.py", imported)
        self.assertEqual(imported, set(ci_lanes.MIGRATION_IMPORTS))

    def test_every_listed_input_exists(self):
        for path in ci_lanes.MIGRATION_IMPORTS | ci_lanes.MIGRATION_TEST_INPUTS:
            with self.subTest(path):
                self.assertTrue((ROOT / path).is_file())

    def test_no_focused_label_contains_a_tagged_module(self):
        # Focused runs never pass --exclude-tag, so they must not reach a tagged test.
        for labels in ci_lanes.UI_TESTS.values():
            for label in labels:
                prefix = label.replace(".", "/")
                for module in self.tagged_modules():
                    with self.subTest(label=label, module=module):
                        self.assertFalse(
                            module == f"{prefix}.py" or module.startswith(f"{prefix}/")
                        )


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
        reference = re.compile(r'\{%\s*(?:include|extends)\s+"([a-z_]+/[^" ]+)"')
        for template in sorted(ROOT.glob("cya/*/templates/**/*")):
            if not template.is_file():
                continue
            app = template.relative_to(ROOT).parts[1]
            for used_path in set(reference.findall(template.read_text(encoding="utf-8"))):
                used = used_path.split("/", 1)[0]
                if used_path in {
                    "shell/base.html",
                    "shell/save_state.html",
                    "shell/empty_state.html",
                    "shell/brand_mark.html",
                }:
                    source_path = f"cya/shell/templates/{used_path}"
                    with self.subTest(template=str(template.relative_to(ROOT)), uses=used_path):
                        self.assertEqual(ci_lanes.classify([source_path])["django-mode"], "full")
                    continue
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

    def test_each_script_route_has_an_existing_harness_that_reads_its_source(self):
        for source_name, harness_name in ci_lanes.SCRIPT_HARNESSES.items():
            source = ROOT / source_name
            harness = ROOT / harness_name
            with self.subTest(source=source_name):
                self.assertTrue(source.is_file())
                self.assertTrue(harness.is_file())
                self.assertIn(source.name, harness.read_text(encoding="utf-8"))

    def test_each_browser_script_exists_and_is_read_by_a_browser_smoke(self):
        sources = [
            (ROOT / smoke).read_text(encoding="utf-8") for smoke in ci_lanes.BROWSER_TEST_PATHS
        ]
        for script_path in ci_lanes.BROWSER_SCRIPTS:
            with self.subTest(script_path=script_path):
                self.assertTrue((ROOT / script_path).is_file())
                self.assertTrue(any(script_path in source for source in sources))

    def test_each_browser_css_path_exists_and_is_read_by_the_browser_smoke(self):
        # Any registered browser smoke may read a CSS file; at least one must.
        sources = [
            (ROOT / smoke).read_text(encoding="utf-8") for smoke in ci_lanes.BROWSER_TEST_PATHS
        ]
        for css_path in ci_lanes.BROWSER_CSS:
            with self.subTest(css_path=css_path):
                self.assertTrue((ROOT / css_path).is_file())
                self.assertTrue(any(css_path in source for source in sources))
                css = (ROOT / css_path).read_text(encoding="utf-8")
                for django_delimiter in ("{%", "{{", "{#"):
                    with self.subTest(css_path=css_path, delimiter=django_delimiter):
                        self.assertNotIn(django_delimiter, css)

    def test_each_browser_test_path_exists_and_runs_in_the_browser_job(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        browser_command = package["scripts"]["test:browser-smoke"]
        for path in ci_lanes.BROWSER_TEST_PATHS:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())
                self.assertIn(path, browser_command)


if __name__ == "__main__":
    unittest.main()
