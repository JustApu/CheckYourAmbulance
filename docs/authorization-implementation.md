# Authorization implementation ([issue #22](https://github.com/JustApu/CheckYourAmbulance-archive/issues/22))

Product authority is [SPEC §3](../SPEC.md#3-roles-and-authorization);
schema and transaction rules are in the [R1 contracts](data-model-contracts.md).

## Policy and request guards

`cya/authorization/policy.py` expresses 34 approved permission rows and the
[issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) supplemental administrator-only invalidation action as immutable
policy data. Role codes are `CREW` (canonical base role, with `EMT` retained
as a backward-compatibility alias for historical audit evidence and grants),
`SUPERVISOR`, `SUPPLY_OFFICER` and
`ADMINISTRATOR`, matching storage, audit and navigation; display labels in the
approved matrix are translated only in the fixture-comparison tests.
Authorization uses the union of current roles and denies unknown actions,
unknown roles, inactive actors, missing conditional facts and unspecified
export scope. Administrators cannot invalidate their own checks. An allow
decision does not bypass business validation, including COR-4.

`requests.authorized_request` reads the organization without an exclusive lock,
reloads `request.cya_user` under its user-row lock, and calls
`guard.require_permission`. Different users can enter ordinary guarded bodies
concurrently; the same user remains serialized against identity/role changes.
A server-owned context loader
supplies ownership, assignment and supply scope; posted role/scope values are
never used. Locks cover the brief synchronous body. Denial rolls it back before
an independent durable denial audit. Missing/failed role or audit storage fails
closed. This must be the outermost application transaction; `ATOMIC_REQUESTS`
and surrounding transactions are unsupported.

`requests.permission_required` wraps synchronous, non-streaming views, renders
deferred responses within the transaction, and maps audited denial to 403 and
storage/adapter failure to sanitized 503. Loaders receive the view arguments;
the context loader also receives the freshly locked user. These helpers lock
only the actor. They must not acquire an organization lock later or perform
role/active-state changes. Those commands retain organization-first locking;
other multi-user commands acquire all affected users together in sorted order.
Domain validation, CSRF, receipt replay and success/
failure auditing remain the domain handler's responsibility.

Shell navigation uses the current uncached role lookup. Section denial is also
audited; audit failure returns 503. Placeholder sections do not expose role
mutation or bootstrap HTTP routes. Feature issues supply their own protected
handlers and server-owned resource loaders; the one role-administration handler
is described under [Role administration over HTTP](#role-administration-over-http-issue-25).

## Receipt-linked role history and commands

`role_grant` is an append-only A table. Its foreign keys retain the organization,
target user, actual command receipt, optional human actor and exact prior grant.
Rows have per-user sequences, receipt timestamps and actor-role snapshots.
Database guards reject UPDATE/DELETE, cross-tenant references, receipt actor/time
mismatch, gaps, duplicate active grants and invalid/repeated revocations. Grants
and revocations remain separate rows. Canonical role codes and UUIDs are checked.
The schema verifier and additive runtime-grant command discover this table;
runtime needs SELECT/INSERT only. Schema-owner privileges are required to run
its migrations. The [audit operator prerequisites](audit-implementation-notes.md#operator-procedure)
for binary-log-enabled trigger creation and durable DEFINER accounts also apply.

`DatabaseRoleBackend` implements the four methods in `backend.RoleBackend`:
current streamed role evidence, all-time administrator history, atomic
provisioning/bootstrap receipt+role+audit appends, and post-rollback denial audit.
There is no in-memory production fallback. `lifecycle.current_roles` folds the
ordered stream and refuses malformed, forked or wrong-scope history.

Trusted server handlers call `commands.change_role` or `commands.set_user_active`
with canonical organization/actor/target UUIDs, a client UUID4 `operation_id`,
and server correlation/request-attempt UUIDs. Role changes additionally take a
contract role code and `GRANT` or `REVOKE`; active changes take a strict boolean.
Both call the shared audited `run_command` boundary: lock organization first,
then actor/target users in ID order, reload current roles, require
`manage_users_roles`, and read the organization's current active administrators.
Revoking or disabling the last active administrator is refused. The organization
lock serializes all role/active mutations, including bootstrap and provisioning.

Receipts precede effects; role/user changes and successful audit commit together.
Already-satisfied role state appends no redundant role row, but the command still
gets a receipt and audit acknowledgement. Replay checks current administrator
access, returns the original acknowledgement without new effects, and isolates
actors. Changed intent under an existing operation ID cannot execute new effects.
Failure rolls back before separate failure audit. The shared boundary retries
only known rolled-back deadlocks and resolves ambiguous commits without blind
re-execution. Exceptions require an HTTP handler's sanitized mapping;
the one handler that maps them is described under
[Role administration over HTTP](#role-administration-over-http-issue-25). These
programmatic commands have no dry-run; the operator bootstrap below does.

## Role administration over HTTP ([issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25))

`/administration/roles/` (`cya/authorization/views.py`) is the walking skeleton's
end-to-end action: sign-in, server-side authorization, persistence, audit and
history through the web interface. It is not a general user-management screen.

- **Reaching it:** an Administrator opens Administration in the navigation, whose
  page links to `/administration/roles/`; the Administration menu entry stays
  highlighted there. Other roles are never shown the link.
- **GET** shows the organization's latest 50 role-history rows and a change form
  to an Administrator. A signed-out visitor is redirected to sign-in; anyone else
  gets 403 and an audited `VIEW_ADMINISTRATION` denial (503 if that audit fails).
- **POST** takes `target` (user UUID), `role`, `action` (`GRANT` or `REVOKE`) and
  `operation_id` (a canonical UUID4, generated per rendered form so a double
  submit replays). Django's CSRF check applies first. The actor and organization
  always come from the session user, never the form; `EMT` is not accepted.
- The handler calls `commands.change_role`, which authorizes under the locks,
  writes receipt, role row and audit together, or records a failure audit after
  rollback. Responses: success is a 302 back to the history page; 400 malformed
  input (nothing written or audited); 403 not permitted (a `DENIED` audit row is
  committed); 404 target missing or in another organization (a non-administrator gets 403
  instead, so user ids cannot be probed; error pages show them no history); 409 last
  administrator or an operation id reused for a different request (the earlier
  change stands, the new one is not applied and is never reported as saved); 503 audit,
  database or unknown-outcome failure, with a message that does not say whether
  the change saved. No exception text reaches the page.
- Replaying the same `operation_id` returns the original acknowledgement and adds
  no role, receipt or audit row.

`cya/tests/test_role_administration_http.py` and the bound scenario
`EX-role-change-end-to-end` (`cya/authorization/scenario_steps.py`, run by the
scenario-library runner with real commits) prove these paths on MariaDB with
only the Google identity provider stubbed; role grants, receipts and audit are
real. That stub test proves application logic. The separate real-Workspace
verification in [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) is performed outside CI by the owner and is not
covered by it.

## Sign-in and first-administrator operator procedure

Settings install the concrete collaborators:

- `CYA_INITIAL_ROLE_GRANTER=cya.authorization.roles.grant_initial_emt`
- `CYA_AUTHORIZATION_BACKEND=cya.authorization.persistence.DatabaseRoleBackend`
- `CYA_ROLE_LOOKUP=cya.authorization.persistence.current_role_codes`
- `CYA_AUTH_AUDIT_SINK=cya.core.auth_audit.record_auth_event`

Sign-in takes the organization lock before user lookup/creation. Initial
provisioning plans only Crew and writes the real receipt, role row and audit
inside that same transaction. Authentication/profile audits use current roles.
Any writer failure rolls back provisioning. Real Google credentials and the
owner's [manual sign-in checks](google-workspace-signin.md) remain deployment
work; offline tests use synthetic identities, never live Google.

The deployer seeds the first Administrator for an existing active crew member:

```sh
local/venv/bin/python manage.py bootstrap_administrator --organization <organization-uuid> --user <user-uuid> --dry-run
local/venv/bin/python manage.py bootstrap_administrator --organization <organization-uuid> --user <user-uuid>
```

Use canonical UUIDs and an authorized operator environment. Both flags are
required. The command locks the organization/target, validates current history,
and refuses inactive identities, a non-crew target, or **any** prior administrator
grant (including revoked grants/inactive users). It commits receipt+role+audit
before reporting success. A repeat refuses rather than seeding a second admin.
`--dry-run` validates under the same locks and writes nothing, including no audit.
Normal failures with a known organization are audited after rollback; malformed
IDs/missing organizations have no fabricated tenant. Errors are sanitized and
never confirm an ambiguous successful grant or automatically retry it.

Provisioning and bootstrap receipts use named services `identity.provisioning`
and `operator.bootstrap`, not the target user's identity as an invented human
actor. Their deterministic UUID5 operation keys use compact JSON job keys of
`[target_user_uuid, correlation_uuid]`. Role evidence and audit identify those
services through the receipt. Keep operator access controlled; bootstrap is not
a password fallback, recovery bypass, or web privilege switch.

## Verification

Run the configured MariaDB tests and separate offline policy tests:

```sh
local/venv/bin/python manage.py test cya --noinput
local/venv/bin/python -m unittest discover -s tests/authorization -v
```

Use only a disposable test server as described in
[CONTRIBUTING](../CONTRIBUTING.md#python-and-django).
`CYA_DB_SECURITY_TESTS=1` requires disposable DBA privileges and exercises real
restricted runtime principals. Tests cover all 128 matrix cells / 153 supplied
variants, conditional cells and forged input, current persisted revocation,
bootstrap dry-run/refusal, atomic provisioning and role/audit rollback, replay
and actor isolation, last-admin refusal and a real two-connection race combining
revocation and deactivation. Schema tests cover trigger and tenant/receipt guards,
restricted runtime appends and refusal of UPDATE/DELETE/REPLACE/upsert/DDL,
and migration rollback/reapply. Separate two-connection regressions prove
different users can enter guarded bodies concurrently, while same-user
requests retain their identity locks until the guarded body exits. Recording
collaborators remain confined to
interface tests; the persistent tests use real MariaDB rows and transactions.

No runtime data, production server or real external identity is used. Tests do
not establish replicated/Galera behavior, production deployment readiness or
live-provider verification. Independent permissions/migration review is required
before merge readiness; owner self-checks and CI do not replace it.
