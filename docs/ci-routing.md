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

- CSS-only changes in `BROWSER_CSS` use the deterministic browser smoke. The stack-neutral classifier tests reject Django template delimiters (`{%`, `{{`, `{#`) in each raw CSS source; because the files contain no template tags, the browser receives the same CSS that Django would render. A future template token fails the stack-neutral check, so it cannot bypass Django rendering coverage. The readiness stylesheet is included in the same smoke with synthetic readiness cards. Server-rendered HTML still uses focused Django tests and the browser smoke; only shell leaf templates are focused, while shared `base.html`, `save_state.html`, and unmapped shell templates run the full suite because other apps extend/include them. Client scripts use their mapped offline Node harness and focused owning Django suite. Scripts explicitly registered in `BROWSER_SCRIPTS` are instead executed by the Chromium smoke and run their owning focused Django tests. These UI changes do not deploy to a host.
- Python, migrations, dependencies, CI/deploy configuration, unknown paths, and changed-file lookup failures run the full server matrix. This is required CI coverage; it does not automatically install or deploy to a host.
- A change to `deploy/**`, runtime/server settings, dependencies, migrations, or the release build is assessed for a throwaway-host check under issue #6 and its runbook before the corresponding release/rehearsal is accepted. Issue #6 requires a deployable build and an owner/operator-provided host.
- Issue #10 lists issue #6 (host) and issue #7 Part A (restore timing) as dependencies, and its load criteria also require a deployable build and host. Follow the current issue status/dependency record when scheduling its rehearsal. The current-read rehearsal in issue #70 also needs the issue #67 projection and must be scheduled with the issue #10 host owner; do not overlap measurements on the same host/history.
- Before pilot/full M5 exit, the owner must record completion or an accepted exception for the host/runbook, restore, monitoring and load-verification child issues on M5 tracking issue #2. These are milestone gates; this policy creates no automatic or periodic host run.
- Host install, deployment, or load rehearsal is an explicit operator action with synthetic data and owner authorization for host changes. There is no CI flag to bypass those gates. `workflow_dispatch` is a full-CI override for a PR head, not a host-validation override; compare its SHA with the PR head before using it as evidence.

Pushes to `main` and `workflow_dispatch` continue to run the full Django matrix, client harnesses and browser smoke. A manually dispatched run is the full-CI override for a PR head; compare its SHA with the PR head before using it as evidence.

The browser-smoke job keeps Playwright 1.65.0 and its matching Chromium revision. Each browser job configures the runner's existing apt mirrors at priority 1 and `archive.ubuntu.com` at priority 2 before Playwright installs system dependencies. A manual `workflow_dispatch` run additionally probes failover by replacing each existing primary with a reserved unresolvable hostname and checking only the Ubuntu Noble `main` component through that mirror list. The diagnostic disables language indexes and APT retries and has a two-minute timeout. Its failure emits a warning and restores the runner's mirror list; it does not block a run when the normal primary is available. Normal mirror configuration and dependency installation remain fail-closed. Playwright's dependency installation gets at most two attempts, each limited to three minutes; the browser smoke itself is not retried. This setup touches only the ephemeral GitHub runner and leaves test routing and coverage unchanged. Run 43067234935 recorded a 31-second browser-smoke job, including the successful fallback probe, install and cross-device smoke; the earlier PR #114 browser job took about 35 seconds. This is one observation, about four seconds lower, and does not establish a repeatable runtime improvement.

## Issue #113 design and expected savings

The PR proposes three conservative routing changes:

1. Route only shell leaf templates (`home.html`, `forbidden.html`, `placeholder.html`, and `administration.html`) to the existing `cya.tests.test_shell` suite, which checks role-based navigation, server-side denials, signed-out pages, accessibility markers, save-state text, and shared responsive CSS. The shared `base.html` and `save_state.html` templates, plus unmapped shell templates, retain the full suite because application templates extend/include them. Previously the missing `shell` map entry failed closed to the full suite.
2. Route only the existing inspection and shell CSS files to a deterministic Chromium smoke test that reads the real CSS and shell JavaScript and uses synthetic page markup. It checks phone, tablet portrait/landscape, and desktop widths, horizontal overflow, navigation/accessibility labels, visible action/status distinctions, touch targets and the offline banner/save-state behavior. CSS-only changes do not need Django or MariaDB; a stack-neutral test rejects Django template delimiters in these CSS source files, preserving equivalence between raw and rendered CSS. An unknown UI path remains full-suite.
   Issue #103 extends the same smoke to run the real checklist script with a synthetic checklist API at the same viewport sizes: compartment progress and kit-instance grouping, the flat and attention-filter views, touch targets/overflow, and per-user/session resume isolation. The script records no real user or vehicle data.
   Issue #101 adds `tools/vehicle_landing_smoke.mjs` for the inspection landing page and registers it in `BROWSER_TEST_PATHS`; a change to that test source runs both the full Django suite and browser smoke.
   Issue #107 extends the same cross-device smoke with the deficiency detail summary, visible next action, initially collapsed secondary actions, keyboard expansion/collapse, and the disclosure touch target.
   The role-administration browser journey reads and executes `cya/authorization/templates/authorization/roles.js`; it checks the staged checkbox change, keyboard review/confirmation, responsive layout and forced-colors styling. This script is listed in `BROWSER_SCRIPTS` and routed to the focused authorization tests plus browser smoke.
3. Keep HTML and context-bearing client-script changes on the owning focused Django tests. Client scripts use their explicit Node harness or browser-smoke route. Unknown, server, migration, security, authorization, inspection/deficiency, and CI-config paths keep their existing full routing unless a path has a focused route above.

A CSS-only change currently starts both MariaDB jobs and accounts for 6.8 of the 7.0 runner-minutes in the sampled focused UI workflow. The browser-only route removes those database jobs. The browser job on PR #114 full-check run 42958166677 took 35 seconds (about 0.6 runner-minutes), so the estimated net saving for a CSS-only change is about 6.2 runner-minutes. This estimate subtracts the measured browser cost from the prior UI-run database cost; an actual CSS-only lane total has not yet been observed. Playwright 1.65.0 installs its matching Chromium revision; the downloaded browser archive is not separately verified against a pinned digest. Shared shell templates use the full suite, while the four shell leaf pages use `cya.tests.test_shell`; savings for those leaf pages are estimated pending a representative shell-leaf PR. These are estimates, not promised percentages. Compare new action job durations before claiming observed lane savings.

## Issue #158: migration test tags

Tests that reverse or reapply migrations carry the Django test tag `migration_rollback`, and `tools/migration_test_tags.py` (run with the tool tests) fails if a migrating test class is missing it. Since item 4 option 2 (below), CI uses the tag to leave these tests out of pull requests that cannot affect them.

Measured on the Rocky 9 / MariaDB 10.11 test host on 2026-11-15 (NFS-backed storage, synthetic data):
- `AuditSchemaLifecycleTests.test_integrity_migrations_reverse_and_reapply_on_empty_disposable_schema`: 410 s;
- nine deficiency migration rollback tests: 59 to 144 s each.

Those ten tests took about 22 of the 22.5 minutes of a 26-test run. These are test-host timings; compare GitHub job durations before claiming CI savings.

### Item 4, option 1: one test process per CPU

The owner approved option 1 of the item 4 proposal on 2026-11-15 ([issue #158](https://github.com/JustApu/CheckYourAmbulance/issues/158)). The Django test step now runs one process per runner CPU, at most 4, and prints both numbers; previously it ran 2. Coverage is unchanged.

Before the change, from PR runs 43675239946 and 43674589307:
- Django jobs took 19 to 27 minutes, and the test step 1119 to 1507 s (about 94% of the job). Container start, dependency install and migrations together took about 1.5 minutes.
- The tagged migration tests took roughly 214 to 242 s of the test step (15 to 18%). This was estimated from per-class result timestamps with 2 processes, so it is approximate. They are cheaper in CI than on the test hosts because the CI database runs on tmpfs with flushes off.

Each process gets its own copy of the test database, about 15 MB each in a local run, on the service's 1 GiB tmpfs. After the change, the test step took 884 s (MariaDB 11.8) and 1073 s (10.11).

### Item 4, options 2 and 3: route the migration tests and shard the suite

The owner deferred options 2 and 3 on 2026-11-16 and asked to revisit them on 2026-11-19. Before this change, on 2026-11-19, full Django jobs took 17 to 19 minutes, of which the test step took 15.5 to 17.5 minutes (PR runs [44581299409](https://github.com/JustApu/CheckYourAmbulance/actions/runs/44581299409), [44574198877](https://github.com/JustApu/CheckYourAmbulance/actions/runs/44574198877) and [44569928424](https://github.com/JustApu/CheckYourAmbulance/actions/runs/44569928424), and push run [44581105734](https://github.com/JustApu/CheckYourAmbulance/actions/runs/44581105734)). Two runs measured the change on 2026-11-19, both green with 3 shards:

| Run | Migration tests | Django jobs (6) | Test steps | Runner minutes, Django jobs | Wall time |
|---|---|---|---|---:|---:|
| Before: 4 runs above (2 jobs each) | included | 17 to 19 min | 15.5 to 17.5 min | 34 to 38 | 17 to 19 min |
| PR #232 handoff, [44590447850](https://github.com/JustApu/CheckYourAmbulance/actions/runs/44590447850) | included (the PR changes the classifier) | 6.8 to 12.5 min | 333 to 647 s | 53.1 | 12.7 min |
| Measurement PR #233, [44590519874](https://github.com/JustApu/CheckYourAmbulance/actions/runs/44590519874) (one non-migration Python file) | excluded | 5.3 to 8.8 min | 247 to 430 s | 38.7 | 9.1 min |

- Wall time falls by a quarter to a third for a run that includes the migration tests, and by about half for an ordinary PR. Runner minutes rise by about half when the migration tests run, because each extra job repeats about 1.5 minutes of setup and creates its own test database; an ordinary PR uses about the same runner minutes as before.
- The shards are uneven when the migration tests run: the class hash put 13 of the 22 tagged tests in shard 1, whose MariaDB 11.8 job was the slowest at 12.5 minutes. Its `--durations` list was led by the audit lifecycle test (154 s), eight migration-reversal tests (47 to 95 s each) and four seeded projection-history tests (36 to 61 s). Without them the shards are within 3.5 minutes of each other. A change of `DJANGO_SHARDS` moves classes between shards; it should be measured the same way before it is adopted.

**Option 3, sharding.** A full run splits the Django suite into `DJANGO_SHARDS` jobs (3) per MariaDB version, so 6 jobs instead of 2. Each job passes `--shard K/N` to the project test runner (`cya/testing/runner.py`), which keeps only the test classes whose SHA-256 hash of module and class name falls in shard K (`cya/testing/sharding.py`). Every job computes the same split independently, and a class always stays whole in one shard, so class-level setup and Django's per-class process split are unchanged. A test module that fails to import is reported in every shard. A shard that selects no tests from a non-empty suite stops the run instead of passing.

- `cya/tests/test_test_sharding.py` loads the whole suite and checks that the configured shard count, run through the real runner, puts every test and every class in exactly one non-empty shard; it checks other counts with the same function.
- Lint, the audit helper tests in `tests/` and the authorization policy tests run once, in shard 1.
- Focused (template) runs stay in one job. Each extra job repeats the container start, dependency install, migrations and test-database creation, about 2 to 3 minutes of runner time, so sharding uses more runner minutes in exchange for shorter wall time.
- The test step prints the 15 slowest tests (`--durations 15`), so an unbalanced shard can be seen and the count or split revisited.

**Option 2, routing the migration tests.** On a pull request, a full run passes `--exclude-tag migration_rollback` unless the PR changes one of these inputs (`is_migration_input` in `tools/ci_lanes.py`):

- any file in an app's `migrations/` package;
- an application module a migration imports (`MIGRATION_IMPORTS`);
- a test module with a tagged class, or a test-support module those modules import, directly or through each other (`MIGRATION_TEST_INPUTS`);
- the shared test package `cya/testing/`, settings, the database session checks in `cya/core/db.py`, `manage.py`, `pyproject.toml`, dependencies, the tag check, and the classifier and its tests.

The tool tests fail if a tagged module, a test-support module it imports, or a module a migration imports is missing from these lists, and check that no focused label reaches a tagged module. Every fail-closed route also runs the tagged tests: a failed, empty or possibly truncated changed-file query, CI configuration, an unclassified path, and a UI path without focused coverage. A PR that changes migrations therefore keeps the same handoff coverage as before ([issue #158](https://github.com/JustApu/CheckYourAmbulance/issues/158) acceptance criteria).

Pushes to `main`, manual dispatches and a new nightly scheduled run (10:23 UTC, `main` only) run the whole suite, including the tagged tests. The trade-off, which is the owner's decision in approving this option: a PR that changes application code without touching any migration input, but still breaks a migration test (for example through a model or service the test uses after reapplying migrations), is caught by the `main` push run or the nightly run after merge, not before. GitHub notifies the author of the most recent change to the schedule when a scheduled run fails, and disables schedules in a public repository after 60 days without repository activity.
