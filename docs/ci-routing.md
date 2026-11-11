# CI routing measurements and policy

This is a non-normative record for the change-aware checks in `tools/ci_lanes.py` and `.github/workflows/checks.yml`. Read it when changing CI routing or CI-host validation policy. Issue #113 supplied the baseline and the initial design.

## Baseline measured before issue #113

Representative successful runs from GitHub Actions (job durations are runner time; matrix jobs run in parallel):

| Change class | Pull request or event | Workflow run | Database job durations | Runner minutes | Wall time |
|---|---|---:|---:|---:|---:|
| UI-only (inspection JavaScript, CSS and harness) | PR #93 | [42761520920](https://github.com/JustApu/CheckYourAmbulance/actions/runs/42761520920) | MariaDB 10.11: 3.4 min; 11.8: 3.4 min | 6.8 | 3.7 min |
| Server-only (migration, model and tests) | PR #82 | [42626968571](https://github.com/JustApu/CheckYourAmbulance/actions/runs/42626968571) | MariaDB 10.11: 17.3 min; 11.8: 14.1 min | 31.4 | 17.6 min |
| Mixed server and UI | PR #96 | [42932174920](https://github.com/JustApu/CheckYourAmbulance/actions/runs/42932174920) | MariaDB 10.11: 13.3 min; 11.8: 17.1 min | 30.4 | 17.3 min |
| Markdown-only | PR #98 | no run created by the path filter | none | 0 | 0 |
| Push to `main` | merge of PR #96 | [42944269218](https://github.com/JustApu/CheckYourAmbulance/actions/runs/42944269218) | MariaDB 10.11: 16.9 min; 11.8: 10.7 min | 27.6 | 17.1 min |

The action timing includes checkout, container startup, MariaDB client installation, hash-checked dependency installation, Django checks/migrations and tests. The focused UI run spent 2.17 and 2.18 minutes in the focused test step, with about 0.9 minutes of job setup/checks per database. The full PR #82 test steps took 16.03 and 12.85 minutes. Ruff is run in both matrix jobs today, but each lint step rounded below the Actions API's 0.01-minute display resolution; moving that sub-minute work was not selected as a runtime target.

A Markdown-only run has no Actions job duration to measure: GitHub filtered the workflow before creating a run. The recorded zero is therefore the configured runner cost, not a measured job.

## Host validation before issue #113

`.github/workflows/checks.yml` is the repository's only workflow. It does not deploy to or run smoke tests on a rehearsal host. The Rocky Linux and Ubuntu host procedures in `docs/deployment-runbook.md`, and the load rehearsal in issue #10, are operator-run and separately scheduled. CI must not mutate a rehearsal or production host.

The change policy is:

- A UI-only CSS or HTML change is checked by the browser smoke lane; HTML still receives its focused Django template suite. No host deployment is triggered.
- Client JavaScript keeps its offline script harness and the owning app's focused Django suite. No host deployment is triggered.
- Python, migrations, dependencies, CI/deploy configuration, unknown paths, and changed-file lookup failures use the full server matrix and retain fail-closed routing. Such CI does not replace a host rehearsal when issue #6 or issue #10 requires one.
- A host install, deployment, or load rehearsal remains an explicit operator action under its issue/runbook, with synthetic data and owner authorization for host changes. A pre-release/full M5 verification remains required; there is no automatic skip or opt-in override for an absent host workflow.
- Pushes to `main` and `workflow_dispatch` continue to run the full checks. A manually dispatched run is the full-CI override for a PR head; compare its SHA with the PR head before using it as evidence.

## Issue #113 design and expected savings

The PR proposes three conservative routing changes:

1. Route shell templates to the existing `cya.tests.test_shell` suite. It already checks role-based navigation, server-side denials, signed-out pages, accessibility markers, save-state text, and shared responsive CSS. Previously the missing `shell` map entry failed closed to the full suite.
2. Route only the existing inspection and shell CSS files to a deterministic Chromium smoke test that reads the real CSS and shell JavaScript and uses synthetic page markup. It checks phone, tablet portrait/landscape, and desktop widths, horizontal overflow, navigation/accessibility labels, visible action/status distinctions, touch targets and the offline banner/save-state behavior. CSS-only changes do not need Django or MariaDB; an unknown UI path remains full-suite.
3. Keep HTML and context-bearing client-script changes on the owning focused Django tests. Client scripts also run the current offline Node harnesses. Unknown, server, migration, security, authorization, inspection/deficiency, and CI-config paths keep their existing full routing.

A CSS-only change currently starts both MariaDB jobs and takes about 3.4 wall minutes / 6.8 runner-minutes in the sampled focused UI run. The browser-only route removes those database jobs; the net runner-minute saving depends on the measured browser install/smoke duration. A shell-template change currently takes the full path (about 17 minutes wall and 31 runner-minutes in the server-only sample); the focused shell route is expected to be near the 3.4-minute focused UI sample, plus the browser smoke lane. These are estimates, not promised percentages. Compare new action job durations before claiming savings.
