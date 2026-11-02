# Milestone M4 deficiency resolution and readiness verification

[issue #18](https://github.com/JustApu/CheckYourAmbulance/issues/18) binds `EX-m4-deficiency-resolution` in `cya/testing/scenarios.py`.
The driver is `cya/testing/resolution.py`. It reuses the M3 synthetic agency
(`cya/testing/inspections.py`, [M3 verification](inspection-integration.md)) with its own fixture,
`tests/fixtures/resolution-integration.json`. It uses real audited commands, MariaDB transactions
and database guards in a disposable committing test database. Only the clock and the email sender
are injected. The checklist, users and schedules are synthetic and non-authoritative.

## Assertions and specification coverage

| Journey | Executable evidence |
|---|---|
| Lifecycle, DEF-4/DEF-5, §8.5 | A Failed check reports a deficiency, which is then: assigned (the assignee is notified); given a deadline; started; put in Awaiting External Dependency, which the dashboard shows with its follow-up; returned to work; and given an extension, requested by the assignee (both supervisors notified) and decided with a different date (the requester notified). |
| AC-16: verification | The repair materializes the item scope. A routine Pass on the item leaves it Pending Verification. The verification Pass closes it, recording one `VERIFICATION_CLOSED`. A compartment-wide deficiency's last two scope rows pass at the same moment from two threads: both saved, closed exactly once. |
| AC-14/17, INV-4/INV-7 | Invalidating the closing verification check reopens the deficiency to Open, notifying the assignee and both supervisors; it is repaired and verified again. Added verification after closure reopens it with the new deadline and notifies only the current assignee. A supervisor's administrator closure is refused; an administrator closes and reopens it. Invalidating the original Failed check never closes it (INV-3). |
| AC-18, SUS-1 to SUS-4 | Three items are suspended for three deficiencies. Today's obligation keeps the Failed check recorded before the suspension (the owner decision on PR #29); later ones are Suspended, never Satisfied, and the dashboard counts them. The deficiencies then close by verification, administrator closure and Reported in Error. Each closing event ends its suspension, and none is left in force. |
| AC-19/20, OPS-2 | A Critical Failed check, an overdue item and the overdue-notice job change no operational status. While out of service, obligations due in that time are Exempt, never Passed. |
| RPT-7, AC-28 | The dashboard counts Passed, Failed, Exempt, Invalidated and Superseded separately, with no single compliant flag. Every email intent is sent once through the stub sender to its recipient's address, and external DNS is blocked. |
| AC-26/27 | T1 (after the extension), T2 (after the concurrent closure) and T3 (after the suspensions, Critical failure and overdue item) give identical obligations, checklist, dashboard and deficiency states after every later event. A shuffled event history replays to the same obligations without database reads. |

## Running and limits

With the documented disposable development database settings, run:

```sh
local/venv/bin/python manage.py test cya.tests.test_scenarios --noinput
```

`-k m4` runs only this scenario. The full `manage.py test cya` suite discovers it automatically.
It needs schema-owner mode and real commits; its test teardown truncates synthetic rows.

The scenario is not a production command, has no `--dry-run`, and is not itself idempotent within
an occupied database. Individual retry behavior is covered by the component suites. Assertion or
command failures fail the test, and thread waits have explicit time bounds. The runner restores the
clock and the network guard even on failure.

It does not exercise a real browser, contact any external provider, or bind the symbolic AC files,
which keep their status. The rule-based RPT-7 states (Not Applicable, Carried over, other waivers,
supervisor-completed Unable, Completed late) are covered by the dashboard's component tests
(`cya/attention/tests/test_readiness.py`), not by this journey. Milestone
[issue #1](https://github.com/JustApu/CheckYourAmbulance/issues/1) records verification and review
status; passing this scenario alone does not authorize milestone closure or merge.
