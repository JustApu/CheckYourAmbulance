# Deployment runbook and server hardening

[issue #6](https://github.com/JustApu/CheckYourAmbulance/issues/6). **Draft; Docker diagnostic checks recorded below, full host verification
pending; not approved for production use.** Implements the operator procedure for [SPEC §17.8](../SPEC.md#178-deployment-secrets-and-recovery-oq-24-issue-16),
DEP-1–DEP-5 and DEP-10. SPEC remains authoritative. This document does not
authorize procurement, production changes, restores or a pilot.

## Readiness and scope

[issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25)'s M1 implementation and real-Workspace verification are complete.
The Docker diagnostic evidence below is historical and does not verify the
current source or a host deployment. Repeat applicable checks against the
current source before treating this runbook as verified. The application
changes this runbook needed are built; each still needs its host check:

| Gate | Current baseline and required completion |
|---|---|
| Production application server | Built ([issue #24](https://github.com/JustApu/CheckYourAmbulance/issues/24)): uvicorn 0.54.0 with its pure-Python `wsproto` WebSocket protocol is pinned in `requirements/base.txt`, and [Proxy and application service](#proxy-and-application-service) gives the one-process launch command. Do not use Django `runserver` publicly. Rehearsed on Rocky Linux 9 (2026-11-02): one process after restart and reboot, socket reachable only by the proxy. |
| HTTPS recognition | Built ([issue #25](https://github.com/JustApu/CheckYourAmbulance/issues/25)): `CYA_TRUST_PROXY_SSL_HEADER`, `CYA_SSL_REDIRECT` and `CYA_HSTS_SECONDS` (below) recognize HTTPS behind the proxy, redirect to it and send HSTS. All are off by default; enable them only with the proxy rule in [Proxy and application service](#proxy-and-application-service). Rehearsed on Rocky Linux 9 (2026-11-02): HTTPS recognized behind nginx, spoofed forwarding headers overwritten, HSTS observed. |
| Application rate limits | Built for sign-in ([issue #26](https://github.com/JustApu/CheckYourAmbulance/issues/26)): `/auth/login/` and `/auth/callback/` are limited in the application per client address (`CYA_SIGNIN_RATE_LIMIT`, `CYA_SIGNIN_RATE_WINDOW_SECONDS`), in addition to the proxy. Export routes do not exist yet; every future export route must apply the same application limiter and a proxy limit before it ships. Rehearsed on Rocky Linux 9 (2026-11-02): both layers answer 429 and spoofed client-address headers do not reset the count. |
| Rehearsal | A Rocky Linux 9 throwaway-host rehearsal ran on 2026-11-02 ([host profile](#rocky-linux-9-host-profile-rehearsed-2026-11-02), [verification record](#throwaway-server-verification-record)). Rows that need vehicles and real checklists, alert delivery or named operators remain pending; SSH source restriction is deferred by owner decision. An Ubuntu 26.04 rehearsal with MariaDB 11.8 (outside the SPEC §17.5 pin; compatibility test only) followed on 2026-11-03 ([host profile](#ubuntu-2604-host-profile-rehearsed-2026-11-03), [evidence](#ubuntu-2604-rehearsal-evidence-2026-11-03-utc)). |

This issue's runbook scope does not include changing application files; the
application changes above were made under their own issues. Backup jobs and restore measurements
belong to [issue #7](https://github.com/JustApu/CheckYourAmbulance/issues/7), alert delivery to [issue #8](https://github.com/JustApu/CheckYourAmbulance/issues/8), paper fallback to [issue #9](https://github.com/JustApu/CheckYourAmbulance/issues/9),
and load/hot-backup/high-availability evaluation to [issue #10](https://github.com/JustApu/CheckYourAmbulance/issues/10).

## Operator preparation and cost envelope

The owner chooses the provider and privately names the accountable operator
and deputy. Their roles may be filled by volunteers, paid staff, or a mix. Both
need tested administrative recovery access; hold names, hosts, keys and account
identifiers outside GitHub. Use a supported Linux release
with security updates, MariaDB 10.11 or 11.8 (SPEC §17.5), Python compatible with the pinned Django
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
   retain console access while testing. DEP-10 permits only HTTPS and restricted
   SSH, plus port 80 only for ACME HTTP-01 challenges (step 7).
7. Use automated certificate renewal. DEP-10 (SPEC 1.23.0, [issue #48](https://github.com/JustApu/CheckYourAmbulance/issues/48)) admits
   port 80 solely for ACME HTTP-01: the proxy answers only
   `/.well-known/acme-challenge/` there and permanently redirects every other request
   to HTTPS, never serving application content over HTTP. DNS-01 with narrowly scoped
   DNS credentials, protected like other secrets, keeps port 80 closed instead.
   Rehearse issuance and renewal with the client's staging service, then check its
   timer and failure alert.

Use the chosen distribution's versioned installation and firewall documentation
when making a concrete host profile; do not guess package names, firewall commands or
unit names. The [Rocky Linux 9 host profile](#rocky-linux-9-host-profile-rehearsed-2026-11-02)
below was verified on a throwaway host, and the [Ubuntu 26.04 host profile](#ubuntu-2604-host-profile-rehearsed-2026-11-03)
on another (with MariaDB 11.8, outside the SPEC §17.5 pin).

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
| `CYA_SIGNIN_RATE_LIMIT` | Sign-in requests allowed per route and client address per window; default 30, allowed 1–1000. Size it for a whole crew signing in from one station address |
| `CYA_SIGNIN_RATE_WINDOW_SECONDS` | The limit's window; default 60 seconds, allowed 1–3600 |
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
Create the database explicitly with `CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci`
(SPEC §17.5), as development and CI do, on either MariaDB version; never rely on the
server default, which differs between 10.11 and 11.8. Confirm it before migrating:
`SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME = 'cya'` must return `utf8mb4` and `utf8mb4_unicode_ci`.
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
Then switch to the runtime file and fresh runtime connections. With an authorized administrator, use
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
local/venv/bin/python manage.py check --deploy
```

Schema inspection uses owner credentials; never grant TRIGGER to runtime to
make it inspect guards. Resolve failed checks before enabling traffic.
`check --deploy` prints warnings without failing. Before the proxy settings in
[Proxy and application service](#proxy-and-application-service) are enabled it
reports `security.W004` and `security.W008`; after them, only `security.W005`
and `security.W021` may remain. Those two remain because HSTS subdomains and
preload stay off unless the owner approves them, so do not add
`--fail-level WARNING` to this command. Any other warning must be
resolved, and none may be silenced to claim readiness. After any grant change restart app
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
Run the pinned uvicorn from the release directory's `local/venv` (where the
hash-checked dependencies were installed above), with the release directory as
working directory so `cya` imports, the runtime-only environment and the
unprivileged service identity:

```sh
local/venv/bin/uvicorn cya.asgi:application \
    --uds /run/cya/app.sock --workers 1 --loop asyncio --http h11 --ws wsproto \
    --lifespan off --no-proxy-headers --no-access-log --no-server-header \
    --timeout-graceful-shutdown 10
```

Each flag is checked against `uvicorn --help` for 0.54.0, and
`cya/tests/test_asgi_server.py` parses this exact block with uvicorn's own
command line and serves HTTP and the live-update WebSocket through it:

- `--workers 1` and no `--reload`: one process (DEP-2). Setting the count
  explicitly stops a `WEB_CONCURRENCY` variable from raising it.
- `--uds`: a Unix socket for the proxy (DEP-10). uvicorn creates a new socket
  file with mode 0666 (0.54.0 `Server.startup`; a file already at that path
  keeps its mode), so the directory controls access. Create it owned by the
  service identity, mode 0750, with a group only the proxy's identity shares,
  and check that other local accounts cannot connect. If loopback is used
  instead, replace `--uds` with `--host 127.0.0.1 --port <port>`; any local
  process can then reach the application.
- `--loop asyncio --http h11 --ws wsproto`: the pinned pure-Python
  implementations, so installing an optional accelerator later cannot change
  them.
- `--lifespan off`: the application's Channels router has no lifespan handler
  and refuses lifespan events.
- `--no-proxy-headers`: otherwise uvicorn trusts `X-Forwarded-For` and
  `X-Forwarded-Proto` from loopback and rewrites the client address and scheme,
  bypassing `CYA_TRUST_PROXY_SSL_HEADER` and the `X-Real-IP` rule below.
- `--no-access-log`: uvicorn's access log records full request lines, including
  OAuth codes in `/auth/callback/` query strings; keep access logging at the
  proxy under the log rules below.
- `--timeout-graceful-shutdown 10`: without it, a restart waits indefinitely for
  open WebSockets to close.

Bound the service's resources and restarts in its supervisor and recheck the
process count after restart. No service unit is installed by this draft.

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
increasing the HSTS duration. `manage.py check --deploy` then no longer reports
`security.W004`/`W008`; `W005`/`W021` remain while subdomains and preload are off. Do not expose generic private files,
configuration directories or database dumps through a catch-all file server.

Rate-limit sign-in at the proxy and in the application. With nginx,
`limit_req_zone` defines the key/rate in the HTTP context and `limit_req` applies
it to locations; use a bounded burst and intentional rejection status. Configure
all `/auth/` entry points, especially login and callback, without blocking
legitimate shared-IP crew sign-ins. Tune and record rates on the rehearsal host.
The application limit (`cya/accounts/ratelimit.py`) counts `/auth/login/` and
`/auth/callback/` separately per client address and answers 429 with
`Retry-After` before any sign-in cookie, session, audit record or call to Google.
Behind the proxy it keys on `X-Real-IP` only when `CYA_TRUST_PROXY_SSL_HEADER=1`,
so the proxy must overwrite that header on every request
(`proxy_set_header X-Real-IP $remote_addr;`). A missing or malformed value falls
back to the connection's address, which every client shares: the proxy's
loopback address, or an empty key over the Unix socket, where uvicorn reports
no client address. Counters live in the
single application process (DEP-2) and reset on restart. Pages that re-read a
panel in the background get 401 when signed out and stop, instead of following a
redirect to `/auth/login/` ([issue #37](https://github.com/JustApu/CheckYourAmbulance/issues/37)), so a tab left open past its
session does not count against the limit; check this on the rehearsal host. Do not trust arbitrary
client IP headers for limiting. Protect exports at both layers before adding their
routes; do not invent a current `/export/` endpoint.

The shell currently includes its stylesheet/script inline; there is no current
static collection step to assume. The application also exposes the authenticated
WebSocket route `/ws/v1/vehicles/<uuid>/live/` through `cya.asgi:application`.
The launch command above serves HTTP and WebSocket in the same process: the
current in-memory Channels layer is process-local, and splitting HTTP
publishing from the socket consumer loses updates. Configure the proxy to
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

## Rocky Linux 9 host profile (rehearsed 2026-11-02)

Verified on a throwaway Rocky Linux 9.8 (x86_64) host with SELinux enforcing, using
synthetic data and release `036d45c`. The installed files are in
[`deploy/rocky9/`](../deploy/rocky9/); replace `__HOST__` with the public host name and
`__ORG_ID__` with the organization id. Run as root (or with sudo) unless stated. Every
package comes from Rocky's own repositories except Certbot (EPEL).

1. **Packages.** MariaDB 10.11 and nginx 1.26 are AppStream module streams. Python 3.12
   comes from AppStream with vendor security updates; Django 6.1 and the project support
   3.12 or later (CI uses 3.13, which Rocky 9 ships only in EPEL).

   ```sh
   dnf module enable -y mariadb:10.11 nginx:1.26
   dnf install -y mariadb-server nginx python3.12 python3.12-devel python3.12-pip gcc \
       mariadb-connector-c-devel pkgconf-pkg-config policycoreutils-python-utils tar certbot
   ```

2. **Firewall and logs.** Remove `cockpit`, keep `https` and `ssh`, keep `http` only for
   ACME HTTP-01 (DEP-10; remove it when using DNS-01), then restrict SSH to the approved
   administration sources:
   `firewall-cmd --permanent --remove-service=cockpit; firewall-cmd --reload`. Make the
   journal persistent (Rocky keeps it in memory by default):
   `install -d -m 2755 -g systemd-journal /var/log/journal; systemctl restart systemd-journald`.

3. **Service identity and release.** `useradd --system --no-create-home --home-dir /srv/cya --shell /sbin/nologin cya`.
   Unpack the reviewed release (`git archive`) into a root-owned
   `/srv/cya/releases/<sha>/`, create `local/venv` with `python3.12 -m venv`, run
   `local/venv/bin/pip install --require-hashes -r requirements/base.txt`, and point
   `/srv/cya/current` at it. SELinux labels `/srv` as `var_t`, which services may not
   execute; label the virtual environments' `bin/` directories as programs, otherwise
   systemd fails with `203/EXEC` and no audit message (SELinux does not audit `access()` checks):

   ```sh
   semanage fcontext -a -t bin_t '/srv/cya/releases/[^/]+/local/venv/bin(/.*)?'
   restorecon -R /srv/cya/releases
   ```

4. **MariaDB.** Install `mariadb-cya.cnf` as `/etc/my.cnf.d/cya.cnf` (loopback bind,
   binary logging, `log_bin_trust_function_creators=1`), then `systemctl enable --now mariadb`
   and run the read-only check in [Database setup and migrations](#database-setup-and-migrations).
   The DBA channel is `sudo mariadb` (root over the local socket). Remove anonymous
   accounts and the `test` database; create the `cya` database with the character set and
   collation in [Database setup and migrations](#database-setup-and-migrations), `cya_owner@127.0.0.1`
   with `ALL PRIVILEGES ON cya.*` only, and `cya_app@127.0.0.1` with no grants. Generate
   passwords and the Django secret key on the host and pass the SQL on standard input,
   never as command arguments (sudo logs full command lines).

5. **Secrets.** `/etc/check-your-ambulance/` (root, `0700`) holds `owner.env`,
   `runtime.env` and `dba.env` (root, `0600`) in systemd `EnvironmentFile=` syntax. `dba.env`
   is `owner.env` with `CYA_DB_USER=root`, `CYA_DB_HOST=localhost` and a placeholder
   password, because MariaDB's root authenticates over the socket as OS root; it is used
   only for grant installation.

6. **Management commands.** Install `cya-manage` as `/usr/local/sbin/cya-manage` (root,
   `0750`) and call it by full path (Rocky's sudo `secure_path` omits `/usr/local/sbin`).
   It starts `manage.py` as a detached transient oneshot unit (`systemd-run --no-block`)
   with the chosen environment file, as `cya` (or root for `dba`), from the read-only
   release, with a 15-minute start timeout; it then follows that unit's journal and exits
   with the command's status. Nothing is attached to the unit's input or output, so a
   dropped SSH session ends only the follower: in the rehearsal an earlier attached
   `systemd-run --wait --pipe` cut off by an SSH timeout stalled new logins until a reboot,
   while this runner, killed mid-command, let the command finish with logins unaffected.
   The unit keeps its result until the runner reads it and then stops and resets the unit.
   If the runner is interrupted (Ctrl+C or a dropped session), it stops its journal follower
   and starts a detached helper unit, `<unit>-cleanup`, that waits for the command to end,
   logs its result (`journalctl -u <unit>-cleanup`) and then stops and resets the unit.
   There is no interactive input; use `sudo mariadb` for a SQL shell.

   ```sh
   /usr/local/sbin/cya-manage owner migrate --noinput
   /usr/local/sbin/cya-manage owner verify_audit_schema
   /usr/local/sbin/cya-manage dba audit_runtime_grants --user cya_app --host 127.0.0.1 --dry-run
   /usr/local/sbin/cya-manage dba audit_runtime_grants --user cya_app --host 127.0.0.1
   /usr/local/sbin/cya-manage runtime verify_audit_runtime
   /usr/local/sbin/cya-manage owner create_organization --name '<name>' --time-zone '<zone>' --dry-run
   ```

7. **Application service.** Install `cya.service` (the launch command in
   [Proxy and application service](#proxy-and-application-service), as `cya`, with the
   runtime file, bounded restarts and memory, and systemd sandboxing). `/run/cya` is its
   runtime directory (`0750`, `cya:cya`); add nginx to group `cya` (`usermod -aG cya nginx`)
   so only the proxy can reach the socket. SELinux needs the directory labelled for the web
   server and one rule letting nginx connect to a socket owned by a systemd service:

   ```sh
   semanage fcontext -a -t httpd_var_run_t '/var/run/cya(/.*)?'   # /run is an alias of /var/run
   checkmodule -M -m -o cya_nginx_socket.mod cya_nginx_socket.te
   semodule_package -o cya_nginx_socket.pp -m cya_nginx_socket.mod
   semodule -i cya_nginx_socket.pp
   systemctl enable --now cya
   ```

   The module allows `httpd_t` to connect to Unix sockets of any `unconfined_service_t`
   service; the directory permissions and label are what confine it to this socket. A
   dedicated SELinux domain for the application would be narrower and is not built.

8. **nginx and certificate.** Replace `/etc/nginx/nginx.conf` with `nginx.conf` (keep the
   stock file): it logs paths without query strings and, on port 80, serves only
   `/.well-known/acme-challenge/` from `/var/lib/cya-acme` (label `httpd_sys_content_t`)
   and redirects everything else to `https://__HOST__`. Rehearse with Let's Encrypt
   staging, then issue the real certificate with a reload hook and check renewal:

   ```sh
   certbot certonly --staging --webroot -w /var/lib/cya-acme -d __HOST__ -m '<operator email>' --agree-tos --cert-name cya-staging
   certbot delete --cert-name cya-staging
   certbot certonly --webroot -w /var/lib/cya-acme -d __HOST__ -m '<operator email>' --agree-tos --cert-name cya --deploy-hook 'systemctl reload nginx'
   systemctl enable --now certbot-renew.timer
   certbot renew --dry-run --cert-name cya
   ```

   Then install `nginx-cya.conf` as `/etc/nginx/conf.d/cya.conf` and
   `cya_proxy_headers.conf` in `/etc/nginx/`, add `CYA_TRUST_PROXY_SSL_HEADER=1`,
   `CYA_SSL_REDIRECT=1` and `CYA_HSTS_SECONDS=300` to the environment files, and restart
   `cya`. Unknown TLS host names get no handshake (`ssl_reject_handshake`). The shared
   headers are an include because a location-level `proxy_set_header` replaces every
   inherited one. `/auth/` logs errors at `crit` only, because nginx error lines carry
   the request line, including callback codes.

9. **Recurring jobs.** Install the `cya-obligations` and `cya-sessions` service and timer
   units (the obligations unit names the organization) and `systemctl enable --now` both
   timers. A oneshot service never runs twice at once, so a late timer does not overlap.
   Do not install a notification timer until a sender is configured: the delivery command
   refuses to run without one.

10. **Teardown.** Remove any temporary passwordless sudo rule and test credentials, and
    destroy the throwaway host and its certificate when the rehearsal is recorded.

## Ubuntu 26.04 host profile (rehearsed 2026-11-03)

Verified on a throwaway Ubuntu 26.04.1 LTS (x86_64) host with AppArmor enabled, using
synthetic data and release `05cb656`. It follows the [Rocky Linux 9 host profile](#rocky-linux-9-host-profile-rehearsed-2026-11-02)
step by step and reuses its files from [`deploy/rocky9/`](../deploy/rocky9/), except
[`deploy/ubuntu2604/nginx.conf`](../deploy/ubuntu2604/nginx.conf). Every package comes from
Ubuntu's own repositories. Only the differences are listed here.

**MariaDB version.** Ubuntu 26.04 ships MariaDB 11.8. This profile was first rehearsed as a
compatibility test (2026-11-03); SPEC 1.25.0 ([issue #56](https://github.com/JustApu/CheckYourAmbulance/issues/56)) then allowed 11.8 alongside 10.11, and
CI tests both. The application suite's version check accepts exactly those two versions.

1. **Firewall first.** The image has no firewall tool. Install `ufw`, then, with console
   access or a scheduled rollback (`systemd-run --on-active=300 /usr/sbin/ufw disable`) in
   case SSH is cut off: `ufw default deny incoming`, `ufw default allow outgoing`,
   `ufw allow OpenSSH`, `ufw allow 443/tcp`, `ufw allow 80/tcp` (ACME HTTP-01 only, DEP-10;
   omit with DNS-01) and `ufw --force enable`. Confirm a fresh SSH login, then stop the
   rollback timer. Rules apply to IPv4 and IPv6. Restrict SSH sources as on Rocky when the
   owner lifts the deferral.

2. **Packages.** No module streams or EPEL:

   ```sh
   apt-get install -y mariadb-server nginx python3-venv python3-dev build-essential \
       libmariadb-dev pkg-config certbot
   ```

   Python is 3.14 (the project supports 3.12 or later; CI uses 3.13). The journal is
   already persistent, and Certbot's `certbot.timer` is enabled by the package.

3. **Service identity and release.** As on Rocky, with `--shell /usr/sbin/nologin` and
   `python3 -m venv`. No SELinux labels are needed.

4. **MariaDB.** Install `mariadb-cya.cnf` as `/etc/mysql/mariadb.conf.d/60-cya.cnf`. The
   stock server already binds to `127.0.0.1`. MariaDB 11's default collation for
   `utf8mb4` is `utf8mb4_uca1400_ai_ci`, so the explicit character set and collation in
   [Database setup and migrations](#database-setup-and-migrations) matter here. The DBA
   channel is `sudo mariadb`, as on Rocky.

5. **Secrets and management commands.** As on Rocky. sudo's `secure_path` includes
   `/usr/local/sbin`; the rehearsal still called `cya-manage` by full path.

6. **Application service.** Install `cya.service` unchanged. nginx runs as `www-data`, so
   `usermod -aG cya www-data`. No AppArmor profile confines nginx or Python, so no socket
   rule is needed.

7. **nginx and certificate.** Keep a copy of the stock `/etc/nginx/nginx.conf` and install
   `deploy/ubuntu2604/nginx.conf` in its place (worker user `www-data`, modules from
   `/etc/nginx/modules-enabled/`); it no longer includes `sites-enabled/`, so the default
   site is not served. Certificate steps are as on Rocky, except the renewal timer is
   `certbot.timer` (already enabled). Then install `nginx-cya.conf` and
   `cya_proxy_headers.conf` and set the HTTPS variables as on Rocky.

8. **Google sign-in.** Besides the client ID, secret and hosted domain, `runtime.env` needs
   `CYA_GOOGLE_REDIRECT_URI=https://__HOST__/auth/callback/`, and the OAuth client must list
   that address; without it, sign-in stays unavailable (503).

9. **Recurring jobs and teardown.** As on Rocky.

## Throwaway-server verification record

Run the procedure on a disposable host using the current source with synthetic
data only. Record the full source SHA, tested component versions, date, tester,
pass/fail and sanitized explanation for each row. Do not commit hostnames,
addresses, account IDs, credentials or raw output. No row below is claimed fully
passed by the earlier Docker subset. The Rocky Linux 9 rehearsal on 2026-11-02
([evidence](#rocky-linux-9-rehearsal-evidence-2026-11-02-utc)) passed, partly passed or left
pending each row as stated. The statuses below are Rocky's. The Ubuntu 26.04 rehearsal
([evidence](#ubuntu-2604-rehearsal-evidence-2026-11-03-utc)), on MariaDB 11.8 (not the
approved version), repeated the build, firewall, TLS, MariaDB, runtime-isolation,
recurring-job, restart, sign-in, proxy-trust/CSRF, rate-limit and WebSocket-origin checks
listed there, with the same results.

| Check | Required evidence | Status |
|---|---|---|
| Build and dependency installation | Hash-checked install and selected server command/unit verified against installed help; one app process after reboot | Passed (2026-11-02) |
| SSH/firewall | Operator and deputy access survives policy reload; only HTTPS and restricted SSH exposed; DB/upstream denied externally over IPv4/IPv6 | Partial: only 22/80/443 answer, database and upstream unreachable, no global IPv6. SSH source restriction deferred by owner decision (2026-11-02; compensating controls in place); deputy access untested |
| TLS/renewal | Trusted certificate, host rejection, staging renewal and timer/failure handling; port 80 closed, or serving only ACME HTTP-01 and redirecting everything else (DEP-10) | Partial: trusted certificate, unknown-name rejection, staging issuance, renewal dry run and timer passed; port 80 serves only ACME challenges and redirects the rest; renewal-failure alert waits for issue #8 |
| Proxy trust/CSRF | Spoofed forwarding headers do not change trust; HTTPS POST works, missing/invalid CSRF refused; secure cookie flags and HSTS observed | Passed (2026-11-02): spoofed headers overwritten; a signed-in HTTPS form POST (role change) succeeded; missing token and `http` Origin refused; HSTS observed; session cookie Secure, HttpOnly and SameSite=Strict confirmed in the owner's browser |
| Secrets/rotation | Root-only files, runtime identity cannot read owner file, no secret-bearing logs; rotation/restart/reauthentication tested | Partial: root-only files, no secrets in logs, database password rotation with restart passed (Rocky). Signing-key rotation and reauthentication passed on Ubuntu (2026-11-03): the new key was generated on the host and written to the three root-only files, the application restarted, the existing session was refused at once and a new Google sign-in succeeded and was audited. Google's silent re-sign-in made it invisible to the user, so do not rely on users noticing a rotation. Rocky's key was not rotated |
| MariaDB/DEP-5 | 10.11 binary logging on, trust=1 persistent after rebuild; owner has no SUPER; migration/guard checks pass and definers preserved | Passed for restart and reboot (2026-11-02); persistence after a rebuild belongs to the issue #7 restore rehearsal |
| Runtime isolation | Fresh runtime verification and system/deploy checks pass; negative UPDATE/DELETE tests use synthetic disposable rows only | Passed (2026-11-02): runtime and deploy checks pass; runtime refused (1142) for UPDATE, DELETE and DDL; on real sign-in rows the schema owner was refused (1644, append-only) for UPDATE and DELETE on audit, receipt and role-grant tables, each inside a rolled-back transaction |
| Schedule and bindings | Agency schedule rules and checklist bindings are present; initial obligation generation succeeds and is idempotent | Pending: synthetic organization only; agency rules and real checklists (OQ-01) not loaded |
| Recurring jobs | All three jobs use runtime identity, survive reboot, report/alert failures, and notification delivery cannot overlap | Partial: obligation and session jobs run as the runtime identity and survive reboot; notification job not installed (no sender); failure alerts wait for issue #8 |
| WebSocket proxy | ASGI serves HTTP and WebSocket in one process; proxy upgrade, origin rejection, authorization recheck and reconnect recovery are verified | Partial: one process serves both, upgrades reach the application through nginx and an unknown Origin is refused; authorized connection, recheck and reconnect wait for sign-in |
| Integration | [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) authorized action persists and appears in history/audit; denial recorded; same operation ID produces one result | Partial: real Workspace sign-in provisioned Crew; first-administrator bootstrap (dry run writes nothing, repeat refused and audited); a Supervisor grant and revoke from the Administration page each wrote a receipt, role row and audit record; a denied sign-in was audited. Same-operation replay passed on both rehearsal hosts (2026-11-03): a repeated command with the same operation ID returned the stored answer and wrote nothing, and a changed body under it got the original answer flagged as reused (the API's `409 OPERATION_REUSED`) |
| Rate limits | Authentication limits enforced by both layers and tested with bounded requests; exports remain gated until implemented | Passed (2026-11-02) |
| Restart/failure | App/DB restart, interrupted migration recovery plan and prior-code compatibility examined; failure keeps traffic closed | Partial: app, database and host restarts recover unaided; interrupted-migration recovery and prior-code compatibility not exercised |
| Data/log hygiene | Reviewed logs omit tokens, callback query and cookies; synthetic host artifacts/credentials destroyed at completion | Partial: a marked callback code and session cookie appeared in no application or nginx log; the throwaway host is not yet destroyed |

[issue #6](https://github.com/JustApu/CheckYourAmbulance/issues/6) cannot close until its server checks and independent security review
pass. DEP-12 additionally requires named operators and [issue #7](https://github.com/JustApu/CheckYourAmbulance/issues/7)'s restore
rehearsal, including chosen-timestamp recovery and realistic-size timing.
Monitoring, paper readiness and load evidence retain their own issue gates.

## Rocky Linux 9 rehearsal evidence, 2026-11-02 UTC

Tester: Claude (Anthropic), as operator on an owner-provided throwaway host, with synthetic
data and source `036d45c4c130b85ee05384a5c7973cc7ffabd300` (main after issue #19). Host
names, addresses, accounts and raw output stay with the owner.

| Component | Version |
|---|---|
| Operating system | Rocky Linux 9.8, x86_64, SELinux enforcing, systemd 252 |
| MariaDB | 10.11.18 (AppStream `mariadb:10.11` stream) |
| nginx | 1.26.3 (AppStream `nginx:1.26` stream) |
| Python | 3.12.15 (AppStream) |
| Application | Django 6.1.2, uvicorn 0.54.0, from a fresh `pip install --require-hashes` |
| Certificate client | Certbot 3.1.0 (EPEL), Let's Encrypt |

Results:

- **Passed:** hash-checked install (mysqlclient built against Connector/C); 74 migrations
  by a schema owner without SUPER or global privileges, with binary logging on (MIXED) and
  `log_bin_trust_function_creators=1`; `verify_audit_schema` (40 append-only and 44
  command-guarded tables); all 166 triggers defined by the schema owner; DBA grant preview
  (69 statements), apply and repeat apply; `verify_audit_runtime`;
  `check --fail-level WARNING`; `check --deploy` reports W004/W008 before the proxy settings
  and only W005/W021 after.
- **Passed:** one uvicorn process on the Unix socket; another local account cannot reach
  the socket; HTTPS through nginx with a Let's Encrypt certificate; unknown TLS host
  names refused; port 80 redirects to HTTPS; HSTS (300 s) and security headers present;
  a client-supplied `X-Forwarded-Proto: http` is overwritten; CSRF passes with the HTTPS
  Origin and refuses a missing token or an `http` Origin; 40 rapid sign-in requests with
  rotating spoofed `X-Real-IP` gave 30 application answers, then the application's 429
  with `Retry-After`, then nginx's 429; WebSocket upgrades reach the application, which
  refuses an unknown Origin.
- **Passed:** after a reboot every service and timer started, with one application
  process, the socket's label and permissions, the database settings and firewall rules
  intact, fresh runtime verification, and no SELinux denials. Database password
  rotation for both accounts kept runtime access, the schema and all trigger definers.
  The runtime account was refused (error 1142) for UPDATE, DELETE, CREATE TABLE and CREATE
  TRIGGER. Certbot's staging issuance and renewal dry run passed.
- **Found and fixed during the rehearsal** (now in the host profile): the minimal image
  lacks `tar`; SELinux blocks executing the virtual environment's scripts until they are
  labelled `bin_t`; nginx needs the socket directory labelled `httpd_var_run_t` and one
  local policy rule to connect; Rocky's journal is not persistent by default; sudo's
  `secure_path` omits `/usr/local/sbin`; a `systemd-run --wait --pipe` cut off by an SSH
  timeout left new logins hanging until a reboot, so `cya-manage` now always runs the
  command as a detached unit and only follows its journal (re-tested by killing the
  session mid-command: the command finished and logins were unaffected).
- **Passed with real Google sign-in** (the owner's internal OAuth client; synthetic
  organization): sign-in redirects to Google with a `Secure`, `HttpOnly`, `SameSite=Lax`
  sign-in cookie limited to `/auth/`; the owner's session cookie is `Secure`, `HttpOnly`
  and `SameSite=Strict`; first sign-in provisioned Crew; `bootstrap_administrator` with
  the runtime role wrote nothing on `--dry-run`, granted Administrator, and refused and
  audited a repeat; a Supervisor grant and revoke from the Administration page each
  committed a receipt, role row and audit record; the schema owner was refused (1644)
  for UPDATE and DELETE on those real rows; no server errors after sign-in was configured.
- **Pending:** an authorized WebSocket connection, recheck and reconnect (no vehicles
  loaded); signing-key rotation and reauthentication (done on Ubuntu instead);
  agency rules and real checklists; alert delivery (issue #8); deputy access (SSH source
  restriction deferred by owner decision); interrupted-migration recovery and prior-code
  compatibility; host teardown. The port-80 rule is now DEP-10 (SPEC 1.23.0, issue #48).

## Ubuntu 26.04 rehearsal evidence, 2026-11-03 UTC

Tester: Claude (Anthropic), as operator on an owner-provided throwaway host, with synthetic
data and source `05cb656051dee42df56d42672f3180564152831d` (PR #47 at the time) for the
deployment and the full suite; `cya-manage` was later replaced by the PR #47 version with
interrupt cleanup. Host names, addresses, accounts and raw output stay with the owner.

| Component | Version |
|---|---|
| Operating system | Ubuntu 26.04.1 LTS, x86_64, AppArmor enabled, systemd 259, sudo-rs 0.2.13 |
| MariaDB | 11.8.6 (Ubuntu package; outside the SPEC §17.5 pin) |
| nginx | 1.28.3 (Ubuntu package) |
| Python | 3.14.4 (Ubuntu package) |
| Application | Django 6.1.2, uvicorn 0.54.0, from a fresh `pip install --require-hashes` |
| Certificate client | Certbot 4.0.0 (Ubuntu package), Let's Encrypt |
| Firewall | ufw 0.36.2 |

Results:

- **Passed:** firewall admits only 22, 80 and 443 over IPv4 and IPv6, with database, DNS
  and other ports dropped from outside; hash-checked install (mysqlclient built against the
  11.8 client library); 74 migrations in 141 s by a schema owner without SUPER, with
  binary logging on and `log_bin_trust_function_creators=1`; `verify_audit_schema` (40
  append-only and 44 command-guarded tables); all 166 triggers defined by the schema owner;
  DBA grant preview, apply and `verify_audit_runtime`; `check --deploy` reports only
  W005/W021 with the proxy settings.
- **Passed:** one uvicorn process on the Unix socket, reachable by nginx and refused to
  another local account; HTTPS with a Let's Encrypt certificate (staging first, then
  production, then a renewal dry run); unknown TLS names and requests by address get no
  handshake; port 80 serves only ACME challenges and redirects the rest; HSTS (300 s) and
  security headers present; obligation and session jobs run and their timers are set.
- **Passed:** after a reboot the firewall, MariaDB (same settings), nginx, the application
  and all three timers started with no failed units; the database and upstream ports stayed
  closed from outside.
- **Passed with real Google sign-in** (the owner's internal OAuth client; synthetic
  organization): the sign-in redirect uses PKCE (S256), the hosted-domain hint and the
  configured redirect address, with a `Secure`, `HttpOnly`, `SameSite=Lax` sign-in cookie
  limited to `/auth/`; first sign-in provisioned Crew; `bootstrap_administrator` wrote
  nothing on `--dry-run`, granted Administrator and refused and audited a repeat; a
  Supervisor grant and revoke each committed a receipt, role row and audit record; the
  owner confirmed the session cookie is `Secure`, `HttpOnly` and `SameSite=Strict`; the
  runtime account was refused (1142) and the schema owner (1644) for UPDATE and DELETE on
  real audit rows and on deficiency severities, inside rolled-back transactions; no
  application or nginx errors and no callback codes in nginx logs. One earlier sign-in
  attempt was refused and audited as expired.
- **Passed (2026-11-03, from outside the host):** client-supplied `X-Forwarded-Proto: http`,
  `X-Forwarded-Host` and `X-Forwarded-Port` changed nothing (the normal redirect to sign-in);
  a `Host` other than the TLS name got nginx's 421; an unauthenticated API POST with the HTTPS
  Origin and a CSRF token reached the application (401), while a missing token or an `http`
  Origin was refused (403 `CSRF_REJECTED`); WebSocket upgrades reached the application, which
  refused an unknown Origin (and an unauthenticated one) with 403; 40 rapid sign-in requests with
  rotating spoofed `X-Real-IP` and `X-Forwarded-For` gave 30 application answers, then the
  application's 429 with `Retry-After`, then nginx's 429. Same-operation replay through
  `cya-manage` on both hosts: a repeated command returned the stored answer and wrote nothing;
  a changed body under the same operation ID got the original answer flagged as reused (409 at
  the API) and wrote nothing. Signing-key rotation: a new key generated on the host replaced
  the shared key in the three root-only files and the application restarted; the owner's
  existing session cookie was refused at once (302 to sign-in) and Google signed the owner in
  again within a second without showing a page, so the rotation was not visible to the user;
  the new sign-in was audited and no key value appeared in any output or log.
- **Found during the rehearsal** (now in the profile): no firewall tool in the image;
  MariaDB 11's default collation; the nginx worker user and modules path; the
  redirect-address setting.
- **Full test suite (2026-11-03, source `05cb656`)**, run on each rehearsal host as a detached
  unit under an unprivileged test identity with a temporary DBA-level test account (dropped
  afterwards), `--parallel 8`, durability relaxed as in CI and restored afterwards:
  - Rocky Linux 9 (MariaDB 10.11.18, Python 3.12): 1,825 tests ran: 1,766 passed and 59 skipped; the
    helper and authorization-policy tests passed; 77 minutes in all, 74 for the suite.
  - Ubuntu 26.04 (MariaDB 11.8.6, Python 3.14): of 1,825, 1,761 passed and 59 skipped; 101 minutes in all,
    98.5 for the suite. Failures: the deliberate `test_server_is_mariadb_10_11` version check,
    and four network-guard tests, because on Python 3.14 parallel workers start by
    `forkserver` and do not install the test network guard
    ([issue #53](https://github.com/JustApu/CheckYourAmbulance/issues/53); CI's Python 3.13
    forks and is unaffected). Every database test, including the migration-reversal,
    trigger and database-security tests, passed on 11.8. A first attempt stopped at the
    version check because a parallel worker could not report it; `tblib` was added to that
    throwaway test environment only.
  - The slowest tests on both hosts are migration reversals (the kit migration test took 923 s
    on Rocky and 1,116 s on Ubuntu).
- **Pending:** as for Rocky, plus the SPEC decision on MariaDB 11.8.

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
for issue completion and production use. (As of 2026-11-02 the server pin,
deployment settings and application rate limits listed here are built; see
[Readiness and scope](#readiness-and-scope).)
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
- uvicorn 0.54.0: the installed `uvicorn --help` and package source, checked
  2026-11-02, for every launch flag and the Unix-socket mode.
- [systemd environment and execution](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).
