#!/usr/bin/env python3
"""Route a pull request's changed files to CI lanes (issue #19).

Reads changed paths (both the new and the previous name of a rename) one per line on standard
input and decides, in one pass:

- run-django: whether the Django/MariaDB job runs at all;
- django-mode: "full" (lint, checks, the whole offline suite and the helper tests) or "focused"
  (lint, checks and only the test labels covering the changed server-rendered templates);
- django-labels: the manage.py test labels for that job;
- run-client-scripts: whether the Node client-script harnesses run;
- run-browser-smoke: whether the deterministic cross-viewport browser smoke runs.

It fails closed: a failed or empty file query, an unclassified path, a workflow change or a
template without focused coverage runs the full Django suite and both UI jobs. Documentation-only
paths need neither (the workflow's top-level path filter usually skips those PRs before any job
starts). Every value printed is one of the constants below, never text taken from a path.

Usage: tools/ci_lanes.py [--all | --query-failed] [--github-output FILE] < paths

--all is for events other than a pull request (push to main, manual dispatch): full suite and both
UI jobs.
"""

import argparse
import re
import sys

FULL_LABELS = ("cya",)
# GitHub's list-pull-request-files endpoint returns at most 3,000 files; at or above that count
# the returned list may be a prefix that misses a server path, so routing fails closed.
FILE_LIST_LIMIT = 3000

# Server-rendered template or client-asset paths: cya/<app>/templates/** or cya/<app>/static/**.
UI_PATH = re.compile(r"^cya/([a-z_]+)/(?:templates|static)/.+")
# The offline Node harnesses that execute the client scripts.
HARNESS_PATH = re.compile(r"^cya/[a-z_]+/tests/[a-z_]+_harness\.js$")

# The focused offline suites covering each app's server-rendered HTML, including apps whose
# templates include inspections templates, cross-app page tests, and the shared shell suite.
# An app missing here has no focused coverage, so its changed UI paths run the full suite.
UI_TESTS = {
    "attention": ("cya.attention", "cya.tests.test_background_refresh"),
    "authorization": (
        "cya.authorization",
        "cya.tests.test_role_administration_http",
        "cya.tests.test_role_transition_integration",
    ),
    "inspections": (
        "cya.inspections",
        "cya.attention",
        "cya.operations",
        "cya.tests.test_background_refresh",
    ),
    "operations": ("cya.operations", "cya.tests.test_background_refresh"),
    "shell": ("cya.tests.test_shell",),
}

# Shared shell templates that affect every extending/included page keep full coverage. Only the
# shell's leaf pages use its focused suite; an unmapped shell template also fails closed below.
SHELL_FULL_TEMPLATES = frozenset(
    {
        "cya/shell/templates/shell/base.html",
        "cya/shell/templates/shell/save_state.html",
    }
)
SHELL_FOCUSED_TEMPLATES = frozenset(
    {
        "cya/shell/templates/shell/home.html",
        "cya/shell/templates/shell/forbidden.html",
        "cya/shell/templates/shell/placeholder.html",
        "cya/shell/templates/shell/administration.html",
    }
)

# Only CSS files loaded directly by the browser fixture may bypass database checks.
BROWSER_CSS = frozenset(
    {
        "cya/shell/templates/shell/shell.css",
        "cya/inspections/templates/inspections/inspection.css",
    }
)

# Every context-bearing client script has an offline Node harness that reads its source.
SCRIPT_HARNESSES = {
    "cya/attention/templates/attention/readiness.js": "cya/attention/tests/readiness_script_harness.js",
    "cya/inspections/templates/inspections/checklist.js": "cya/inspections/tests/workflow_script_harness.js",
    "cya/inspections/templates/inspections/workflows.js": "cya/inspections/tests/workflow_script_harness.js",
    "cya/inspections/templates/inspections/vehicle.js": "cya/inspections/tests/vehicle_script_harness.js",
    "cya/operations/templates/operations/status.js": "cya/operations/tests/status_script_harness.js",
}

# Shared shell behavior is executed in Chromium rather than a mock-DOM harness.
BROWSER_SCRIPTS = frozenset({"cya/shell/templates/shell/shell.js"})
BROWSER_TEST_PATHS = frozenset({"tools/browser_smoke.mjs"})

# Documentation and repository metadata need neither lane.
NEUTRAL_PATH = re.compile(r"^(?:docs/.*|.*\.md|\.gitignore|\.gitattributes)$")
# Markdown read by tests, so a server input despite the extension: the contract manifest test
# reads the contracts document; cya/tests/test_asgi_server.py parses the runbook's launch command.
# tools/tests/test_ci_lanes.py fails if a test starts reading another document not listed here.
SERVER_DOCUMENTS = frozenset({"docs/data-model-contracts.md", "docs/deployment-runbook.md"})
# Paths known to affect only server checks (Python, migrations, dependencies, tests, tools).
SERVER_PATH = re.compile(
    r"^(?:cya/.+\.py|tests/.+|tools/.+|requirements/.+|manage\.py|pyproject\.toml|compose\.ya?ml)$"
)
# Workflow and CI configuration: run both lanes so the changed jobs are exercised.
WORKFLOW_PATH = re.compile(r"^\.github/.+")


def classify(paths, query_failed=False, changed_files=None):
    """Return the lane decision for an iterable of changed paths.

    changed_files is the PR's total changed-file count from the event; at or above the API's
    list limit, or when it is missing or malformed, the list may be incomplete.
    """
    paths = [p.strip() for p in paths if p.strip()]
    if changed_files is not None:
        try:
            count = int(changed_files)
        except (TypeError, ValueError):
            count = None
        if count is None or count < 0 or count >= FILE_LIST_LIMIT:
            return _decision(
                full=True,
                client=True,
                browser=True,
                reason=f"the PR's changed-file count ({changed_files!r}) may exceed the "
                f"{FILE_LIST_LIMIT}-file list limit or is unknown; running both lanes",
            )
    if query_failed or not paths:
        reason = "the changed-file query failed" if query_failed else "no changed files"
        return _decision(
            full=True, client=True, browser=True, reason=f"{reason}; running all lanes"
        )

    full = client = browser = False
    labels = []
    reasons = []
    for path in sorted(set(paths)):
        if path in SERVER_DOCUMENTS:
            full = True
            reasons.append(f"{path}: read by the contract manifest test")
        elif NEUTRAL_PATH.match(path):
            continue
        elif HARNESS_PATH.match(path):
            client = True
            reasons.append(f"{path}: client-script harness")
        elif match := UI_PATH.match(path):
            app_labels = UI_TESTS.get(match.group(1))
            if path in BROWSER_CSS:
                browser = True
                reasons.append(f"{path}: CSS; browser smoke")
            elif path in SCRIPT_HARNESSES:
                client = True
                if app_labels is None:
                    full = True
                    reasons.append(f"{path}: script without focused coverage; full suite")
                else:
                    labels.extend(app_labels)
                    reasons.append(f"{path}: tested client script; focused tests and harness")
            elif path in BROWSER_SCRIPTS:
                browser = True
                if app_labels is None:
                    full = True
                    reasons.append(f"{path}: browser script without focused coverage; full suite")
                else:
                    labels.extend(app_labels)
                    reasons.append(f"{path}: shared shell script; focused tests and browser smoke")
            elif path in SHELL_FULL_TEMPLATES:
                full = True
                browser = True
                reasons.append(f"{path}: shared shell template; full suite and browser smoke")
            elif path in SHELL_FOCUSED_TEMPLATES:
                labels.extend(UI_TESTS["shell"])
                browser = True
                reasons.append(f"{path}: shell leaf template; focused tests and browser smoke")
            elif path.endswith(".html") and app_labels is not None and match.group(1) != "shell":
                browser = True
                labels.extend(app_labels)
                reasons.append(f"{path}: server template; focused tests and browser smoke")
            else:
                full = True
                client = True
                browser = True
                reasons.append(f"{path}: UI path without explicit focused coverage; all lanes")
        elif path in BROWSER_TEST_PATHS:
            full = True
            browser = True
            reasons.append(f"{path}: browser test source; full suite and browser smoke")
        elif WORKFLOW_PATH.match(path):
            full = client = browser = True
            reasons.append(f"{path}: CI configuration; all lanes")
        elif SERVER_PATH.match(path):
            full = True
            reasons.append(f"{path}: server")
        else:
            full = client = browser = True
            reasons.append(f"{path}: unclassified; all lanes")

    if full:
        return _decision(full=True, client=client, browser=browser, reason="; ".join(reasons))
    if labels:
        unique = tuple(dict.fromkeys(labels))
        return _decision(labels=unique, client=client, browser=browser, reason="; ".join(reasons))
    return _decision(
        client=client, browser=browser, reason="; ".join(reasons) or "documentation only"
    )


def _decision(full=False, client=False, browser=False, labels=(), reason=""):
    if full:
        return {
            "run-django": True,
            "django-mode": "full",
            "django-labels": FULL_LABELS,
            "run-client-scripts": client,
            "run-browser-smoke": browser,
            "reason": reason,
        }
    return {
        "run-django": bool(labels),
        "django-mode": "focused" if labels else "none",
        "django-labels": tuple(labels),
        "run-client-scripts": client,
        "run-browser-smoke": browser,
        "reason": reason,
    }


def output_lines(decision):
    return [
        f"run-django={'true' if decision['run-django'] else 'false'}",
        f"django-mode={decision['django-mode']}",
        f"django-labels={' '.join(decision['django-labels'])}",
        f"run-client-scripts={'true' if decision['run-client-scripts'] else 'false'}",
        f"run-browser-smoke={'true' if decision['run-browser-smoke'] else 'false'}",
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--all", action="store_true", help="not a pull request: full suite and both UI jobs"
    )
    parser.add_argument("--query-failed", action="store_true")
    parser.add_argument("--changed-files", help="the PR's total changed-file count")
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)
    if args.all:
        decision = _decision(
            full=True, client=True, browser=True, reason="not a pull request; all lanes"
        )
    else:
        decision = classify(
            [] if args.query_failed else sys.stdin, args.query_failed, args.changed_files
        )
    print(f"Lane decision: {decision['reason']}")
    lines = output_lines(decision)
    for line in lines:
        print(line)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
