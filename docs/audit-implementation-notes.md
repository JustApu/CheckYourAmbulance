# Audit foundation ([issue #23](https://github.com/JustApu/CheckYourAmbulance-archive/issues/23))

This is implementation and operator guidance, not a SPEC amendment. Authority:
[SPEC §16.1](../SPEC.md#161-audit-log-c-req-23-9), §17.7–§19 and the
[R1 data-model contracts](data-model-contracts.md). Retention remains indefinite;
there is no pruning command or scheduled deletion. The owner's five-year
planning direction on [issue #5](https://github.com/JustApu/CheckYourAmbulance/issues/5) does not authorize deleting immutable history.

## Scope and remaining feature obligations

The owner approved **Option A** on [PR #39](https://github.com/JustApu/CheckYourAmbulance-archive/pull/39) (2026-10-21): implement the audit
foundation and protect the two existing A tables, `command_receipt` and
`audit_event`; retain the complete 38-table A catalogue, but create no placeholder
schemas for future features. `tools/audit_enforcement.py` classifies A/V/M tables;
`cya_core.0004_audit_integrity` freezes the current catalogue in migration history.
Tests compare it with the contracts and inspect the physical database.

Unknown physical tables, unsupported views, missing infrastructure tables,
missing UPDATE/DELETE guards and no-op lookalike triggers fail schema inspection.
Absent future tables are expected. A feature migration introducing an A table
must install rejection triggers and narrow runtime grants and extend the real
positive/negative tests. Its handler must atomically append business evidence,
receipt, audit and any notification intent. Future M/V features must supply
creation-column/publication guards and reviewed write grants; this foundation
does not prove enforcement for tables that do not exist. The current runtime
checker conservatively allows SELECT/INSERT on A/V and additionally UPDATE on
M, never broad schema grants or header DELETE. Future draft/projection
permissions need an explicit extension with their guard tests.

AC-25's scenario stays `contract`: it names future check/event tables. Existing
receipt/audit enforcement is proved by executable tests, rather than reporting
that full-catalogue scenario as passed. Role evidence, last-administrator
protection and concrete role commands are implemented by
[issue #22 (archive)](https://github.com/JustApu/CheckYourAmbulance-archive/issues/22),
documented in the [authorization implementation notes](authorization-implementation.md).
Domain routes, outbox
schemas and production deployment are separate work.

## Schema and append APIs

`cya_core.CommandReceipt` and `AuditEvent` use the merged `cya_accounts`
identities, CHAR(36) ASCII binary UUID columns and UTC DATETIME(6) instants.
Migrations run as the schema owner:

- `0001_initial`: receipt/audit tables and unique organization/operation keys.
- `0002_audit_append_only`: four BEFORE UPDATE/DELETE SIGNAL rejection triggers.
- `0003_auditevent_job_key_auditevent_service_identity`: named worker audit
  attribution, including failures without a committed receipt.
- `0004_audit_integrity`: organization-aware actor/receipt FKs with RESTRICT;
  UUID format/variant, human-v4/worker-v5, exclusive attribution, nonblank
  service identity/job key, digest/result/outcome/role/target/snapshot checks;
  an INSERT trigger rejects audit/receipt actor, service and correlation mismatch.

Migrations freeze their DDL, support reversal, and do not repair/delete existing
history. A pre-existing invalid row causes migration failure and requires
investigation; never bypass checks or rewrite history to force migration.
Reversal tests use only an empty disposable schema. Reversing field migrations
would discard those columns; this is not a production recovery procedure.

`cya.core.audit_repository.append_receipt` INSERTs a complete acknowledgement
inside the caller's explicit transaction, without upsert or separate reservation
commit. Human operations require canonical RFC-layout UUIDv4. Worker operations
must equal the contracts' deterministic UUIDv5 computed by
`tools.command_identity.worker_operation_id`, with a compact ASCII JSON job key.
The trusted worker handler must authorize its allowlisted service/action; these
low-level writers do not authenticate or grant worker privileges.

`append_audit` accepts typed actor, role, action, target, outcome, reason,
correlation and prior/new snapshots. Outcomes are SUCCEEDED, DENIED, FAILED or
OUTCOME_UNKNOWN. Codes are bounded uppercase identifiers; anonymous/service
actors have no human roles. Snapshots use exactly
`{"schema_version": 1, "values": {...}}`. Result/snapshot JSON is bounded to
256 KiB; unsupported versions, non-objects, NaN and malformed metadata are
refused. Callers select approved semantic fields: this API is validation,
**not automatic redaction of arbitrary dictionaries**. Never pass raw request
bodies, exceptions, credentials or provider responses. Target type/UUID is
visibility-checked audit metadata, not a substitute for a domain evidence FK.
Current target types are USER, ORGANIZATION and COMMAND_RECEIPT; the writer
and INSERT guard reject missing/cross-organization targets. Future features
must explicitly register and guard their target types. Command-linked audit
uses the receipt's recorded instant; feature evidence
should use that same instant so an as-of boundary includes the complete command.

## Transaction and replay contract

`cya.core.audit_commands.run_command` requires an outermost autocommit boundary.
Feature handlers supply two trusted callbacks:

1. `authorize(organization)` locks/reloads the current actor/roles (sorted user
   IDs; organization first for role changes), enforces policy, and returns
   `Actor(user, roles)` or a named worker Actor. It may raise `CommandDenied`
   carrying known, trusted actor context. HTTP handlers enforce authentication,
   CSRF and input limits before entering; they never accept client role/service
   claims. Authorization also runs on replay.
2. `prepare(actor)` acquires existing resource anchors in the contracts' order,
   revalidates domain facts and preallocates IDs. It returns `Plan(response_body,
   apply, ...)`. `apply(receipt, actor)` performs only database effects; the
   executor inserts the complete receipt first, applies effects, appends the
   success audit and commits before returning `Result`. Plans can supply safe
   target and prior/new snapshot metadata. Extra per-effect audit is permitted
   in that transaction. Feature-specific validation and business FK constraints
   remain the handler's responsibility.

Callbacks perform no external/network effects. Future handlers persist outbox
intent with their business writes and may publish hints with `on_commit`
(prefer `robust=True`). A hint callback error after commit is alerted but does
not change a Saved acknowledgement into a false failure.

The unique `(organization_id, operation_id)` key arbitrates concurrent duplicate
receipts across resource locks. Only a confirmed duplicate of that key triggers
post-rollback receipt resolution. Other integrity errors remain failures.
Matching identities receive the original status/body/correlation. A changed
human request gets that original acknowledgement with `operation_reused=True`;
new decisions require new UUIDs. Another actor or service key gets a generic
`OperationConflict` and never the competing result. Worker conflicts emit a
fixed operational alert independent of any receipt/outbox.

Only fully rolled-back InnoDB deadlocks (1213) are retried, at most three
attempts with bounded jitter. Exhaustion raises `RetryLater`. Connection loss
is never blindly retried: discard the session, reauthorize and resolve the
same receipt key. A resolved receipt returns the original acknowledgement;
otherwise raise `OutcomeUnknown` and persist OUTCOME_UNKNOWN, without falsely
claiming rollback. Caller HTTP mappings must remain sanitized (e.g. 503
RETRY_LATER, OUTCOME_UNKNOWN or AUDIT_UNAVAILABLE); exceptions are not response
bodies. No domain HTTP endpoint is supplied by this infrastructure.

DENIED/FAILED audit runs in a separate transaction after rollback, using safe
codes rather than raw exceptions; failure audit has no receipt. Named worker
identity survives rollback without a fabricated human. If audit persistence
also fails, a fixed operational alert is emitted and `AuditPersistenceFailure`
requires an unavailable response. No successful audit claim is made.

The older `execute_audited_command` is a lower-level callback boundary without
receipt arbitration/retry. Its failure callback now receives safe `Failure`
metadata (outcome/reason code), not the original exception. Prefer `run_command`
for feature commands.

## Sign-in adapter

`cya.core.auth_audit.record_auth_event` implements the existing accounts
`AuthAuditEvent` sink. Provisioning/profile/sign-in evidence joins the caller's
transaction; standalone sign-out and post-rollback refusal/failure evidence
commit in an audit-only transaction. It reloads the user and current roles,
keeps anonymous attempts anonymous, maps REFUSED to DENIED, and permits only
email/display-name profile snapshots. Missing/ambiguous R1 organization,
missing role reader or malformed evidence fails closed. No raw Google fields
or tokens are persisted.

[issue #22](https://github.com/JustApu/CheckYourAmbulance-archive/issues/22) installs `CYA_AUTH_AUDIT_SINK=cya.core.auth_audit.record_auth_event`
and the current, uncached `CYA_ROLE_LOOKUP=cya.authorization.persistence.current_role_codes`.
Its initial Crew and bootstrap role writers link their own receipts/evidence
through the published append API. The adapter itself does not invent sign-in
receipts or grant roles. Offline integration tests exercise durable role and
sign-in auditing; no live Google or production deployment is claimed.

## Operator procedure

Use only synthetic development/test databases until deployment is approved.
Never access the application's `data/` directory during this procedure.
Production credentials come from the operator's secret configuration, never
source, PR comments or shell arguments. No account passwords are accepted by
the management commands below.

1. With separate schema-owner credentials, explicitly set
   `CYA_DATABASE_ROLE=schema_owner`. **Before migrating a binary-log-enabled
   MariaDB 10.11 server**, the DBA sets `log_bin_trust_function_creators=1` in
   the production server configuration (SPEC §17.8 DEP-5; owner decision on
   [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16),
   2026-10-21). Schema-level privileges alone are insufficient: without it,
   CREATE TRIGGER fails with error **1419** (SUPER privilege required while
   binary logging is enabled), and of the narrower privileges tried on the
   pinned image (`BINLOG ADMIN`, `SET USER`, `BINLOG REPLAY`) none lifted it.
   The schema owner is not given SUPER. This affects the accounts trigger
   migration and audit migrations 0002/0004. The setting is server-wide and is
   not applied by any migration: a rebuilt or restored server must carry it, and
   the first restore rehearsal confirms that. Keep binary logging for ARC-7
   recovery; never grant SUPER or trigger-creation powers to the runtime
   account. See MariaDB's [binary-log privilege rules](https://mariadb.com/docs/server/server-usage/stored-routines/binary-logging-of-stored-routines).

   Triggers execute in their recorded DEFINER security context. These
   migrations use the creating account, so preserve that account and the
   privileges its triggers need. Rotate its password without dropping or
   renaming the account; a missing definer can cause error **1449**, including
   refusal of audit INSERTs. A dedicated durable non-login definer is an
   alternative for a separately reviewed DBA deployment procedure, not an
   implicit change to these migrations. Verify definers during restore and
   account rotation. See MariaDB's [DEFINER documentation](https://mariadb.com/docs/server/server-usage/triggers-events/triggers/create-trigger).

   Once the prerequisite is satisfied, run:

   ```sh
   local/venv/bin/python manage.py migrate --noinput
   local/venv/bin/python manage.py verify_audit_schema
   ```

2. A DBA creates a **distinct**, password-authenticated runtime account through
   its secure administrative channel. Do not reuse the schema-owner account,
   attach roles, grant PROXY, or give global/schema-wide data/DDL powers. The
   commands refuse a missing account rather than implicitly creating one.
   Preview and apply grants (replace the placeholders):

   ```sh
   local/venv/bin/python manage.py audit_runtime_grants --user <runtime-user> --host <account-host> --dry-run
   local/venv/bin/python manage.py audit_runtime_grants --user <runtime-user> --host <account-host>
   ```

   The preview reads schema/grants and prints SQL; it changes neither data nor
   privileges. Apply adds SELECT/INSERT to each existing A table, SELECT on
   organization and SELECT/INSERT/UPDATE on user (whose identity trigger rejects
   retargeting). It rejects pre-existing unsafe grants/roles and never revokes
   privileges or writes application rows. GRANT auto-commits: partial failure is
   reported, and the same command is safe to inspect and rerun. DBA actions need
   their separate operational record; no atomic application-audit claim is made.

3. Configure the application with the runtime credentials and
   `CYA_DATABASE_ROLE=runtime` (the default). Every new Django database connection
   verifies UTC/strict/check/FK/READ COMMITTED settings, CURRENT_ROLE and actual
   SHOW GRANTS. Broad grants, delegation, assigned roles (even inactive),
   unclassified scopes and missing SELECT/INSERT on visible A tables are refused
   and the connection closes. Authentication clauses from SHOW GRANTS are never
   printed. These checks run when a connection opens, not continuously: after
   changing privileges, recycle existing application connections and verify
   fresh ones. A schema-owner credential in runtime mode is refused too. Run
   `manage.py check --fail-level WARNING` using the runtime configuration.
   Explicitly verify the runtime connection with:

   ```sh
   local/venv/bin/python manage.py verify_audit_runtime
   ```

   Do not start the application in schema_owner mode.

Runtime cannot inspect trigger definitions without TRIGGER privilege; do not
weaken its grants to allow inspection. The separate schema-owner check and CI
tests verify exact installed rejection guards. No runtime privilege checker
can defend against a malicious DBA replacing schema objects. Application
handlers still enforce policy and never disable session checks; connection
verification is not authentication of an arbitrary actor supplied by a
compromised application.

## Verification

Pinned dependencies are installed with `--require-hashes`; CI uses Python 3.13,
Django 6.1.2, Ruff 0.17.1 and MariaDB 10.11. The regular CI job runs the full
Django suite plus offline audit helpers and schema/manifest tests.

For the account/role tests use a disposable DBA-controlled server only:

```sh
export CYA_DATABASE_ROLE=schema_owner CYA_DB_SECURITY_TESTS=1
local/venv/bin/python manage.py test cya --noinput
DJANGO_SETTINGS_MODULE=cya.settings local/venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

CI asserts that the security-test flag is `1` before running the suite, so a
missing flag fails the job instead of silently skipping restricted-account
coverage. Normal development tests skip them unless opted
in; a development schema owner need not have CREATE USER/GRANT OPTION. Tests
create random synthetic account/role names and clean them, use an isolated
MariaDB test schema and never read production config/data. Transaction tests
TRUNCATE only that disposable schema as owner because ordinary DELETE correctly
fails for retained attribution/history.

Coverage includes actual restricted-account INSERT and ORM/raw UPDATE/DELETE,
REPLACE/upsert/DDL/grant negatives; global/schema/table/role grant rejection;
malformed inserts and tenant/attribution mismatch; migration reversal/reapply;
future table/no-op guard failures and an isolated immutable-header comparison
probe (case-only and NULL-to-value rewrites); atomic business/receipt/audit commit;
rollback plus independently committed failure audit; audit-unavailable errors;
current-access replay, original-result preservation and actor isolation;
real concurrent worker duplicate arbitration and actual InnoDB deadlock retry;
lost-COMMIT-response injection and post-commit hint failure; and sign-in adapter
snapshots/rollback/refusal behavior. Lost-response and audit-outage faults are
injected over real transactions; they are not claims of a live network-chaos or
production recovery exercise. No future catalogue enforcement or live Google
verification is claimed by these tests.
