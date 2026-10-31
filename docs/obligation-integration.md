# Milestone M2 obligation-engine verification

[issue #61](https://github.com/JustApu/CheckYourAmbulance-archive/issues/61) binds `EX-m2-obligation-engine` in the offline scenario library.
Its two steps are registered in `cya/testing/scenarios.py`; the reusable driver
is `cya/testing/obligations.py`. The committing scenario executes the actual
MariaDB-backed commands from issues [issue #55](https://github.com/JustApu/CheckYourAmbulance-archive/issues/55), [issue #56](https://github.com/JustApu/CheckYourAmbulance-archive/issues/56), [issue #57](https://github.com/JustApu/CheckYourAmbulance-archive/issues/57), [issue #58](https://github.com/JustApu/CheckYourAmbulance-archive/issues/58), [issue #59](https://github.com/JustApu/CheckYourAmbulance-archive/issues/59), [issue #60](https://github.com/JustApu/CheckYourAmbulance-archive/issues/60), not a separate obligation simulator.

## Synthetic seven-day journey

The fixture `tests/fixtures/obligation-integration.json` is explicitly synthetic
and non-authoritative. It combines the existing four-ambulance fleet seed with
an invented glucometer item. No real stocking requirement or crew data is used.

The journey spans October 28 through November 3, 2026, in America/New_York:

1. Seed Vehicles 1 through 4, publish a synthetic checklist, configure the
   optional carry-over rule and seed six illustrative schedule rules. Bind the
   appropriate slots to each ambulance. A rejected edit proves a published
   definition is frozen by the database, not merely hidden by the UI.
2. Run generation throughout seven local days with the default 48-hour horizon.
   Every run is repeated; obligation, selection, odometer, receipt and audit
   counts must remain unchanged on the repeat.
3. Keep default fleet schedules unchanged and bind an **extra test-only 01:30
   schedule** to Ambulance 1. The November 1 fold must produce 05:30Z, never a
   duplicate 06:30Z. The ordinary 06:00 inspection changes from 10:00Z before
   the transition to 11:00Z after it. This complements the prerequisite schedule
   engine's spring-gap and fall-fold unit tests (TIME-3).
4. Publish a new template at 05:30 local on October 31, removing the invented
   warning-light check. Ambulance 1's open 06:00 inspection retains its original
   seven obligation IDs. Its future 18:00 inspection receives six replacement
   obligations, retires the removed item and uses the new requirement version.
5. Take Ambulance 2 out of service from November 1 at 04:30 local until November
   3 at 14:00. Pre-created inspections become `EXEMPT`, not Passed. An inspection
   beyond the prior generation horizon is never created during the outage;
   returning to service still does not backfill it. Future routine generation
   resumes, and the status event interval preserves both audited boundaries.
6. Evaluate the real carry-over engine with explicit local Humanity/prior-check
   stubs. Unchanged crew with a prior qualifying check creates a real `WAIVED`
   determination that the projection API reads. Changed crew, unavailable roster
   and missing prior check leave obligations required (`DUE`) with no waiver.
7. Capture T1 before publication, T2 after publication, and T3 during the outage
   with the waiver present. After all later events, each as-of API answer must
   equal its original immutable observation. Replay shuffled selection events
   from the final exported history and compare the same complete states and pins.

An independent wall-calendar oracle verifies every vehicle's due-instants set,
including Ambulance 2's weekday/weekend times. Final totals within the seven-day
interval are **21, 6, 7 and 7 effective inspections**, respectively, and **266
effective item obligations**. Each effective inspection has one odometer
obligation. The projection's entire ID set must equal the latest non-retired
selection per anchor: archived requirements cannot become phantom overdues.
Ordinary unsatisfied obligations really become overdue, so simply hiding all
old obligations cannot satisfy the assertion.

## Offline execution and limits

Run the scenario library with a disposable development database:

```sh
local/venv/bin/python manage.py test cya.tests.test_scenarios --noinput
```

The normal CI command `python manage.py test cya --noinput` discovers the bound
scenario automatically. It needs schema-owner mode for the explicit schedule
seed and real commits for audited commands. Do not run it against operational
data. `tests/README.md` documents scenario isolation and committing teardown.

The scenario installs its own Python network guard in addition to the offline
test runner, uses only local stubs, and verifies that an external synthetic DNS
lookup is refused before resolution. Clock and guard cleanup runs even when a
step fails. No request reaches Google or Humanity. The existing guard's documented
C-extension limitation still applies; MariaDB is the isolated test database.

This is test support, not a production seed command or deployment gate. It writes
synthetic fixture rows, audited commands and immutable events only in the test
database. Failures raise assertions with the violated invariant; generation-unit
failures are never ignored. Repeat generation is idempotent; rerunning the whole
scenario requires its normal fresh test database. There is no `--dry-run` flag.

The scenario does not exercise check/link storage, inspection sessions, real
provider integration, backup restore or the remaining symbolic AC scenarios.
The linked early-check example is an explicit check stub. Existing prerequisite
suites supply broader authorization, concurrency, malformed-input, roster
conflict, randomized AC-26 and DST spring-gap coverage. Tracking [issue #9](https://github.com/JustApu/CheckYourAmbulance-archive/issues/9) records
the current verification head and independent-review/merge status; milestone
closure still requires the repository's normal owner/review procedure.
