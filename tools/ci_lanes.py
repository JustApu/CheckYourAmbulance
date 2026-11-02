#!/usr/bin/env python3
"""Route a pull request's changed files to CI lanes (issue #19).

Reads changed paths (both the new and the previous name of a rename) one per line on standard
input and decides, in one pass:

- run-django: whether the Django/MariaDB job runs at all;
- django-mode: "full" (lint, checks, the whole offline suite and the helper tests) or "focused"
  (lint, checks and only the test labels covering the changed server-rendered templates);
- django-labels: the manage.py test labels for that job;
- run-client-scripts: whether the Node client-script harnesses run.

It fails closed: a failed or empty file query, an unclassified path, a workflow change or a
template without focused coverage runs both lanes in full. Documentation-only paths need neither
(the workflow's top-level path filter usually skips those PRs before any job starts). Every value
printed is one of the constants below, never text taken from a path.

Usage: tools/ci_lanes.py [--all | --query-failed] [--github-output FILE] < paths

--all is for events other than a pull request (push to main, manual dispatch): both lanes in full.
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

# The smallest offline suites covering each app's templates, including the apps whose templates
# include them (operations and attention include inspections templates) and the cross-app page
# tests in cya/tests. An app missing here (for example shell, whose base template every page
# extends) has no focused coverage, so a change to it runs the full suite.
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
}

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
                reason=f"the PR's changed-file count ({changed_files!r}) may exceed the "
                f"{FILE_LIST_LIMIT}-file list limit or is unknown; running both lanes",
            )
    if query_failed or not paths:
        reason = "the changed-file query failed" if query_failed else "no changed files"
        return _decision(full=True, client=True, reason=f"{reason}; running both lanes")

    full = client = False
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
            client = True
            app_labels = UI_TESTS.get(match.group(1))
            if app_labels is None:
                full = True
                reasons.append(f"{path}: template without focused coverage; full suite")
            else:
                labels.extend(app_labels)
                reasons.append(f"{path}: template; focused tests")
        elif WORKFLOW_PATH.match(path):
            full = client = True
            reasons.append(f"{path}: CI configuration; both lanes")
        elif SERVER_PATH.match(path):
            full = True
            reasons.append(f"{path}: server")
        else:
            full = client = True
            reasons.append(f"{path}: unclassified; both lanes")

    if full:
        return _decision(full=True, client=client, reason="; ".join(reasons))
    if labels:
        unique = tuple(dict.fromkeys(labels))
        return _decision(labels=unique, client=client, reason="; ".join(reasons))
    return _decision(client=client, reason="; ".join(reasons) or "documentation only")


def _decision(full=False, client=False, labels=(), reason=""):
    if full:
        return {
            "run-django": True,
            "django-mode": "full",
            "django-labels": FULL_LABELS,
            "run-client-scripts": client,
            "reason": reason,
        }
    return {
        "run-django": bool(labels),
        "django-mode": "focused" if labels else "none",
        "django-labels": tuple(labels),
        "run-client-scripts": client,
        "reason": reason,
    }


def output_lines(decision):
    return [
        f"run-django={'true' if decision['run-django'] else 'false'}",
        f"django-mode={decision['django-mode']}",
        f"django-labels={' '.join(decision['django-labels'])}",
        f"run-client-scripts={'true' if decision['run-client-scripts'] else 'false'}",
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--all", action="store_true", help="not a pull request: both lanes")
    parser.add_argument("--query-failed", action="store_true")
    parser.add_argument("--changed-files", help="the PR's total changed-file count")
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)
    if args.all:
        decision = _decision(full=True, client=True, reason="not a pull request; both lanes")
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
