# Deployment runbook and server hardening

[issue #6](https://github.com/JustApu/CheckYourAmbulance/issues/6). **Draft; Docker diagnostic checks recorded below, full host verification
pending; not approved for production use.** Implements the operator procedure for [SPEC §17.8](../SPEC.md#178-deployment-secrets-and-recovery-oq-24-issue-16),
DEP-1–DEP-5 and DEP-10. SPEC remains authoritative. This document does not
authorize procurement, production changes, restores or a pilot.

## Readiness and scope

[issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25)'s M1 implementation and real-Workspace verification are complete.
The Docker diagnostic evidence below is historical and does not verify the
current source or a host deployment. Repeat applicable checks against the
current source before treating this runbook as verified. The following
deployment gaps remain in the current application:

| Gate | Current baseline and required completion |
|---|---|
| Production application server | `cya.asgi:application` exists, but no production ASGI server is pinned in `requirements/base.txt`. Select, pin and review one before supplying a service launch command. Do not use Django `runserver` publicly. Keep one application process (DEP-2). |
| HTTPS recognition | Built ([issue #25](https://github.com/JustApu/CheckYourAmbulance/issues/25)): `CYA_TRUST_PROXY_SSL_HEADER`, `CYA_SSL_REDIRECT` and `CYA_HSTS_SECONDS` (below) recognize HTTPS behind the proxy, redirect to it and send HSTS. All are off by default; enable them only with the proxy rule in [Proxy and application service](#proxy-and-application-service). Still to verify on the rehearsal host. |
| Application rate limits | No authentication limiter is installed and export routes do not exist. Proxy limiting alone does not satisfy DEP-10's application limit. Implement through separately scoped application work; reconcile exact routes before verification. |
| Rehearsal | The Docker diagnostic subset below has run. The full host/service checklist remains pending; diagnostic overlays are not installed production configuration. |

This issue's runbook scope does not include changing application files. The
application gaps above need separately scoped implementation work before those
behaviors can pass a deployment rehearsal. Backup jobs and restore measurements
belong to [issue #7](https://github.com/JustApu/CheckYourAmbulance/issues/7), alert delivery to [issue #8](https://github.com/JustApu/CheckYourAmbulance/issues/8), paper fallback to [issue #9](https://github.com/JustApu/CheckYourAmbulance/issues/9),
and load/hot-backup/high-availability evaluation to [issue #10](https://github.com/JustApu/CheckYourAmbulance/issues/10).

## Operator preparation and cost envelope

The owner chooses the provider and privately names the accountable operator
and deputy. Their roles may be filled by volunteers, paid staff, or a mix. Both
need tested administrative recovery access; hold names, hosts, keys and account
identifiers outside GitHub. Use a supported Linux release
with security updates, MariaDB 10.11, Python compatible with the pinned Django
release, a reverse proxy and a service manager. Record exact versions privately
and sanitized version/build evidence in the rehearsal result.

DEP-4 is a **planning figure, not a ceiling**. Initial allocation for evaluating
quotes: VPS USD 15/month, off-host storage USD 7/month, monitoring USD 3/month,
total USD 25/month. These are allocation estimates, not vendor prices or a
capacity guarantee. Before procurement obtain current quotes including tax,
backup/log retention (30 daily and 12 monthly full backups plus associated
logs), transfer/restore charges, DNS and any certificate automation cost. Size
storage using measured growth and [issue #7](https://github.com/JustApu/CheckYourAmbulance/issues/7)'s rehearsal, not the allocation.
Account for donations explicitly; ask the owner to decide material differences.
No provider or paid service is selected by this draft.

## Host and network preparation

Perform the first installation only on an isolated throwaway host with
synthetic data. Host package installation and firewall/service changes are
operator actions; this draft has not performed them on the developer machine.

1. Establish restricted, key-based administrative SSH for operator and deputy.
   Verify a second login and provider recovery console before changing access.
   Use individually attributable administrative access; do not share private keys.
2. Apply supported OS security updates before deploying. Schedule subsequent
   patch review, reboot windows and a deputy check. Rehearse updates before
   production, retaining the prior release and an owner-authorized recovery path.
3. Run the application as a dedicated non-login, unprivileged service identity.
   Keep release code and the virtual environment unwritable by that identity;
   do not give it sudo, database administration or container-host control.
4. Synchronize the host clock and check UTC timestamps after restart. Application
   connections also enforce UTC and strict database modes; do not disable them.
5. Bind the app upstream to loopback or a permission-restricted Unix socket and
   MariaDB to `127.0.0.1`. Deny remote database/app ports, including IPv6. When
   containers are eventually selected, verify actual published ports and firewall
   behavior; the development compose file is not a production deployment.
6. Permit inbound HTTPS (443) and SSH only from the approved administration
   sources, on both host and provider firewalls. Verify from outside the host;
   retain console access while testing. Do not open port 80 by default: DEP-10
   currently permits only HTTPS and restricted SSH.
7. Use automated certificate renewal compatible with that exposure rule, e.g.
   DNS-01 using narrowly scoped DNS credentials, protected like other secrets.
   HTTP-01 needs port 80 and would require an approved change to this rule;
   do not temporarily open it unnoticed. Rehearse issuance and renewal with
   the selected client's staging service, then check its timer and failure alert.

Use the chosen distribution's versioned installation and firewall documentation
when making the concrete host profile. Package names, firewall commands and
service unit names are deliberately not guessed here.

## Secrets and service configuration

Use a root-owned directory outside the checkout and web root, for example
`/etc/check-your-ambulance/` (0700), with separate mode-0600 runtime and migration
environment files owned by root. Do not put credentials in command arguments,
shell history, source, screenshots, logs or GitHub. Populate through the
operator's secure credential channel. Use different secrets on the rehearsal
host and production. Do not read or copy the repository's runtime `data/`.

The service manager may load the runtime file for a service running as the
unprivileged application identity; for systemd this is `EnvironmentFile=`.
The application cannot read a root-only file directly. Environment values are
still available to the child process: restrict process inspection and service
administration, disable diagnostic environment dumps and protect crash reports.
Use the service manager's documented file syntax; do not shell-source the file
or share it with the reverse proxy. The application does not load dotenv files.

systemd recommends its credential facilities instead of environment variables
for secrets. The current app consumes environment variables only; a reviewed
credential-loading integration is preferable before production. Never place
secret values in a world-readable unit's `Environment=` directive. This draft
does not claim a root-only source file prevents exposure by the running process.

| Setting read by current application | Required deployment value |
|---|---|
| `CYA_SECRET_KEY` | Unique, long random production key, generated privately |
| `CYA_DEBUG` | `0` |
| `CYA_ALLOWED_HOSTS` | Exact approved hostname(s), comma-separated; no wildcard |
| `CYA_DB_HOST`, `CYA_DB_PORT`, `CYA_DB_NAME` | `127.0.0.1`, selected local port (normally `3306`), selected database |
| `CYA_DB_USER`, `CYA_DB_PASSWORD` | Distinct runtime account in runtime file; owner credentials only in protected maintenance context |
| `CYA_DATABASE_ROLE` | `runtime` for the app; `schema_owner` only for maintenance commands |
| `CYA_GOOGLE_CLIENT_ID`, `CYA_GOOGLE_CLIENT_SECRET`, `CYA_GOOGLE_HOSTED_DOMAIN`, `CYA_GOOGLE_REDIRECT_URI` | Organization-owned OAuth configuration; exact HTTPS callback `/auth/callback/` |
| `CYA_SESSION_MAX_AGE` | Explicitly chosen revocation bound in seconds; current range 300–86400, default 28800 |
| `CYA_PUBLIC_BASE_URL` | Public HTTPS base URL used for email deep links; unset sends a bare path |
| `CYA_NOTIFICATION_SENDER` | Dotted path to the sender factory; unset leaves email queued and the delivery command refuses to run |
| `CYA_NOTIFICATION_MAX_ATTEMPTS` | Attempts before `GAVE_UP`; default 5, allowed 1–20 |
| `CYA_NOTIFICATION_RETRY_SECONDS` | Base retry delay, doubled per attempt; default 60 seconds, allowed 1–86400 |
| `CYA_LIVE_MAX_CONNECTIONS_PER_USER` | WebSocket connection limit per user per process; default 4, allowed 1–32 |
| `CYA_LIVE_AUTH_RECHECK_SECONDS` | WebSocket authorization recheck interval; default 60 seconds, allowed 10–300 |
| `CYA_TRUST_PROXY_SSL_HEADER` | `1`, only with the proxy rule below (the proxy overwrites `X-Forwarded-Proto` and the app is reachable only through it); default `0` |
| `CYA_SSL_REDIRECT` | `1`; needs `CYA_TRUST_PROXY_SSL_HEADER=1` (startup refuses it otherwise); default `0` |
| `CYA_HSTS_SECONDS` | Start at `300`, raise only after HTTPS sign-in, callback and form posts are checked; allowed 0–63072000, default 0 (off); needs `CYA_TRUST_PROXY_SSL_HEADER=1`. Subdomains and preload stay off unless the owner approves them |

Register the callback and verify managed-account behavior using the
[sign-in procedure](google-workspace-signin.md#configuration) and [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25).
Do not substitute offline identity stubs for real-Workspace verification.

Rotate secrets yearly, on operator change and on suspected exposure (DEP-10).
Privately inventory OAuth, database, session-signing, DNS, backup and selected
notification-transport credentials. `CYA_NOTIFICATION_SENDER` selects a factory;
it is not itself a credential. The repository defines no provider-specific email
credential variables, so document the chosen sender's exact settings and put its
credentials in the protected runtime configuration. Stage new values through
their owning service, update the protected files,
restart affected services to clear old connections, verify sign-in and audited
writes, then revoke superseded credentials. Never log values to prove rotation.
Changing the Django signing key without retaining old keys invalidates sessions;
plan reauthentication. Rotate the schema-owner **password**, never drop or
rename the trigger DEFINER account. On suspected compromise, contain access
first and involve the owner; do not retain a compromised signing key for comfort.

## Database setup and migrations

Use the [audit operator procedure](audit-implementation-notes.md#operator-procedure)
for the exact grants and guard contract. A DBA creates the database and separate
password-authenticated owner and runtime accounts using a secure local channel.
Do not use development passwords or CI's root account. The schema owner needs
schema migration privileges and the authorized grant-installation capability;
the runtime account must have no broad grants, roles, delegation, SUPER or DDL.

Persist `log_bin_trust_function_creators=1` and binary logging in MariaDB's
server configuration; do not set them in a migration or disable logging to get
past error 1419. Persist `bind-address=127.0.0.1`. Determine the actual loaded
configuration path from the selected MariaDB installation and verify after a
restart/rebuild. With a DBA connection, these queries read state only:

```sql
SELECT VERSION(), @@GLOBAL.log_bin,
       @@GLOBAL.log_bin_trust_function_creators, @@GLOBAL.binlog_format,
       @@GLOBAL.bind_address;
SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, DEFINER
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = DATABASE();
```

Select the application database before the trigger query. Check every definer
against the retained owner account, privately; publish no account identifiers.
MariaDB's variable reference says the trust setting removes the extra creator
restriction for functions/triggers; its routines page describes the restriction
under statement logging. SPEC DEP-5 records a pinned-10.11 experiment. The
documentation is consistent with enabling the setting, but is not a substitute
for reproducing the non-SUPER migration on the chosen 10.11 build and logging
format. Record any discrepancy on [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16); do not expand runtime privileges.

Use an immutable reviewed release, install hash-checked dependencies into its
`local/venv`, and run from that release directory. The following commands assume
the selected protected maintenance environment is already loaded by the service
manager/operator runner, not by placing secrets on the command line:

```sh
local/venv/bin/python manage.py migrate --plan
local/venv/bin/python manage.py migrate --noinput
local/venv/bin/python manage.py verify_audit_schema
```

Stop application traffic before schema changes; confirm the backup/recovery
procedure from [issue #7](https://github.com/JustApu/CheckYourAmbulance/issues/7) before touching a populated production schema.
`--plan` reads the plan, not a migration rehearsal. Migrations write schema/data;
MariaDB DDL can leave partial changes on failure. Stop on a nonzero exit and
inspect the migration record and schema; do not fake completion, blindly reverse
or repeatedly restart. Rehearse the recovery on a copy first.

After the DBA creates the runtime account, preview grants, inspect the target
and apply using the actual account name and account host. These synthetic
examples assume the DBA created exactly this account; the host is the MariaDB
account host, not necessarily the application's `CYA_DB_HOST` value:

```sh
local/venv/bin/python manage.py audit_runtime_grants --user cya_app --host localhost --dry-run
local/venv/bin/python manage.py audit_runtime_grants --user cya_app --host localhost
```

The preview reads schema/grants and writes nothing. Apply adds narrow grants
to an existing account, does not revoke unsafe grants, and auto-commits each
GRANT. A partial failure requires inspection; reapplying safe missing grants is
idempotent. Keep its private administrative record separate from app audit.

For first setup only, seed the organization using `create_organization` with
`--name` and `--time-zone`, previewing with `--dry-run` first. It creates only
when none exists; it does not rename an existing organization. Follow the
[first-administrator procedure](authorization-implementation.md#sign-in-and-first-administrator-operator-procedure)
after an authorized managed user signs in. Bootstrap has a preview, and refuses
a repeat after any prior administrator grant; it is not a recovery bypass.

Create the agency's schedule rules and publish its checklist before binding
vehicles. If the illustrative initial schedule seed is appropriate, run
`seed_default_schedules` while `CYA_DATABASE_ROLE=schema_owner`, before switching
to the runtime file. It uses the organization's configured station time zone; its
demonstration times are not universal defaults. Otherwise publish the agency's
own rules. Follow [the schedule setup procedure](schedule-engine.md#illustrative-schedule-seed)
and [the obligation setup procedure](obligation-generation.md#operator-commands).
Run the illustrative schedule seed under `CYA_DATABASE_ROLE=schema_owner`
before switching to the runtime configuration. Then switch to the runtime
file and fresh runtime connections. With an authorized administrator, use
`bind_requirement` to bind each deployed vehicle to its checklist template
and schedule slot. Run `generate_obligations` once per active organization
under the runtime role; inspect failures and confirm the run reports all units
already current on a repeat. Never use the schema-owner role for runtime
bindings, scheduled jobs or the application.

Run these read-only verification commands with fresh runtime connections
before starting the app:

```sh
local/venv/bin/python manage.py verify_audit_runtime
local/venv/bin/python manage.py check --fail-level WARNING
local/venv/bin/python manage.py check --deploy --fail-level WARNING
```

Schema inspection uses owner credentials; never grant TRIGGER to runtime to
make it inspect guards. Resolve failed checks before enabling traffic.
`check --deploy` can currently fail on missing deployment settings; do not
silence warnings to claim readiness. After any grant change restart app
connections and repeat runtime verification. Never run the app as schema owner.

## Scheduled application jobs

Run recurring management commands with the protected runtime environment,
`CYA_DATABASE_ROLE=runtime`, and the unprivileged application service identity.
Do not use the schema-owner account for recurring work. Configure the host
service manager or cron only after testing the command manually on synthetic
data; keep output and logs private. Verify that each schedule remains enabled
and runs after a host reboot. [issue #8](https://github.com/JustApu/CheckYourAmbulance/issues/8) owns alert delivery; until it is in place,
operators must inspect each job's result and arrange an approved alert path.

- **Obligations:** invoke `manage.py generate_obligations --organization
  <org-uuid>` for each active organization at least hourly. It is restart-safe,
  idempotent, and locks/replans each vehicle and due instant; failures produce a
  nonzero exit after reporting the failed units. If a run is missed longer than
  the configured lookback, increase `--lookback-hours` for the recovery run.
- **Notifications:** when a sender is configured, run
  `manage.py deliver_notifications --limit 50`; each pass covers every active
  organization. Choose the delivery cadence to meet the approved operational
  expectation, which is not specified by the current product rules. Prevent
  overlapping delivery runs: SMTP can accept a message before its attempt row is
  written, so an overlapping/repeated attempt can send a duplicate. Check the
  printed `errors` and `collisions` counts even if the process exits successfully.
  If `CYA_NOTIFICATION_SENDER` is unset, the command refuses to run and email
  intents remain queued. No current business event enqueues notifications; see
  [notification delivery](notifications.md) before enabling this timer.
- **Session cleanup:** run `manage.py close_inactive_sessions --limit 200` on an
  operator-selected cadence. It is idempotent and closes at most 200 oldest
  eligible sessions per pass; sessions are treated as closed after six idle
  hours even if the job is late. The command exits nonzero if any close fails.

The scheduler must not make application startup depend on job success. Alert on
missed or failed runs through the approved operational path; do not silently
ignore nonzero exits or positive notification `errors`/`collisions`. The exact
frequency for notification delivery and session cleanup is a deployment choice,
not a new product default. See [obligation generation](obligation-generation.md),
[notification delivery](notifications.md) and [session cleanup](inspection-sessions.md).

## Proxy and application service

Keep the public listener closed until the readiness gaps above are resolved.
The selected service launch must use the pinned server with `cya.asgi:application`,
one application process, no reload watcher, bounded resources/restarts, the
runtime-only environment and an unprivileged identity. Recheck process count
after restart. No service unit or server dependency is installed by this draft.

At the proxy, terminate TLS, reject unknown hosts and forward only to the local
upstream. Overwrite client-supplied forwarded protocol/host headers: set
`X-Forwarded-Proto` to the real scheme on every request (with nginx,
`proxy_set_header X-Forwarded-Proto $scheme;`) and forward the client's `Host`
(`proxy_set_header Host $host;`), which Django compares with the HTTPS `Origin` of
form and API posts. Only then set `CYA_TRUST_PROXY_SSL_HEADER=1`, and only while
the app is unreachable except through the proxy (DEP-10): otherwise a client could
claim HTTPS. Then set `CYA_SSL_REDIRECT=1` and `CYA_HSTS_SECONDS=300`; startup
refuses either without the trust setting, because behind the proxy every request
would look like plain HTTP. Check HTTPS POST/CSRF and the sign-in callback before
increasing the HSTS duration. `manage.py check --deploy` then reports no
`security.W004`/`W008`. Do not expose generic private files,
configuration directories or database dumps through a catch-all file server.

Rate-limit sign-in at the proxy and in the application. With nginx,
`limit_req_zone` defines the key/rate in the HTTP context and `limit_req` applies
it to locations; use a bounded burst and intentional rejection status. Configure
all `/auth/` entry points, especially login and callback, without blocking
legitimate shared-IP crew sign-ins. Tune and record rates on the rehearsal host.
Do not trust arbitrary client IP headers for limiting. Protect exports at both
layers before adding their routes; do not invent a current `/export/` endpoint.

The shell currently includes its stylesheet/script inline; there is no current
static collection step to assume. The application also exposes the authenticated
WebSocket route `/ws/v1/vehicles/<uuid>/live/` through `cya.asgi:application`.
Use a pinned ASGI server that supports both HTTP and WebSocket in the same
process: the current in-memory Channels layer is process-local, and splitting
HTTP publishing from the socket consumer loses updates. Configure the proxy to
forward WebSocket upgrade/connection headers for this route and set bounded
handshake, read and idle timeouts using the selected proxy's documentation. The
Origin allow-list comes from `CYA_ALLOWED_HOSTS`; the consumer also checks the
signed session, current role, organization and vehicle access, then rechecks
authorization at the configured interval. These messages are identifier-only
hints, not inspection state; clients must reread authoritative state after
reconnect. See [live updates](live-updates.md) for the event and recovery
contract. Do not deploy multiple application processes without an approved
shared channel layer and connection accounting.

Keep access/error logs private and bounded. Exclude OAuth codes/query strings,
authorization headers, cookies, tokens and request bodies from logs and error
reports; verify at both proxy and app server, including errors. Do not publish
raw logs. [issue #8](https://github.com/JustApu/CheckYourAmbulance/issues/8) must test alerts for unreachable site, certificate expiry
within 14 days, disk space, stale backup/log shipping, refused DB connections
and repeated 5xx errors, delivered to both operator and deputy.

## Upgrade, failure and recovery

Rehearse OS/database/app updates on the synthetic host first, review dependency
changes, and pin the reviewed release. During maintenance keep the prior code
and protected configuration available to the operator. Check backup freshness,
announce the outage/paper procedure, stop writes, apply the reviewed migration
plan, verify guards/grants and repeat the authorized/denied/replay checks before
reopening. Restart after OS patches when required and repeat network checks.

If app startup or verification fails, keep traffic closed. Revert code only when
it is compatible with the current schema; do not assume a schema downgrade is
safe. A database restore can lose accepted writes and requires owner approval
(DEP-7), followed by the [issue #7](https://github.com/JustApu/CheckYourAmbulance/issues/7) recovery procedure and verification. Preserve
audit history and binary logs; do not prune or recreate tables to recover.
The RPO is 15 minutes, RTO 24 hours; provider snapshots alone do not prove either.

## Throwaway-server verification record (pending)

Run the procedure on a disposable host using the current source with synthetic
data only. Record the full source SHA, tested component versions, date, tester,
pass/fail and sanitized explanation for each row. Do not commit hostnames,
addresses, account IDs, credentials or raw output. No row below is claimed fully
passed by the earlier Docker subset; the evidence below distinguishes the
exercised parts from the host work still pending.

| Check | Required evidence | Status |
|---|---|---|
| Build and dependency installation | Hash-checked install and selected server command/unit verified against installed help; one app process after reboot | Pending |
| SSH/firewall | Operator and deputy access survives policy reload; only HTTPS and restricted SSH exposed; DB/upstream denied externally over IPv4/IPv6 | Pending |
| TLS/renewal | Trusted certificate, host rejection, staging renewal and timer/failure handling with port 80 closed | Pending |
| Proxy trust/CSRF | Spoofed forwarding headers do not change trust; HTTPS POST works, missing/invalid CSRF refused; secure cookie flags and HSTS observed | Pending |
| Secrets/rotation | Root-only files, runtime identity cannot read owner file, no secret-bearing logs; rotation/restart/reauthentication tested | Pending |
| MariaDB/DEP-5 | 10.11 binary logging on, trust=1 persistent after rebuild; owner has no SUPER; migration/guard checks pass and definers preserved | Pending |
| Runtime isolation | Fresh runtime verification and system/deploy checks pass; negative UPDATE/DELETE tests use synthetic disposable rows only | Pending |
| Schedule and bindings | Agency schedule rules and checklist bindings are present; initial obligation generation succeeds and is idempotent | Pending |
| Recurring jobs | All three jobs use runtime identity, survive reboot, report/alert failures, and notification delivery cannot overlap | Pending |
| WebSocket proxy | ASGI serves HTTP and WebSocket in one process; proxy upgrade, origin rejection, authorization recheck and reconnect recovery are verified | Pending |
| Integration | [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) authorized action persists and appears in history/audit; denial recorded; same operation ID produces one result | Pending |
| Rate limits | Authentication limits enforced by both layers and tested with bounded requests; exports remain gated until implemented | Pending |
| Restart/failure | App/DB restart, interrupted migration recovery plan and prior-code compatibility examined; failure keeps traffic closed | Pending |
| Data/log hygiene | Reviewed logs omit tokens, callback query and cookies; synthetic host artifacts/credentials destroyed at completion | Pending |

[issue #6](https://github.com/JustApu/CheckYourAmbulance/issues/6) cannot close until its server checks and independent security review
pass. DEP-12 additionally requires named operators and [issue #7](https://github.com/JustApu/CheckYourAmbulance/issues/7)'s restore
rehearsal, including chosen-timestamp recovery and realistic-size timing.
Monitoring, paper readiness and load evidence retain their own issue gates.

## Docker diagnostic evidence, 2026-10-22 UTC

Tester: Codex (OpenAI). The owner authorized as much verification as possible
in Docker after checking [PR #54](https://github.com/JustApu/CheckYourAmbulance-archive/pull/54). Tested **[PR #54](https://github.com/JustApu/CheckYourAmbulance-archive/pull/54) candidate head
[`27acdb62713c3911b0404dd874fa00f825a2a962`](https://github.com/JustApu/CheckYourAmbulance-archive/commit/27acdb62713c3911b0404dd874fa00f825a2a962)**, based on main
[`3b3c3ec51f0bff5d2472a22b0293eac5dc54c5f9`](https://github.com/JustApu/CheckYourAmbulance-archive/commit/3b3c3ec51f0bff5d2472a22b0293eac5dc54c5f9). [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25)'s real-Workspace
verification and M1 exit are now complete, but this older candidate evidence
does not verify current main or the deployment host requirements in this issue.

The isolated network had Python, MariaDB and nginx containers and **no published
host ports**. Only synthetic identities, credentials and records were used.
The source was extracted into a private temporary directory; no runtime `data/`
was read. All lab containers, anonymous database volumes, network, temporary
dependencies/source, synthetic sessions and generated certificate/key were
destroyed afterwards. Downloaded image cache layers remain reusable and contain
no lab data.

| Component | Version / immutable image |
|---|---|
| Python image | `python:3.13-bookworm@sha256:073ffebb96ae4d0ed73ccad59f08c47bd84af8a79f5258130f8458ac50ad9ff4` |
| MariaDB | 10.11.19; `mariadb:10.11@sha256:0d0945c57f75b3a3b9ebe71aedaaad005fa5305054e8fca3d2e42a8c92b43b5e` |
| nginx | 1.30.6; `nginx@sha256:a7675fcee220f59b4a99f7098c06a0d5d999c2c0ee37c0211efb780536e5cb20` |
| Django | 6.1.2 from hash-checked `requirements/dev.txt` and `base.txt` |

Results:

- **Passed:** `ruff check .`, `ruff format --check .` (140 files), Django system
  checks and `makemigrations --check --dry-run`. The full offline Django suite
  ran **295 tests, OK, 54 skipped**, with `CYA_DB_SECURITY_TESTS=1` against the
  disposable DBA server. The skip count includes pending scenario contracts;
  it is not a production-readiness count. Tool tests: 55 OK. Offline audit and
  authorization helper discovery under `tests/`: 29 OK.
- **Passed:** migrations with a separate schema-scoped owner without SUPER,
  with binary logging and `log_bin_trust_function_creators=1`; schema verifier
  confirmed three existing guarded append-only tables. No global creator
  privilege was granted to the runtime account.
- **Observed and handled:** the schema owner's grant preview failed because
  that narrow account could not inspect another account's grants. The separate
  disposable DBA channel performed preview/apply, then repeated apply safely.
  Both owner-mode selection and actual DBA capabilities are needed for grant
  installation; do not broaden the application account to make it work.
- **Passed:** fresh runtime privilege verification and ordinary Django checks.
  Runtime UPDATE and DELETE on synthetic audit rows were refused with error
  1142; the owner's corresponding attempts were rejected by the append-only
  guards with error 1644. Runtime verification passed again after DB restart.
- **Passed:** database restart preserved binary logging, trust=1 and MIXED log
  format; zero missing trigger definers. Repeated migrations found no work and
  schema verification passed. Rotating the synthetic schema-owner password
  without changing its identity preserved guards and runtime access.
- **Passed with diagnostic fixtures:** nginx configuration validation, HTTPS
  using a locally trusted one-day synthetic certificate, unknown-host rejection,
  and bounded proxy rate limiting (429 observed). No public certificate issuance,
  automated renewal or application-level rate limiting was demonstrated.
- **Baseline gap reproduced:** through a TLS-terminating proxy the unchanged
  app reported the request as insecure and rejected an HTTPS-origin form POST
  with 403. Deployment checks also reported missing HSTS and HTTPS redirect
  (security.W004/W008). A short synthetic lab signing key additionally triggered
  W009; this is a fixture limitation, not evidence about a production secret.
- **Passed with an explicit test-only settings overlay:** setting
  `SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")`,
  `SECURE_SSL_REDIRECT=True` and `SECURE_HSTS_SECONDS=300`, with nginx overwriting
  forwarded protocol, made HTTPS recognition and CSRF work; spoofed client
  protocol was overwritten and HSTS was observed. Missing CSRF was refused.
  Runtime credentials served the Administrator form and committed a role change;
  identical replay succeeded and changed-intent replay returned 409. A Crew
  history request was denied and signed-out users were redirected. SQL counts
  confirmed one grant, one receipt and one success audit for the original write.
  These checks passed again after DB restart and owner password rotation.

The diagnostic HTTP instances used Python's standard-library `wsgiref` server
as UID 1000, **not a production server**. They were separate baseline and overlay
instances, not a verification of DEP-2's production process model. Inter-container
listeners used a private bridge, not the prescribed loopback/Unix-socket host
topology. Synthetic sessions were created by the PR's stub HTTP harness; no
real Google calls were made. These distinctions prevent the diagnostic success
from concealing the baseline configuration failures.

Remaining: pin and test the production server/service launch; implement/review
the deployment settings and app rate limits; configure the WebSocket proxy path
and all three recurring jobs; test actual secret-file loading and rotation,
service startup and timer recovery after host reboot, SSH/host/provider firewall
exposure (IPv4/IPv6), clock/OS patching, public ACME issuance/renewal, alerts,
log redaction, failure/recovery handling and actual cost/capacity. A complete
concrete host profile and the [issue #6](https://github.com/JustApu/CheckYourAmbulance/issues/6) checklist remain required
for issue completion and production use. This PR is being handed off for
independent security review of the current draft and its pending gates.
Backup/restore timing and projection rebuild remain [issue #7](https://github.com/JustApu/CheckYourAmbulance/issues/7) work. This
diagnostic pass changes no approved SPEC requirement and installs no overlay
or new dependency in the repository.

## Documentation consulted

Official documentation consulted 2026-10-21; confirm installed versions when
turning this draft into an executable host profile:

- [Django 6.1 deployment checklist](https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/)
  and [ASGI deployment](https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/).
- [Django proxy SSL trust settings](https://docs.djangoproject.com/en/6.1/ref/settings/#secure-proxy-ssl-header).
- [MariaDB binary logging of routines](https://mariadb.com/docs/server/server-usage/stored-routines/binary-logging-of-stored-routines)
  and [trust variable reference](https://mariadb.com/docs/server/ha-and-performance/standard-replication/replication-and-binary-log-system-variables#log_bin_trust_function_creators).
- [MariaDB server variables](https://mariadb.com/docs/server/server-management/variables-and-modes/server-system-variables#bind_address).
- [ACME challenge types](https://letsencrypt.org/docs/challenge-types/).
- [nginx request limiting](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)
  and [WebSocket proxying](https://nginx.org/en/docs/http/websocket.html).
- [systemd environment and execution](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).
