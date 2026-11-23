# Inspection sessions: [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)

Sessions organize work on a vehicle; their state never satisfies an item obligation
(SPEC §6.1). The current implementation provides server-side Python commands and
database history. It does not expose an HTTP route yet.

## Commands and state

`cya.inspections.commands.open_or_join_session` takes authenticated command
context, a vehicle ID and a checklist template ID. **One session in progress per vehicle
checklist** (SPEC 6.0.0 SES-7, [issue #256](https://github.com/JustApu/CheckYourAmbulance/issues/256)
Part D): a vehicle checklist is the pair (vehicle, checklist template), its identity rather than
a version, and a session is in progress while it is Open and before its end instant: neither idle
for six hours (SES-1) nor at its resume deadline (SES-8, SES-9; see
[the resume limit](#resume-limit-and-end-instant-issue-256-part-e) below). Starting a checklist that has a session in progress joins that
session, even when it pins an earlier version (CHK-4). Sessions of different checklists on one
vehicle run together. All four roles may open or join. `separate=True` is still accepted, and is
refused with `SESSION_IN_PROGRESS` while that checklist has a session in progress; with none in
progress it opens one like any start. If no session is joined, opening resolves the effective
published checklist for the vehicle at the server-assigned open instant and pins its template
version. Joining never changes that pin or the six-hour activity clock. Repeating a join by an
existing participant adds no participant or session event.

`end_session` lets a participant explicitly mark an Open session Closed or
Incomplete; either state is informational. `reopen_session` lets a participant
resume either ended state without approval, but only before its resume deadline (SES-8; refused
with `SESSION_DEADLINE_PASSED`) and while no other session of the same vehicle checklist is in
progress (SES-3, SES-7; refused with `SESSION_IN_PROGRESS`). A refusal changes nothing and is
audited as a failed command. A nonparticipant joins the session in
progress for that checklist, or starts a new one when none is in progress. Reopening retains
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
simultaneous default open joins the session created by the winner, and a start racing a reopen
of the same checklist leaves exactly one session in progress.

Database backstop (migration `cya_inspections` 0006): the `session_event` insert trigger
refuses an OPEN or REOPEN event while another session of the same vehicle and checklist
template has OPEN, JOIN or REOPEN as its latest event and the event's recorded instant is before
that session's end instant (migration 0007: `LEAST(A + 6 h, GREATEST(R, A))`; 0006 used
`A + 6 h`), where A is the activity the auto-close guard uses: OPEN and REOPEN events and checks
in the session. Before checking, the trigger takes the session's
vehicle row lock (`SELECT ... FOR UPDATE`), the same lock open, join, reopen, end, auto-close and
a check in a session all take first. At READ COMMITTED each read then sees what the previous lock
holder committed, so two concurrent OPEN or REOPEN inserts that skip the commands serialize (or
one is rolled back as a deadlock victim) and at most one commits.
Sessions that were in progress together before 6.0.0 are left as they are. Reversing 0006
restores the 0005 trigger.

The session screen lists the rows of the version the session pins (CHK-4, SPEC 5.7.1), even after
a newer version is published: `GET /api/v1/vehicles/{id}/checklist?session_id=` (participants
only; anyone else gets 404) resolves the session's checklist as a check recorded in it is
resolved, while each row's obligation, attention, latest check, deficiencies and kits are the
vehicle's current state. An item the newer version removes is still listed and checkable in the
session, and an item it adds is not.

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
`manage.py close_inactive_sessions [--limit N] [--dry-run]`, intended for a scheduler; it prints
`closed= at_deadline= skipped= failed=` and exits 1 if any close failed; `--dry-run` prints one
`due session= ends_at=` line per session due and `due=N`, and writes nothing) ends every Open
session at or past its end instant (Part E, below), earliest first, at most `--limit` (default
200) per run. Candidates come from the disposable projection's `ends_at`; each close then runs `auto_close_session` as a service-identity command
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
  join it, it does not count as in progress for SES-7, and `reopen_session` and `end_session` first run the same close on it, so a caller
  never waits for the scheduler.
- The close is informational. Nothing here changes an item obligation (AC-2): an Incomplete
  session leaves every obligation as it was; a check is what satisfies an obligation.

`session_event.actor_user` is null only for the job's `AUTO_INCOMPLETE` and
`DEADLINE_INCOMPLETE` (a CHECK ties them together), and the receipt must be a service receipt of
that command type. Reversing migration `cya_inspections` 0004 stops if an `AUTO_INCOMPLETE` event
exists, and reversing 0007 stops if a `DEADLINE_INCOMPLETE` event exists; a recorded close is
never discarded.

## Resume limit and end instant ([issue #256](https://github.com/JustApu/CheckYourAmbulance/issues/256) Part E)

**The setting (SES-8).** `session_resume_limit` (append-only) holds an organization's revisions
of its resume limit, whole hours from 1 to 24. The latest revision applies, and without one the
limit is 6 hours. An Administrator sets it on **Administration → Inspection sessions**
(`/administration/inspection-sessions/`) through
`cya.inspections.resume_limit.set_resume_limit(hours)`, command `SESSION_RESUME_LIMIT_SET`,
policy action `set_inspection_resume_limit` (Administrator only). Refusals: `MALFORMED_REQUEST`
(not a whole number from 1 to 24), `NO_CHANGE` (already the limit), denied (audited). There is no
JSON endpoint.

**Capture.** Opening a session copies the current limit into
`inspection_session.resume_limit_hours`, an immutable header fact, so a later change never moves
that session's deadline. Joins, activity and reopens never change it. To keep the value an open
reads current, the open command takes the organization row in share mode first (before the
actor's and the vehicle's locks: `locked_identities(..., share_organization=True)`), and the
setting command takes it exclusively. Opens therefore run concurrently with one another, and a
change of the limit waits for the opens in flight, and they for it. The `inspection_session`
insert trigger repeats the rule: the captured value must equal the organization's latest revision
(or 6).

**Definitions (SES-9)**, the same in Python (`cya.inspections.projections.fold_session`) and in
the triggers:

- `A`, the last persisted activity: the later of the latest OPEN or REOPEN event (the events that
  carry an activity receipt; JOIN and END never do) and the latest check recorded in the session.
  This is the definition 0005's auto-close and 0006's in-progress guards already used.
- `R = opened_at + resume_limit_hours`, the resume deadline; `R* = max(R, A)`, the effective
  deadline. For a session opened under 6.0.0 nothing is accepted at or after `R*`, so `A < R`
  and `R* = R`. A session from before 6.0.0 may hold a check after `R`; that history stays valid
  and `R*` is its instant.
- `E = min(A + 6 h, R*)`, the end instant. An Open session is in progress only before `E`
  (`is_expired`); `SessionState` carries `resume_deadline_at` (`R*`) and `ends_at` (`E`), and
  `session_projection.ends_at` caches `E` for the job.

**Enforcement.** Nothing waits for the job.

- A check or correction into the session at or after `R*` is refused with 409
  `SESSION_DEADLINE_PASSED` (before `R*`, an ended session still answers `SESSION_NOT_OPEN`:
  reopen it). `reopen_session` at or after `R*` is refused with `SESSION_DEADLINE_PASSED`. A start
  of the checklist never joins a session past `E`, so after the deadline it opens a new session
  pinned to the current version (CHK-4), which shows the earlier checks through current
  obligations (SES-4, SES-5).
- A reopen starts a new six-hour window that ends no later than `R*` (SES-2). With a limit of 6
  hours or less an Open session always reaches `R` no later than its inactivity limit, so only a
  session ended by hand can be reopened; with a longer limit an idle-closed session can be
  reopened until `R*`.
- The job appends the ending at exactly `E`: `AUTO_INCOMPLETE` when `A + 6 h <= R*` (SES-6),
  otherwise `DEADLINE_INCOMPLETE` at `R*`, even if the session was in active use. Its response
  carries `reason` `INACTIVITY` or `DEADLINE`.

**Database guards** (`cya_inspections` 0007 and `cya_checks` 0019, each a rewrite of the previous
trigger text by exact replacement with its anchors asserted once): the `session_event` insert
trigger computes `A` and `R*` and refuses an `AUTO_INCOMPLETE` not at `A + 6 h` or after `R*`, a
`DEADLINE_INCOMPLETE` not at `R*` or when `A + 6 h` comes first, and a JOIN or REOPEN recorded at
or after `R*`; the `check_record` insert trigger refuses a check into a session at or after `R*`.

**Migration (SPEC 18.3 item 3).** Every existing session gets a captured limit of 6 hours, and
every projection its `ends_at`. Sessions whose end instant has passed read as ended from it, and
the job records their ending at that instant; several sessions of one checklist already open on
a vehicle finish under SES-1, SES-8 and SES-9 without being ended early.

**Pages.** The session page shows "This inspection ends by" its deadline while open, and Reopen
"until" it once ended. From `R*` it is read-only for everyone: a notice that the inspection
reached its time limit, that its records are kept and a link to the vehicle page; no Reopen and
no record controls. The vehicle page offers Resume only before `R*`, with "can be resumed until".

Participation rule for checks (an assumption this issue makes; the SPEC does not state it): only
a recorded participant may record into a session, so a nonparticipant cannot keep someone
else's session open. A nonparticipant joins first (all four roles may), then records.

Run offline tests against a disposable MariaDB (10.11 or 11.8) test database with
`python manage.py test cya.inspections --noinput`; the full CI suite includes
them. Fixtures use synthetic people, vehicles and checklists. No external
provider or network request is made by these tests.
