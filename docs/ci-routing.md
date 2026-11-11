# CI routing measurements and policy

This is a non-normative record for the change-aware checks in `tools/ci_lanes.py` and `.github/workflows/checks.yml`. Read it when changing CI routing or CI-host validation policy. Issue #113 supplied the baseline and the initial design.

## Baseline measured before issue #113

Representative successful runs from GitHub Actions (job durations are runner time; matrix jobs run in parallel):

| Change class | Pull request or event | Workflow run | Database job durations | Runner minutes | Wall time |
|---|---|---:|---:|---:|---:|
| UI-only (inspection JavaScript, CSS and harness) | PR #93 | [42761520920](https://github.com/JustApu/CheckYourAmbulance/actions/runs/42761520920) | MariaDB 10.11: 3.4 min; 11.8: 3.4 min | 7.0 | 3.7 min |
| Server-only (migration, model and tests) | PR #82 | [42626968571](https://github.com/JustApu/CheckYourAmbulance/actions/runs/42626968571) | MariaDB 10.11: 17.3 min; 11.8: 14.1 min | 31.6 | 17.6 min |
| Mixed server and UI | PR #96 | [42932174920](https://github.com/JustApu/CheckYourAmbulance/actions/runs/42932174920) | MariaDB 10.11: 13.3 min; 11.8: 17.1 min | 30.7 | 17.3 min |
| Markdown-only | PR #98 | no run created by the path filter | none | 0 | 0 |
| Push to `main` | merge of PR #96 | [42944269218](https://github.com/JustApu/CheckYourAmbulance/actions/runs/42944269218) | MariaDB 10.11: 16.9 min; 11.8: 10.7 min | 27.8 | 17.1 min |

Runner minutes sum every non-skipped job, including checkout and shared checks. The shared `stack-neutral` job took 0.08–0.18 minutes, running the lane classifier, check-tool tests, Markdown links, scenario/contract validation and Gitleaks; the client harness job took 0.10–0.12 minutes when selected. The focused UI run spent 2.17 and 2.18 minutes in the focused Django test step, with about 0.9 minutes of setup/checks per database (container startup, checkout, MariaDB client install, pinned dependency install, lint and migrations). The full PR #82 test steps took 16.03 and 12.85 minutes. Ruff is run in both matrix jobs today, but each lint step rounded below the Actions API's 0.01-minute display resolution; moving that sub-minute work was not selected as a runtime target.

The same job details show pinned Python dependency installation at 0.13–0.20 minutes (about 8–12 seconds) and MariaDB container initialization at 0.38–0.60 minutes. Pip caching was evaluated but not added: the measured install is short, each database job has its own runner, and a cross-job cache would add restore/save work and invalidation state. The existing matrix starts the two MariaDB services in parallel and stores their data on a 1 GiB tmpfs; sharing a database or removing a supported version would weaken the required coverage, so startup is retained. The suite itself dominates full runs.

A Markdown-only run has no Actions job duration to measure: GitHub filtered the workflow before creating a run. The recorded zero is therefore the configured runner cost, not a measured job.

## Test-host validation policy

`.github/workflows/checks.yml` is the repository's only workflow. It does not deploy to or run smoke tests on a rehearsal host. The Rocky Linux and Ubuntu procedures in `docs/deployment-runbook.md`, and the load rehearsal in issue #10, are operator-run and separately scheduled. CI must not mutate a rehearsal or production host.

The path-trigger policy is:

- CSS-only changes in `BROWSER_CSS` use the deterministic browser smoke; server-rendered HTML still uses focused Django tests and the browser smoke; JavaScript keeps its offline harness and focused Django suite. These UI changes do not deploy to a host.
- Python, migrations, dependencies, CI/deploy configuration, unknown paths, and changed-file lookup failures run the full server matrix. This is required CI coverage; it does not automatically install or deploy to a host.
- A change to `deploy/**`, runtime/server settings, dependencies, migrations, or the release build is assessed for a throwaway-host check under issue #6 and its runbook before the corresponding release/rehearsal is accepted. Issue #6 requires a deployable build and an owner/operator-provided host.
- Issue #10 lists issue #6 (host) and issue #7 Part A (restore timing) as dependencies, and its load criteria also require a deployable build and host. Follow the current issue status/dependency record when scheduling its rehearsal. The current-read rehearsal in issue #70 also needs the issue #67 projection and must be scheduled with the issue #10 host owner; do not overlap measurements on the same host/history.
- Before pilot/full M5 exit, the owner must record completion or an accepted exception for the host/runbook, restore, monitoring and load-verification child issues on M5 tracking issue #2. These are milestone gates; this policy creates no automatic or periodic host run.
- Host install, deployment, or load rehearsal is an explicit operator action with synthetic data and owner authorization for host changes. There is no CI flag to bypass those gates. `workflow_dispatch` is a full-CI override for a PR head, not a host-validation override; compare its SHA with the PR head before using it as evidence.

Pushes to `main` and `workflow_dispatch` continue to run the full Django matrix, client harnesses and browser smoke. A manually dispatched run is the full-CI override for a PR head; compare its SHA with the PR head before using it as evidence.

## Issue #113 design and expected savings

The PR proposes three conservative routing changes:

1. Route shell templates to the existing `cya.tests.test_shell` suite. It already checks role-based navigation, server-side denials, signed-out pages, accessibility markers, save-state text, and shared responsive CSS. Previously the missing `shell` map entry failed closed to the full suite.
2. Route only the existing inspection and shell CSS files to a deterministic Chromium smoke test that reads the real CSS and shell JavaScript and uses synthetic page markup. It checks phone, tablet portrait/landscape, and desktop widths, horizontal overflow, navigation/accessibility labels, visible action/status distinctions, touch targets and the offline banner/save-state behavior. CSS-only changes do not need Django or MariaDB; an unknown UI path remains full-suite.
3. Keep HTML and context-bearing client-script changes on the owning focused Django tests. Client scripts also run the current offline Node harnesses. Unknown, server, migration, security, authorization, inspection/deficiency, and CI-config paths keep their existing full routing.

A CSS-only change currently starts both MariaDB jobs and accounts for 6.8 of the 7.0 runner-minutes in the sampled focused UI workflow. The browser-only route removes those database jobs. The browser job on PR #114 full-check run 42958166677 took 35 seconds (about 0.6 runner-minutes), so the estimated net saving for a CSS-only change is about 6.2 runner-minutes. This estimate subtracts the measured browser cost from the prior UI-run database cost; an actual CSS-only lane total has not yet been observed. A shell-template change currently takes the full path (about 17 minutes wall and 31.6 runner-minutes in the server-only sample); the focused shell route should avoid the full-suite test steps, with its actual savings to be measured on a future shell-template PR. These are estimates, not promised percentages. Compare new action job durations before claiming observed lane savings.
