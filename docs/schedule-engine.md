# Schedule rules and UTC windows

[issue #56](https://github.com/JustApu/CheckYourAmbulance-archive/issues/56) implements [SPEC §5.2–5.3 and §5.8](../SPEC.md#5-inspection-obligations-windows-and-scheduling)
and the `schedule_rule` portion of [contracts §2](data-model-contracts.md#2-table-catalogue).
It does not create obligations, run scheduled jobs, attach requirements to
vehicles/items, or implement checklist publication or conditional omission.
Those consumers must pin a schedule revision effective at their due instant; the generator and the binding of
vehicles to slots are described in [obligation generation](obligation-generation.md).

## Calculation interface

`cya.schedules.engine.Rule` validates station IANA zone, naive `wall_time`,
aware `effective_at`, recurrence and nonnegative integer window seconds.
`due_instants(rule, start, end)` streams distinct, ordered UTC instants in an
inclusive horizon, excluding due times before `effective_at`.
`windows(rule, start, end)` returns `Window(due_at, early_at, grace_at)` objects.
All window arithmetic is UTC: E = D − early_seconds; G = D + grace_seconds.
Zero and asymmetric windows are supported; default edges are 3600 seconds.

DAILY selects each station date; WEEKDAY selects Monday–Friday and WEEKEND
Saturday–Sunday, without holiday exceptions. The typed INTERVAL representation
is every `interval_days` station-calendar days on/after `anchor_date`, at the
configured wall time. It preserves local cadence across DST; it is not an
elapsed-hours timer. This is an internal representation choice; no other
interval unit or free-form recurrence expression is accepted. Integer windows
and intervals are bounded to 0–2147483647 and 1–2147483647 respectively.

The resolver uses Python `zoneinfo` and UTC round trips. An ambiguous hour uses
the first occurrence. Imaginary times resolve to the exact gap boundary:
New York 2026-03-08 02:30 becomes 03:00, not 03:30. Non-hour transitions and a
skipped civil date are also tested. A binary search of the two candidate UTC
instants bounds gap resolution; date iteration bounds recurrence generation.
The Linux host must provide IANA timezone data; missing zones fail closed.
Datetime operations outside Python's representable range fail rather than wrap.

TIME-3's “59–61 minutes' wall time” phrase does not define clock-face
subtraction: for a one-hour spring jump, 01:30→03:30 is exactly 3600 elapsed
seconds. At fall-back, 01:30 first→01:30 second is also 3600 seconds. Tests
assert UTC instants and offsets/folds explicitly, preserving TIME-3's absolute
arithmetic and AC-4's resolution rules without imposing a false local duration.

## Persistence and administration

`schedule_rule` stores immutable revisions per `(organization, slot)` with
increasing revision/effective instants. Slots identify independent schedule
series, including the six initial fleet roles; they are not unvalidated vehicle
foreign keys. Future requirement versions reference actual schedule row IDs.
Every revision references its receipt, actor, roles and server-recorded time;
composite foreign keys prevent cross-organization receipt/actor references.
MariaDB triggers forbid UPDATE/DELETE even for the schema owner and enforce
revision ordering, receipt attribution and the station zone on insert.
The runtime account gets SELECT/INSERT only, via `audit_runtime_grants` after
migration; the schema verifier checks immutable schedule guards too.

Administrator edits append through `publish_rule`, using existing current-role
checks, organization/user locks, atomic receipt/audit writes and operation-ID
replay. A nonadministrator or inactive administrator is denied. Different
intent under the same UUID returns the original result with `operation_reused`;
CLI transports treat it as a conflict. Failed business effects roll back; the
existing audit infrastructure records failure independently. No network effects
occur. No automatic retry of an uncertain write is added.

The operator can transport an administrator's revision without editing code:

```sh
python manage.py publish_schedule --organization <organization-uuid> --actor <administrator-uuid> --slot first-due-0600 --zone UTC --wall-time 06:30 --recurrence DAILY --early-seconds 1800 --grace-seconds 3600 --effective-at 2027-01-01T00:00:00Z --operation-id <new-v4-uuid>
```

This is a privileged operator CLI, not an HTTP endpoint or a sign-in mechanism.
The actor must already be an active organization administrator. There is no
`--dry-run` for publication; use `Rule`/the pure engine to inspect calculations.
Reusing the operation UUID resolves replay after an uncertain outcome. A new
revision must have a later effective instant than that series' last revision;
historical rows remain available. No existing obligation is rewritten.

## Illustrative schedule seed

Migrations create schema only. Explicitly select the organization's UUID and
seed during operator setup with schema-owner mode:

```sh
python manage.py seed_default_schedules --organization <organization-uuid> --effective-at 2026-10-22T00:00:00Z --dry-run
python manage.py seed_default_schedules --organization <organization-uuid> --effective-at 2026-10-22T00:00:00Z
```

The configured organization's station zone is used; set it to
the time zone selected for that organization when it is created. The command inserts the six
SPEC §5.2 rows: Ambulance 1 at 06:00/18:00 daily; Ambulance 2 at 09:00 weekdays/12:00
weekends; Ambulance 3 and Ambulance 4 at 18:00 daily, all initially ±3600 seconds. These are configurable demonstration values, not fixed recurrence-engine behavior. Deployments should set their own schedule rules. The command never creates or renames an organization or vehicle.

The seed uses one deterministic service operation ID, one atomic command and
one success audit. Repeat with identical parameters changes nothing; different
parameters conflict rather than reseed. Existing initial slots without that
receipt refuse seeding. Preview validates organization/time/zone and writes
nothing, including no audit rows; it does not reserve slots. Partial failure
rolls back all six rows and the receipt; inspect failures and rerun with the
same inputs. After seeding, use administrator revisions to change configuration.

## Verification

Offline tests in `cya/schedules/tests/` cover leap days, normal/transition dates,
fold/gap boundaries, closed horizons, effective instants, weekday/weekend and
interval cadence, zero/asymmetric windows, malformed input, seed/replay,
permission denials, rollback, immutable/tenant guards and concurrent revisions.
With `CYA_DB_SECURITY_TESTS=1`, disposable runtime-account tests prove actual
SELECT/INSERT publication and refusal of UPDATE/DELETE/DDL, not mocked grants.
Run `python manage.py test cya.schedules --noinput` in the documented
[development environment](../CONTRIBUTING.md#python-and-django).

Implementation references: [Python 3.13 zoneinfo](https://docs.python.org/3.13/library/zoneinfo.html)
and [PEP 495](https://peps.python.org/pep-0495/) explain folds, imaginary local
times and UTC conversions. The resolver supplies the SPEC's explicit gap policy.
