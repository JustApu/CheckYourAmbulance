# Contributing

**Current stage:** specification approved; a Django project contains sign-in and backend feature foundations under incremental development, but is not a production application. Non-Markdown PRs start the `checks` workflow at review handoff; stack-neutral checks run, with Django/MariaDB selected by the workflow's changed-file classifier. Qualifying Markdown-only PRs skip remote workflows and use the local verification gate in [review procedures](docs/review.md#documentation-only-handoff-verification). Draft PRs skip CI. Start with [README.md](README.md) and [SPEC.md](SPEC.md), the authoritative behavior/architecture source. [REQUIREMENTS.md](REQUIREMENTS.md), [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md), and [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md) are the historical discovery record.

Start with [AGENTS.md](AGENTS.md), the mandatory entry point for engineering and coordination rules. Its linked-document registry identifies authority, read triggers, and Git content blob IDs so retained guidance can be reused until it changes.

For how the roadmap is tracked (milestone tracking issues, `Depends on`, ready and blocked labels, parallel work) and for issue claiming, labels, race resolution, branch ownership, stale takeover, owner escalation, and PR completion, follow [coordination procedures](docs/coordination.md). For independent review, reviewer assignment, and merge readiness, follow [review procedures](docs/review.md). For code and automation work, follow [engineering standards](docs/engineering-standards.md) and the project conventions in AGENTS.md.

The Definition of Done, security, testing, data hygiene, filesystem hygiene, and mandatory model-tier rules remain in AGENTS.md. [Model-efficiency examples](docs/model-efficiency.md) provide optional operational guidance.

This page is a non-normative navigation aid; the linked authoritative documents contain the rules. When changing a registered document, follow the blob-ID update requirement in AGENTS.md.

## Verification for the current documentation stage

For a documentation-only PR:
1. Confirm every changed path is a `.md` file. Inspect renames and file counts locally; if a rename has a non-Markdown side, or there are more than 3,000 changed paths, verify the workflow ran at the current PR head and manually dispatch it if needed.
2. Run the applicable local checks listed in [review procedures](docs/review.md#documentation-only-handoff-verification), including the targeted contract manifest test when `docs/data-model-contracts.md` changes.
3. Review changed Markdown for broken relative links, contradictory decisions, unapproved claims, credentials, personal data, and sensitive operational data; confirm consistency with `SPEC.md` and its decision register.
4. Record which checks were actually run; do not claim code tests, remote CI, or deployment verification that did not run. Complete the documentation audit and owner Fresh Eyes check before review handoff.

## Repository checks

The [`checks` workflow](.github/workflows/checks.yml) runs at handoff for PRs with any non-Markdown path, on pushes to `main`, and when manually dispatched; draft PRs skip jobs. Markdown-only PR changes do not create a workflow run, so their pushes cannot reach the `synchronize` guard; the rule to return a ready PR to draft before pushing still applies but is not CI-enforced for Markdown-only changes. Do not trigger remote CI while implementation and owner checks are still in progress. Push updates only while the PR is draft; each new head needs fresh owner checks. For Markdown-only PRs, follow the local verification gate in [review procedures](docs/review.md#documentation-only-handoff-verification), including the pinned Gitleaks scan; if `docs/data-model-contracts.md` changes, also run its targeted contract manifest test locally. Changes under authorization-policy paths still run the separate `authorization-policy` workflow. GitHub path filtering examines at most 3,000 changed files; if a PR exceeds that limit or renames a non-Markdown path to Markdown and no `checks` run is present for the current head, manually dispatch the full workflow against the PR head branch and verify the run SHA. The stack-neutral job uses Python 3.10 or later and git; its Gitleaks 8.30.1 binary is pinned and checksum-verified in CI. Run relevant checks locally from the repository root:

```sh
python3 -m unittest discover -s tools/tests   # tests for the check tools themselves
python3 tools/check_markdown_links.py         # relative links and heading anchors in tracked *.md files
python3 tools/validate_scenarios.py           # tests/ scenario, contract and fixture structure and coverage
gitleaks git --redact --no-banner --timeout 300 .  # Markdown-only handoff secret scan (Gitleaks 8.30.1)
```

Each check exits 0 on success, 1 when it finds problems (listed as `file:line: message` or `file: message`), and 2 on a usage or read error. External URLs are not fetched. The scenario validator checks structure and SPEC traceability only; it does not run application behavior.

## Python and Django

The approved stack is Python with Django and MariaDB 10.11 on Linux ([SPEC §17.5](SPEC.md#175-hosting-database-engine-backup-oq-24)). The Django project lives in `manage.py` and package `cya/`; its incremental backend foundations are summarized in [README.md](README.md). The inherited Perl, Mojolicious and SQLite examples in older guidance do not apply.

**Prerequisites.** Python 3.12 or later (CI uses 3.13; the suite has been run on 3.12 and 3.13). Building the `mysqlclient` driver needs a C compiler, `pkg-config` and the MariaDB Connector/C headers (on Debian/Ubuntu: `build-essential pkg-config libmariadb-dev`). Those are host/system packages, so agents need owner approval to install them; the alternative is to run the commands inside the same pinned `python:3.13-bookworm` image CI uses, which already contains them. The development database runs in Docker with Compose v2.

Run from the repository root:

```sh
python3 -m venv local/venv                                         # project-local, gitignored
local/venv/bin/pip install --require-hashes -r requirements/dev.txt
docker compose up -d --wait db                                     # MariaDB 10.11 on 127.0.0.1:3306
export CYA_SECRET_KEY=dev-only-not-a-secret CYA_DB_USER=cya_owner CYA_DB_PASSWORD=dev-only-owner-password CYA_DATABASE_ROLE=schema_owner

local/venv/bin/ruff check .                                         # lint
local/venv/bin/ruff format --check .                                # formatting (ruff format . applies it)
local/venv/bin/python manage.py check --fail-level WARNING          # Django system checks
local/venv/bin/python manage.py makemigrations --check --dry-run    # fails if a model change lacks a migration
local/venv/bin/python manage.py migrate --noinput                   # apply migrations (schema-owner account)
local/venv/bin/python manage.py test cya --noinput                  # offline tests against MariaDB

docker compose down                                                 # stop; add -v to delete the dev database
```

The `django` job in the [`checks` workflow](.github/workflows/checks.yml) runs the same lint, check, migration and test commands in the pinned Python image against a MariaDB 10.11 service container. It runs on pushes to `main`, manual dispatches, and non-Markdown PRs that change application, test, migration, dependency, or other applicable files. Markdown-only PRs skip the workflow entirely; when `docs/data-model-contracts.md` changes, run `python3 -m unittest discover -s tests -p 'test_contract_manifest.py' -v` locally. Do not configure a path-filtered workflow as a required status check: GitHub leaves its check pending when it skips the workflow. The [`authorization-policy` workflow](.github/workflows/authorization.yml) runs on pushes to `main` and on pull requests touching authorization policy, contract or test files.

**Configuration** comes only from environment variables; nothing secret is committed ([SPEC §17.6](SPEC.md#176-configuration-and-secrets-p)). A missing required variable stops startup with an error.

| Variable | Default | Meaning |
|---|---|---|
| `CYA_SECRET_KEY` | required | Django secret key |
| `CYA_DB_USER`, `CYA_DB_PASSWORD` | required | Database account |
| `CYA_DATABASE_ROLE` | `runtime` | Runtime privilege checks on every new connection; explicitly use `schema_owner` only for operator migrations/tests/setup |
| `CYA_DB_HOST`, `CYA_DB_PORT` | `127.0.0.1`, `3306` | Database server |
| `CYA_DB_NAME`, `CYA_DB_TEST_NAME` | `cya`, `test_cya` | Database, and the one the test runner creates and destroys |
| `CYA_DEBUG` | `0` | `1`/`true` enables Django debug mode; never in production |
| `CYA_ALLOWED_HOSTS` | `localhost` | Comma-separated host names; `*` is rejected |
| `CYA_SESSION_MAX_AGE` | `28800` | Session lifetime in seconds (300 to 86400); bounds Workspace revocation |
| `CYA_GOOGLE_CLIENT_ID`, `CYA_GOOGLE_CLIENT_SECRET`, `CYA_GOOGLE_HOSTED_DOMAIN`, `CYA_GOOGLE_REDIRECT_URI` | unset | Google Workspace sign-in; unset means sign-in is unavailable. See [Google Workspace sign-in](docs/google-workspace-signin.md) |
| `CYA_ROLE_LOOKUP` (a Django setting, not an environment variable) | `cya.authorization.persistence.current_role_codes` | Current uncached contract role codes for navigation and authentication audit. Missing/failed lookup denies access; see [Application shell](docs/application-shell.md). |

The values in the example above, `compose.yaml` and `tools/dev-db/accounts.sql` are synthetic development values. **Database sessions:** every connection sets UTC, strict `sql_mode`, foreign-key and CHECK enforcement and READ COMMITTED, and `cya.core.db` refuses any connection whose actual MariaDB session settings differ ([SPEC §17.7](SPEC.md#177-design-rules-adopted-from-the-spike-for-django--mariadb-p) item 4).

**Migrations** run with the schema-owner account (`cya_owner` in development; SPEC §17.7 item 1). Runtime connections default to privilege verification and reject schema-owner powers. Follow the [audit operator procedure](docs/audit-implementation-notes.md#operator-procedure) to preview/install narrow grants on an existing account and verify the schema. `CYA_DB_SECURITY_TESTS=1` enables the disposable DBA tests (CI enables it); they create/drop synthetic runtime accounts and roles, so use only an isolated test server with account-administration privileges. `django.contrib.auth` is not installed: identity is the `cya.accounts` app (organization and user, keyed by identity-provider issuer and subject), and there is no password login (SPEC AUTH-5). Setup creates the single organization row with `manage.py create_organization` (idempotent, `--dry-run`).

On binary-log-enabled MariaDB 10.11, trigger migrations fail with error 1419
even with schema-level privileges unless the production server sets
`log_bin_trust_function_creators=1`. The owner chose that setting over granting
the schema owner SUPER ([SPEC §17.8](SPEC.md#178-deployment-secrets-and-recovery-oq-24-issue-16)
DEP-5, [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16));
it lives in the server configuration, not in a migration. Preserve trigger
DEFINER accounts during rotation and restore. The operator procedure documents
these prerequisites; keep the runtime account unprivileged and retain binary
logging for recovery.

The [authorization operator procedure](docs/authorization-implementation.md#sign-in-and-first-administrator-operator-procedure) documents the installed role/audit collaborators and the first-administrator bootstrap command, including its read-only `--dry-run`.

**Tests** run with `cya.testing.runner.OfflineTestRunner`. It blocks Python-level network access for the whole run (connections, UDP sends and name lookups to anything but loopback or Unix sockets raise `NetworkAccessBlocked`). The database driver is a C extension and is not intercepted, so tests reach only the configured MariaDB. Database tests need MariaDB 10.11; SQLite is not a substitute (locking and privileges differ).

- **Targeted test execution:** Rather than running the entire `cya` test suite during focused development, run specific application suites to save time and compute:
  ```sh
  local/venv/bin/python manage.py test cya.kits --noinput
  local/venv/bin/python manage.py test cya.checks --noinput
  local/venv/bin/python manage.py test cya.inspections --noinput
  ```
  You can also target individual test classes or test methods:
  ```sh
  local/venv/bin/python manage.py test cya.kits.tests.test_history.ActiveKitsTests --noinput
  local/venv/bin/python manage.py test cya.kits.tests.test_history.ActiveKitsTests.test_membership_follows_the_recorded_history_in_time --noinput
  ```
- **Order and parallel runs:** Run the tests of the module you changed (and DB-free ones such as the transition-table tests) first, then the whole application suite, so mistakes in new tests show up in minutes rather than after a long run. Every run creates and migrates its own test database, so batch several labels into one command. Concurrent runs need distinct `CYA_DB_TEST_NAME` values (for example `test_cya_a` and `test_cya_b`), and a wrapper `timeout` must exceed the suite's runtime because a killed run prints no result summary.
- **Token-saving and debugging flags:**
  - `--failfast`: Stop on the first failure instead of running the remaining tests and generating voluminous traceback logs.
  - `-v 1` (default): Minimal output per test (`.` for pass, `F` for fail). Avoid `-v 2` or `-v 3` in automated agent contexts unless debugging a specific test to keep terminal output and context sizes compact.
- **Lightweight offline checks (no database required):** For changes to documentation, tools, scenarios, or links, run the offline checks without spinning up MariaDB:
  ```sh
  python3 -m unittest discover -s tools/tests -v
  python3 tools/check_normative_registry.py
  python3 tools/check_markdown_links.py
  python3 tools/validate_scenarios.py
  ```
  `tools/tests/test_normative_registry.py` runs in CI `stack-neutral` and verifies that the Git blob IDs in `AGENTS.md` match the current contents of all registered normative documents. If you modify any registered document (`CONTRIBUTING.md`, `docs/coordination.md`, `docs/review.md`, `docs/engineering-standards.md`, or `docs/model-efficiency.md`), update its blob ID in `AGENTS.md` (obtained via `git hash-object <path>`) in the same commit to pass CI.

**Fixtures and stubs.** `tests/fixtures/` is the single fixture directory: hand-written synthetic JSON objects marked `"synthetic": true`, loaded with `cya.testing.fixtures.load_fixture()`, which refuses unmarked or malformed files. External services are reached only through `cya.integrations` (`get_oidc_provider()`, `get_roster_provider()`), which fail closed when unconfigured. Tests inject the offline stubs in `cya.testing.stubs`: `override_settings(**stub_google_settings())` after `activate_stub_google()` wires sign-in to `StubGoogle` (synthetic keys generated at test time) plus recording audit and role-grant stubs; `CYA_ROSTER_PROVIDER="cya.testing.stubs.fixture_roster_provider"` injects the roster stub.

**Scenario runner.** `cya/tests/test_scenarios.py` turns each file in `tests/scenarios/` into a test. Scenarios with status `contract` are reported as skipped ("pending"), never as passed; `bound` scenarios (currently AC-22) run their registered steps against the test database. See [tests/README.md](tests/README.md#validation-and-handoff) for how a feature binds steps.

**Synthetic fleet.** `manage.py seed_synthetic_fleet --actor-user-id <administrator id> [--dry-run]` loads the invented, non-authoritative demo fleet and checklist from `tests/fixtures/demo-fleet.json` (SPEC CHK-7). It is idempotent and for development or demonstration databases only; see [Fleet and versioned checklists](docs/checklists-implementation.md).

**Dependencies.** `requirements/base.txt` (runtime) and `requirements/dev.txt` (adds tools) pin exact versions with SHA-256 hashes, including transitive dependencies; `pyproject.toml` holds the Python version floor and ruff configuration. To add or update a package, pin the exact version with the hashes of all its release files, taken from PyPI's JSON API (`https://pypi.org/pypi/<name>/<version>/json`, `urls[].digests.sha256`), and do the same for any new transitive dependency. Then confirm a fresh `pip install --require-hashes` succeeds and justify the addition in the PR.

**Not configured yet:** a type checker and an SQL linter. Report them as unavailable rather than passed.

Project-local dependencies may be installed into the gitignored `local/` directory under the existing [coordination procedures](docs/coordination.md); host/system package installation requires owner approval.
