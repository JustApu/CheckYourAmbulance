# OQ-24 architecture spike — results and stack recommendation (Perl/Mojolicious and Python/Django on MariaDB and MySQL)

**Issue:** [issue #15](https://github.com/JustApu/CheckYourAmbulance-archive/issues/15) (part of milestone M0, [issue #7](https://github.com/JustApu/CheckYourAmbulance-archive/issues/7)) • **Date:** 2026-10-20 • **Status:** evidence and recommendation. **Decided 2026-10-20: the owner chose option C (Python/Django + MariaDB 10.11)**, recorded in [SPEC §17.5](../../SPEC.md#175-hosting-database-engine-backup-oq-24) by amendment under [§25.4](../../SPEC.md#254-amendments) (SPEC version 1.1.0). The text below is the evidence the owner weighed.

Prototype code is in [`spike/oq24/`](../../spike/oq24/README.md) (Perl) and [`spike/oq24-python/`](../../spike/oq24-python/README.md) (Python) and is throwaway. All data is synthetic. At the owner's request the Python candidate was also run (§5), replacing the desk-only fallback assessment first planned.

## 1. Environment actually used

Ubuntu 24.04, Perl 5.38.2, Mojolicious 9.51, DBD::MariaDB 1.24, DateTime 1.67, Crypt::JWT 0.038; **Python 3.13.16 with Django 6.1.2, mysqlclient 2.3.0, PyJWT 2.15.1, Channels 4.3.2, Daphne 4.2.3 (§5); **MariaDB 10.11.14** (distribution package) and **MySQL 8.0.46** (binaries extracted from the distribution packages and run privately), each as a private socket-only instance. The owner authorized installing database software in this temporary container.

**Documentation access.** Official vendor/framework documentation sites were unreachable from this environment (requests refused). Statements below are therefore verified against (a) the **installed module documentation** (POD shipped with the exact versions tested) and (b) **observed behavior of the real software**. Where neither applies the item is marked **UNVERIFIED**.

## 2. Results by acceptance criterion ([issue #15](https://github.com/JustApu/CheckYourAmbulance-archive/issues/15))

Result key: **PASS** = demonstrated by an executable test that passes on the stated server(s).

| # | Criterion | MariaDB 10.11 | MySQL 8.0 | Evidence |
|---|---|---|---|---|
| 1 | Application account cannot UPDATE/DELETE append-only tables but can append (ARC-2) | PASS | PASS | `t/01-privileges.t` (33 checks each): UPDATE, DELETE, TRUNCATE, DROP, ALTER, DROP TRIGGER, REPLACE and INSERT…ON DUPLICATE KEY UPDATE are all refused for the least-privilege account; INSERTs succeed. Triggers also block UPDATE/DELETE for an account mistakenly granted them, and enforce author-only correction (COR-1). |
| 2 | Concurrency: replay → one record; distinct concurrent operations all kept; conflict/ack semantics (CON-3, CON-5, REC-4, AC-31) | PASS | PASS | `t/03-concurrency.t`: 8 simultaneous processes. Replays of one operation id → exactly one record; lost-response retry → same record; 8 distinct concurrent operations → all 8 recorded with their own actors (4 FAILED, 4 PASSED survived); a conflict warning commits nothing and leaves the id reusable; acknowledging a stale warning returns a fresh warning; audit event written in the same transaction. **40 repeated runs per server, 0 failures.** |
| 3 | Strict data handling; startup check (A.4 item 3) | PASS | PASS | `t/02-strict.t`: oversized values, invalid enum, NULL, foreign-key and CHECK violations rejected. A deliberately non-strict session **silently truncated** an oversized value (risk demonstrated), and a startup/per-connection check detects it. CHECK constraints still reject in a non-strict session. Server defaults: MariaDB `STRICT_TRANS_TABLES`; MySQL adds `NO_ZERO_DATE` etc. |
| 4 | Idempotent obligation generator across spring-forward/fall-back, UTC storage (TIME-3, AC-4) | PASS | PASS | `t/04-dst.t`: nonexistent 02:30 → first instant after the gap; ambiguous 01:30 → first occurrence; windows exactly 2 h wide on 23- and 25-hour days; re-runs and overlapping runs create no duplicates; all stored windows 2 h in UTC. America/New_York is a synthetic test zone (the real zone is OQ-25). |
| 5 | Backup and point-in-time restore to a chosen instant, then projection comparison (ARC-7, AC-26) | PASS | PASS | `t/05-restore.t`: logical backup with binary-log coordinates, later activity, replay of the binary log to a chosen **position**, restored status projection equals the live projection captured at that instant; post-instant activity absent; append-only triggers survive the restore. |
| 6a | Verified ID-token validation mechanics (AUTH-3, AC-22) | PASS (library) | n/a | `t/06-token.t` (21 checks, synthetic RSA keys): signature, kid rotation, issuer, audience, expiry, issued-at, subject, `email_verified`, organization claim, tampering, `alg=none` and HS256-with-public-key confusion all rejected/handled. **Google's actual claim semantics are UNVERIFIED** (see §4). |
| 6b | Server push, session/CSRF tooling (REC-3, 16.2) | PASS (WebSocket, CSRF, cookie flags); SSE partly | n/a | `t/07-push-csrf.t` (43 checks): two clients on a vehicle receive a pushed event after a committed write; other vehicles do not; subscriptions clean up; CSRF-protected POST refused without a token; session cookie has Secure, HttpOnly, SameSite=Strict. |

Static checks: `perl -wc` on every Perl file and `bash -n` on every script pass. **perlcritic and shellcheck are not installed here and were not run.**

## 3. Findings the spec and the M1 contracts must account for

1. **No `UPDATE`/`DELETE` also means no `REPLACE` and no `INSERT … ON DUPLICATE KEY UPDATE`.** Idempotent writes must be built from the unique key: attempt the INSERT, catch the duplicate-key error (1062), then read and return the original record. `INSERT IGNORE` must **not** be used: it downgrades other errors (truncation, invalid values) to warnings and defeats strict mode.
2. **Locking requires a different privilege on each server.** MySQL 8 refuses `SELECT … FOR UPDATE` to an account without `UPDATE`, `DELETE` or `LOCK TABLES` (observed error); MariaDB accepted it with `SELECT` only. The portable design is a small dedicated serialization table (`item_lock`) on which the app holds `UPDATE`; it contains no business data, so immutability of the audit/check tables is unaffected.
3. **Lazily creating the lock row inside the check transaction deadlocked** (InnoDB error 1213) under 8 concurrent writers on first use. Fix applied: lock rows are created when the checklist version is published (an `INSERT`-only operation), the check transaction only locks an existing row, and the write is wrapped in a bounded retry on deadlock. Retrying is safe because the aborted transaction is fully rolled back and the operation id makes the retry idempotent; this is **not** a retry of an ambiguous transport failure. The contracts issue ([issue #20](https://github.com/JustApu/CheckYourAmbulance-archive/issues/20)) must make "create lock rows at publish" a requirement.
4. **`sql_mode` is session-changeable by any account.** Strictness must be enforced on every connection and checked at startup; CHECK constraints and triggers provide a second layer that does not depend on `sql_mode`.
5. **Database privileges are not a defense against the database administrator.** A root/admin account can drop triggers or change grants. Operationally: the application never uses an admin account, admin use is rare and logged outside the database, and the production design should consider shipping audit events to a second store. This was not tested.
6. **Tool differences between the servers** (both handled in `t/05-restore.t`): `mysqlbinlog --database` filters on the *rewritten* database name in MySQL 8 but the *original* name in MariaDB; `--master-data` is deprecated in MySQL 8.0 (`--source-data`); `tx_isolation` (MariaDB) vs `transaction_isolation` (MySQL).
7. **`Crypt::JWT` accepts a token with no `exp` claim by default** (documented: `verify_exp` default is "valid if present", demonstrated by a test). The validator must set `verify_exp => 1`, pin `accepted_alg => 'RS256'`, and require issuer, audience and subject.
8. **Mojolicious `write_sse` is documented EXPERIMENTAL** in 9.51, its drain callbacks received an extra argument that breaks strict subroutine signatures, and an in-process test client did not expose the streamed body (a real HTTP client, curl, saw correct framing). **Recommendation: use WebSocket as the live-update channel** (fully demonstrated); revisit SSE only if a reason appears.
9. **Driver:** `DBD::MariaDB` built and worked against both servers (it uses MariaDB Connector/C and authenticated to MySQL 8). `DBD::mysql` failed to build in the same environment alongside the MariaDB development libraries; it was not investigated further. Long-term maintenance of the chosen driver is **UNVERIFIED**.
10. **Time:** `DateTime` raises errors for ambiguous/nonexistent local times, so TIME-3 needed a small resolver (`Spike::Time`), now covered by tests. The real station zone and the `DateTime::TimeZone` data update process are deployment items.

## 4. Not verified (explicit gaps)

- **Google ID-token claim semantics** (whether `hd` plus `email_verified` is the right organization test, key-rotation endpoint behavior, and how disabling a Workspace account is detected within the revocation bound): official Google documentation was unreachable. [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) requires a separate real-Workspace verification, and this must be settled from Google's documentation before M1 sign-in is implemented.
- Point-in-time restore by **timestamp** (only by log position was tested); **physical/hot backup tools** (only logical dump plus binary log); restore time at realistic data sizes; load and latency under production-like concurrency; replication or high availability.
- The Python prototypes were run only on MariaDB, and no Python linter or type checker was run.
- Public-exposure hardening: TLS termination, rate limiting, reverse proxy, secret storage, dependency audit.
- MySQL vs MariaDB support lifecycle and licensing (see §6; from general knowledge, **UNVERIFIED**).
- Maintenance status and security record of the Perl libraries beyond the versions tested.
- Contributor and hiring pool for a Perl codebase maintained by volunteers (not assessed).

## 5. Python/Django candidate (run on MariaDB 10.11)

The owner asked whether Python works with MariaDB. The identical schema and criteria were run with Python 3.13.16, Django 6.1.2, mysqlclient 2.3.0 (built against the MariaDB client library), PyJWT 2.15.1, Channels 4.3.2 and Daphne 4.2.3, in two forms: the raw driver, and the Django ORM using the same least-privilege account (unmanaged models over the spike schema, since that account has no DDL rights).

| Criterion | Python result | Evidence |
|---|---|---|
| 1 Append-only enforcement | PASS | `tests/test_privileges.py`: identical refusals as in Perl (1142/1419); triggers and author rule enforced; the ORM's `update()` and `delete()` hit the same privilege wall. |
| 2 Concurrency, idempotent replay | PASS (raw driver and Django ORM) | `tests/test_concurrency.py`, `tests/test_django_orm.py`: 8 forked processes, replay → one record, 8 distinct operations all kept, conflict/acknowledgement semantics, audit in the same transaction; ORM variant uses `transaction.atomic()` + `select_for_update()` on a composite-key lock model. **Repeated runs: raw driver 28, ORM 55, 0 failures.** |
| 3 Strict data handling | PASS | `tests/test_strict.py`, ORM error mapping; a non-strict session still truncates (same risk). Django does **not** add strictness: its session `sql_mode` was simply the server default. |
| 4 DST obligations | PASS | `tests/test_dst.py` with the standard-library `zoneinfo`; same expected instants as Perl. |
| 5 Backup + point-in-time restore | PASS | `tests/test_restore.py`: same server tools, same result. |
| 6a Token validation (PyJWT) | PASS | `tests/test_token.py` (synthetic keys); Google's claim semantics remain UNVERIFIED. |
| 6b CSRF, cookies, push | PASS | `tests/test_web.py`: Django's CSRF protection, Secure/HttpOnly/SameSite=Strict session cookie, WebSocket group push with Channels (in-process communicator), and SSE framing from Django `StreamingHttpResponse` under Daphne, read with a real client. |

All 22 Python tests pass. Static checks: none run for the Python files beyond import/execution (no linter installed); disclosed as a gap.

Python-specific findings:

1. **Django needs explicit database setup the framework does not do for you:** its default session time zone was `SYSTEM` (set `init_command` to UTC so server-side defaults like `CURRENT_TIMESTAMP(6)` are UTC) and it did not add strict SQL mode (the server default was strict; enforce it as in the Perl design).
2. **Django's migration tool needs DDL rights**, so the deployment must keep a separate schema-owner account for migrations and a least-privilege runtime account; models over tables the runtime account cannot alter must be unmanaged or migrated by the owner account. This matches the Perl design but is a Django-specific workflow to document.
3. **Built-in protection is broader:** CSRF checking includes an Origin/Referer check on HTTPS POSTs (observed: a valid token alone was still refused until the matching Origin was sent), signed-cookie sessions avoid a session table, and the framework ships authentication and permission scaffolding. These reduce code to write and audit; they were observed, not exhaustively assessed.
4. **PyJWT does not require `exp` unless asked** (same class of issue as `Crypt::JWT`): pass `options={"require": [...]}`. It refuses to *sign* HS256 with a PEM public key, but verification still needs `algorithms` pinned.
5. **Live updates:** Channels' in-memory layer is single-process; a multi-process deployment needs a shared channel layer (typically Redis — an extra service, **UNVERIFIED** here). The Perl prototype's subscriber table is equally single-process, so **either stack needs a shared mechanism once more than one worker serves requests.** SSE worked under Daphne (ASGI); under synchronous workers it would hold a worker per stream (general knowledge, UNVERIFIED).
6. Everything database-related behaved the same as in Perl because it is the database, not the language, doing the enforcing. The language choice therefore mostly affects the application layer: web framework, session/CSRF tooling, token libraries, background jobs, maintainability and contributor pool.

## 6. Comparison of the application layer

| Aspect | Perl / Mojolicious | Python / Django |
|---|---|---|
| Database criteria 1–5 on MariaDB | PASS | PASS |
| Concurrency under test | PASS, 40/40 repeated runs | PASS, 55/55 (ORM) and 28/28 (raw driver) repeated runs |
| CSRF / sessions / cookie flags | Available; assembled by the team | Built in, more defaults |
| WebSocket push | PASS, built into the framework | PASS via the Channels add-on (extra dependencies) |
| SSE | Marked experimental in the framework | Supported through standard streaming responses under ASGI |
| Token library | Crypt::JWT: pinned algorithm, explicit `verify_exp` | PyJWT: pinned algorithms, explicit `require` |
| Migrations / schema ownership | Own tooling to choose | Built-in tool; needs a separate DDL account |
| Language preference | Owner's preference | Not preferred, negotiable |
| Contributor pool, ecosystem size | Smaller (not measured) | Larger (not measured; general knowledge) |
| Extra services for multi-worker push | Needed (not built) | Needed (Redis typical, not built) |

## 7. Not run: PostgreSQL

PostgreSQL was not tested. With the owner's leaning toward MySQL/MariaDB and MariaDB passing every criterion in both languages, nothing here argues for it; it remains a documented alternative only.

## 8. Recommendation

1. **Both language candidates are viable on MariaDB 10.11**, and Perl/Mojolicious is also viable on MySQL 8.0. The technical spike does not eliminate any of them; the remaining choice is about people and operations.
2. **Database: MariaDB 10.11 LTS** as the proposed default (single distribution package, fewer special cases in the spike; MySQL 8.0's upstream end-of-life is understood to be in 2026, **UNVERIFIED**).
3. **Language (owner decision):**
   - **Perl/Mojolicious** honors the stated preference and was fully demonstrated, but the team assembles more of the security and operations tooling, and the contributor pool is smaller.
   - **Python/Django** has more built-in security and workflow tooling and a larger pool, at the cost of the language preference and more dependencies for live updates.
   - The author's view: if volunteer maintainability and built-in protections are weighted above language preference, choose Python/Django; if the preference for Perl is firm and the owner or maintainers are comfortable with Perl, choose Perl/Mojolicious. Either way, the design rules in §3 apply unchanged.
4. **Use WebSocket** for live updates in either stack.
5. **Carry the §3 findings into the contracts issue ([issue #20](https://github.com/JustApu/CheckYourAmbulance-archive/issues/20)) and audit-log issue ([issue #23](https://github.com/JustApu/CheckYourAmbulance-archive/issues/23))**, plus the Python findings above if Django is chosen.
6. **Before M1 sign-in work starts**, settle Google's token claims from Google's own documentation and plan the real-Workspace verification in [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25).

## 9. Decision

**Recorded 2026-10-20:** the owner decided option **(C) Python/Django + MariaDB** ("I will accept Django & MariaDB for this project"). SPEC 1.1.0 carries the decision and the design rules adopted from this spike ([§17.5](../../SPEC.md#175-hosting-database-engine-backup-oq-24), §17.7). The options offered were **(A)** Perl/Mojolicious + MariaDB, **(B)** Perl/Mojolicious + MySQL, **(C)** Python/Django + MariaDB, **(D)** another stack or further evidence. Hosting, TLS, secrets and backups remain open ([issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16)), and §4's unverified items remain unverified.
