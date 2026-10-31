# Inspection sessions: [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)

Sessions organize work on a vehicle; their state never satisfies an item obligation
(SPEC §6.1). The current implementation provides server-side Python commands and
database history. It does not expose an HTTP route yet.

## Commands and state

`cya.inspections.commands.open_or_join_session` takes authenticated command
context, a vehicle ID and a checklist template ID. By default it joins the
latest Open session for that vehicle, regardless of the requested template.
All four roles may open or join. `separate=True` starts a new session even when another is
Open, Closed, or Incomplete. If no Open
session is joined, opening resolves the effective published checklist for the
vehicle at the server-assigned open instant and pins its template version.
Joining never changes that pin or the six-hour activity clock. Repeating a join
by an existing participant adds no participant or session event.

`end_session` lets a participant explicitly mark an Open session Closed or
Incomplete; either state is informational. `reopen_session` lets a participant
resume either ended state without approval. A nonparticipant starts a new session
instead, even if the prior session is Open. Reopening retains
the original pin and appends an
audited REOPEN event. Its result requests a requirements refresh. An authorized
reader can call `cya.inspections.projections.read_session` to get a fresh
vehicle obligation projection alongside the original session version; the
caller supplies the normal check and suspension evidence adapters when those
features are available. The command layer does not infer satisfaction from
the session's Closed state.

The owner-approved SPEC 1.8.0 amendment defines role access. Opening and joining
are allowed to all four roles. Reopening and
manual end additionally require participation in that session. Current roles are
loaded under identity locks for each command; denial is audited after rollback.
The actor, timestamp and role evidence come from the server. No request may
provide them as session facts.

## Storage and recovery

`inspection_session` holds immutable creation facts: organization, vehicle,
published checklist version, actor and open instant. `session_participant` and
`session_event` are append-only, receipt-linked records with same-organization
foreign keys and MariaDB guards. Events have a gapless per-session sequence and
one predecessor. The separate `session_projection` caches current state,
last activity and last applied event; it can be rebuilt from the event stream.
Opening and reopening count as activity, as does a check recorded in the session (Part B,
below). Joining and ending do not advance the timer. Vehicle-row locking serializes concurrent session commands; a
simultaneous default open joins the session created by the winner.

The commands read synthetic or configured checklist publications, vehicle and
current role records; they write a receipt, success or failure audit, and only
the relevant session rows. A failed business precondition or denied command
rolls back business writes and records a separate mandatory audit; malformed
arguments rejected before command entry do not reach that audit boundary.
Operation-ID replay
returns the original result without another event. There is no CLI command
or `--dry-run` flag for these mutations.

## Persisted activity and the six-hour auto-close ([issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) Part B)

Activity (SPEC 6.3 SES-2) is a persisted business event: an OPEN or REOPEN event, or a check
committed with this `session_id` (`record_check` accepts it; see
[check records](check-records.md)). Corrections, odometer entries, deficiency reports and
roster confirmations join the list as those issues land. Viewing, heartbeats, a repeated join,
a manual end and any failed or unacknowledged save do not count, because none of them writes
an activity row. The session's last activity is the later of its latest OPEN/REOPEN event and
its latest check; `session_projection` caches it and is rebuilt after every activity.

`cya.inspections.commands.auto_close_inactive_sessions` (also
`manage.py close_inactive_sessions [--limit N]`, intended for a scheduler; it prints
`closed= skipped= failed=` and exits 1 if any close failed) closes every Open session idle for
six hours, oldest first, at most `--limit` (default 200) per run. Candidates come from the
disposable projection; each close then runs `auto_close_session` as a service-identity command
(`worker.session_auto_close`, command type `SESSION_AUTO_CLOSE`). Under the vehicle row lock
it re-reads the authoritative history and the session's checks, and appends an
`AUTO_INCOMPLETE` event stamped **last activity + 6 hours**, not the instant the job ran, so a
late job leaves a correct record (SES-6). The database insists on that instant: the trigger
recomputes it from the events and checks. Consequences:

- Idempotent: a run with nothing due does nothing; a close is keyed on the session's latest
  event, so replay returns the original result and a close after a reopen is a new one.
  A session whose activity moved since the job chose it is refused (rolled back, no receipt) and
  counted as skipped; a stale projection can only delay a close.
- A late job cannot erase work. A check recorded before the close is activity, so the session is
  not idle. A check offered after six idle hours is refused with `SESSION_NOT_OPEN` even if the
  job has not run, and the insert trigger refuses it as well; the crew member reopens the
  session (a new six-hour window from the reopen instant) and records again. Both paths hold
  the vehicle lock, so a check and a close racing each other resolve to exactly one winner.
- An idle session that the job has not closed yet is treated as closed: a default open does not
  join it, and `reopen_session` and `end_session` first run the same close on it, so a caller
  never waits for the scheduler.
- The close is informational. Nothing here changes an item obligation (AC-2): an Incomplete
  session leaves every obligation as it was; a check is what satisfies an obligation.

`session_event.actor_user` is null only for `AUTO_INCOMPLETE` (a CHECK ties them together), and
the receipt must be a service receipt of that command type. Reversing migration
`cya_inspections` 0004 stops if such an event exists; a recorded close is never discarded.

Participation rule for checks (an assumption this issue makes; the SPEC does not state it): only
a recorded participant may record into a session, so a nonparticipant cannot keep someone
else's session open. A nonparticipant joins first (all four roles may), then records.

Run offline tests against a disposable MariaDB 10.11 test database with
`python manage.py test cya.inspections --noinput`; the full CI suite includes
them. Fixtures use synthetic people, vehicles and checklists. No external
provider or network request is made by these tests.
