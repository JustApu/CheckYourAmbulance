# Milestone M3 inspection and deficiency verification

[issue #88](https://github.com/JustApu/CheckYourAmbulance-archive/issues/88) binds `EX-m3-inspection-deficiency` in `cya/testing/scenarios.py`.
The driver is `cya/testing/inspections.py`; its fixture is
`tests/fixtures/inspection-integration.json`. It uses real audited commands,
MariaDB transactions and database guards in a disposable committing test database.
The invented checklist, users and overlapping UTC schedules are synthetic and
non-authoritative.

## Assertions and specification coverage

| Journey | Executable evidence |
|---|---|
| AC-1/2: individual compliance | Three crew write different items concurrently, each check retains its author, and checked obligations are satisfied while the session is open. Explicit close/reopen changes no compliance and leaves an unchecked item unsatisfied. A separate session remains open without submitting completion. |
| AC-8/31: retries and warnings | A lost-response replay after a newer independent check returns the stored response without a duplicate. Repeated warnings consume no operation receipt; an intervening check makes an old acknowledgement warn again; a new decision proceeds. |
| AC-9/10: duplicate and Pass/Fail race | Barrier-released writers race on one item. Exactly one is warned, naming the winning check and author. Acknowledgement retains both independent outcomes. A later Pass leaves the unresolved deficiency red. |
| AC-12/13: correction | A supervisor cannot correct another author, but can record an independent Pass. Author Correct entry preserves the original and reports the accidental failure in error. Another effective failure keeps its shared deficiency open. |
| AC-14/15/32: invalidation | Crew invalidation and missing explanations are refused. Invalidating a failed check leaves the deficiency open; invalidating the corrected chain reopens its Reported-in-Error deficiency and refuses further correction. |
| AC-29: Unable to Inspect | Missing reason is refused. Unable leaves the obligation unsatisfied, Crew completion is denied, and supervisor completion displays the distinct label while the deficiency remains open. |
| AC-21: odometer | Each scheduled inspection has one shared odometer obligation, satisfiable by another crew member. A lower reading writes nothing until explicitly confirmed. Engine hours are omitted. |
| AC-30: link cardinality | Overlapping eligible windows select the earliest due only. The database rejects a second unruled link. Invalidation leaves neither obligation satisfied. |
| AC-36/TIME-1a: kits | Checking one identical kit leaves the second unsatisfied. A zero-kit control vehicle has no kit obligation. Adding at 06:00 skips both already-open windows (06:00 and 06:30), and owes the next day's inspections. Removing a kit preserves its arrived obligation and retires future ones. |
| AC-6: inactivity | Passive reading does not refresh activity. Auto-close stamps last activity plus six hours, preserves checks and compliance, and a participant reopens with audit history and the original checklist version. |
| AC-26/27: history | T1 (initial crew checks), T2 (corrections), T3 (kit changes/invalidation) retain identical complete obligation, checklist/attention and odometer answers after later events. Shuffled final event history reconstructs the same obligation/attention states without database reads. |
| AC-28/live delivery | A real committed check delivers an identifier-only update through an in-memory channel subscription. The scenario installs its own network guard alongside the runner's and proves synthetic external DNS is blocked. |

## Running and limits

With the documented disposable development database settings, run:

```sh
local/venv/bin/python manage.py test cya.tests.test_scenarios --noinput
```

The full `manage.py test cya` suite discovers the scenario automatically. A separate
parallel/`--keepdb` migration-restoration failure ([issue #145](https://github.com/JustApu/CheckYourAmbulance-archive/issues/145))
was fixed in [PR #165](https://github.com/JustApu/CheckYourAmbulance-archive/pull/165); it was not a scenario-discovery limitation.
Fresh and kept worker clones are supported; see the [check-reference reversal and disposable-schema recovery guidance](check-records.md#reversing-reference-migrations-on-cloned-test-databases).

The scenario needs schema-owner mode and real commits; its test teardown truncates synthetic rows.
The scenario is not a production command, has no `--dry-run`, and is not itself
idempotent within an occupied database. Individual retry behavior is asserted
through operation IDs. Assertion or command failures fail the test; thread waits
and live receives have explicit time bounds. The runner restores the clock and
network guard even on failure, and worker connections/channel subscriptions close.

This journey does not bind unrelated symbolic AC files, exercise a real browser,
contact Google or Humanity, or implement M4 repair/verification/explicit closure.
Odometer recording uses the supported vehicle-level command: its deferred
`session_id` activity wiring is outside this integration issue.
Existing component and browser-script suites cover additional malformed inputs,
authorization boundaries, UI acknowledgement states and provider stubs. The Python
network guard's documented C-extension limitation permits the isolated MariaDB
connection. Milestone [issue #10](https://github.com/JustApu/CheckYourAmbulance-archive/issues/10) records verification and review status; passing
this scenario alone does not authorize milestone closure or merge.
