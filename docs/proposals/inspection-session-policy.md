# Proposal: one inspection in progress per vehicle, and a resume deadline

**Status:** Draft proposal for owner and Codex review. It has no authority. It changes no behaviour, and it does not authorize implementation or deployment.
**Author:** Claude, 2026-11-21.
**Proposed SPEC change:** amendment 6.0.0. It is major under §25.4(b), because it reverses approved session rules (§6.2 concurrent sessions, SES-3 "a nonparticipant starts a new session", and the 1.9.0 decision on [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)).

## 1. Policy direction to confirm

Codex relayed the following on 2026-11-21 as the owner's decision, after the owner compared screenshots of Medic 1. The owner should confirm it in writing before anything here is treated as approved:

1. An incomplete inspection session cannot be resumed indefinitely.
2. Only one inspection session can be in progress per truck. Crew join that session; they cannot open a parallel or separate one.
3. Historical checks and session records are always kept.

No duration was given. Section 4 recommends one.

## 2. How sessions work today (SPEC 5.0.0, main `cb45f27`)

### 2.1 Rules in SPEC

- **§3.3:** all four roles may open or join a session. Only a recorded participant may reopen an ended session or end an open one.
- **§6.2:** the states are Open, Incomplete and Closed. "Multiple sessions on the same vehicle may be open concurrently [C]". Starting a separate session "is allowed even when another session is open". A nonparticipant may start a new session instead of reopening.
- **§6.3 SES-1, SES-2:** after 6 hours with no persisted activity, a session closes as Incomplete. Reopening starts a new 6-hour window.
- **SES-3:** a participant may reopen a Closed or Incomplete session at any time, without approval. There is no age limit.
- **SES-4, SES-5, PRN-1, AC-2:** sessions never decide compliance. Item checks satisfy obligations, and another crew member can resume work without repeating valid checks.
- **SES-6:** the auto-close is stamped at last activity plus 6 h, not at the time the job runs.
- **CHK-4:** a session keeps the checklist version it opened with, including after a reopen.

### 2.2 Code

| Action | Where | What happens |
|---|---|---|
| Start or join | `POST /api/v1/inspection-sessions` → `open_or_join_session` ([cya/inspections/commands.py:162](../../cya/inspections/commands.py)) | Takes the vehicle row lock. With `separate: false` (the default), it joins the newest session on the vehicle that is Open and not idle, **whatever checklist was requested**. A repeat join is a no-op. JOIN adds a participant and a JOIN event, and does not reset the activity clock. If nothing is open, it opens a new session pinned to the checklist version resolved at that moment. |
| Separate | same, `separate: true` | Always opens another session, even while one is open. |
| Activity | `fold_session` ([projections.py:30](../../cya/inspections/projections.py)) | OPEN, REOPEN and checks recorded in the session count as activity. JOIN, END, viewing and failed saves do not. |
| Idle | `is_expired` (projections.py:74); `_settle_idle` (commands.py:461) | At last activity plus 6 h, an Open session is treated as ended at once, when it is read and when anything is written to it. A default join skips it. Reopen and end write the close first. |
| Auto-close job | `close_inactive_sessions` → `auto_close_session` (commands.py:388); `deploy/rocky9/cya-sessions.timer` every 30 min, `Persistent=true` | A service-identity command appends `AUTO_INCOMPLETE` stamped at last activity plus 6 h. It is idempotent, keyed on the latest event. A projection that is out of date can only delay a close. |
| End | `POST …/{id}/end` → `end_session` (commands.py:342) | A participant appends `END_CLOSED` or `END_INCOMPLETE`. |
| Reopen | `POST …/{id}/reopen` → `reopen_session` (commands.py:283) | A participant appends REOPEN, with **no age limit and no check for other open sessions**. It notifies the other participants (NOT-3). The original version pin is kept. |
| Checks in a session | `_session_for_check` ([cya/checks/commands.py:502](../../cya/checks/commands.py)); trigger in `cya/checks/migrations/0003_check_session.py` | The session must be on this vehicle and pinned version, the user must be a participant, and the session must be Open and not idle. The database repeats these checks. |
| DB guards | `cya/inspections/migrations/0003`, `0005` | Header, participant and event rows are append-only. The event chain is gapless. `AUTO_INCOMPLETE` has no actor and its instant must equal last activity plus 6 h. |
| Vehicle page | `vehicle` view and `_session_rows` ([views.py:129–193](../../cya/inspections/views.py)); `vehicle.html`; `vehicle.js` | Shows the newest Open session with Join or Continue. A "Start a separate inspection" disclosure asks for confirmation and then sends `separate: true`. The participant's newest Incomplete session is the primary **Resume** button, however old it is. Up to 20 recent sessions are listed. |
| Session page | `session` view (views.py:275); `session.html`; `checklist.js` | An ended session shows "Reopen session". `checklist.js` maps `SESSION_NOT_OPEN` to "Reopen it to continue". |
| Home | `in_progress` ([cya/shell/home.py:58](../../cya/shell/home.py)) | Lists the user's Open sessions that are not idle. |

API refusals for sessions all map to `409 SESSION_UNAVAILABLE` (api.py:57).

### 2.3 What this allows

- Two or more sessions open on one vehicle at once, each with its own participants. On a vehicle that has several checklists, they can be pinned to different checklists.
- A session from days or weeks ago can be reopened and recorded into. It still uses its original checklist version, which may have been superseded since, and each reopen gives it another 6 hours. A participant's oldest unfinished session stays the primary Resume button indefinitely.

Compliance is still correct in both cases, because checks satisfy obligations and sessions do not. The problems are confusion for crew, split participation, and recording against an outdated checklist version.

The proposal relies on a fact the code already guarantees: **starting a new session loses nothing.** Obligation status is per vehicle (the checklist API is per vehicle). Checks recorded in an earlier session keep satisfying current obligations under SES-4, so a new session shows them as already done.

## 3. Proposed behaviour in short

- **In progress** means the session is Open, has not gone idle (SES-1) and has not reached its resume deadline.
- A vehicle has at most one session in progress. Starting an inspection while one is in progress always joins it. No one can start a separate one, and no one can reopen another session on that vehicle while one is in progress.
- Every session has a **resume deadline** of 12 hours after it opened. The deadline is fixed when the session opens. When it passes, the session ends if it was still Open. It can no longer be reopened, joined or recorded into, and it stays readable permanently.
- Nothing is deleted or rewritten. Every ending is a new append-only event.

## 4. Recommended resume window: 12 hours from the session's open instant

**When it starts:** at the session's `opened_at`, the server instant of its OPEN event. Joins, checks and reopens never move it.

**Why from the open instant:** under SES-2 a reopen counts as activity. A window measured from the last check, the last activity or the last reopen would therefore restart with every reopen, and the session could still be resumed indefinitely. Only a fixed anchor gives a real limit.

**Why 12 hours:**

- A routine check takes well under an hour, and calls interrupt it. SES-1 already covers gaps of up to 6 hours, so with a 12-hour deadline a session that went idle during a long call can still be reopened for at least 6 more hours.
- 12 hours covers a day shift or a night shift. The next shift, or the next day's check on a 24-hour shift, starts a fresh session pinned to the checklist version current at that time.
- A short window costs very little: a new session shows the earlier checks as done (§2.3), so expiry costs one tap. This is also why no override is proposed.
- After a checklist is published, crew can record against the superseded version for at most 12 hours.
- A fixed UTC duration avoids shift-time and DST rules. Shift patterns are not modelled until Humanity is connected.

**What happens when it expires:**

- An Open session ends as **Incomplete**. The job writes a new `DEADLINE_INCOMPLETE` event (see §6). Until the job runs, every read and write already treats the session as ended.
- From then on nobody can reopen, join or record a check into it. The session page shows it as read-only, with its checks, participants and history, and points to the vehicle page to start or join the current inspection.
- The session, its participants, events and checks are kept permanently (AUD-1, AUD-5).

**Who can override:** nobody. A Supervisor or Administrator override would add a permission, an audit path and UI to recover something a new session already gives for free. Open question Q4 lists the alternative.

**Alternatives considered:**

| Window | For | Against |
|---|---|---|
| 6 h from open | Simplest; matches SES-1 | A session idle-closed during a long call usually could not be reopened at all |
| **12 h from open (recommended)** | Covers a shift; leaves at least 6 h to resume after an idle close | A 24-hour crew starts a second session in the evening (nothing is lost) |
| 24 h from open | Matches the OQ-79 assignment maximum | Spans a full shift change; the next crew joins or resumes yesterday's session and its version pin |
| End of station day | Matches daily windows | Depends on station time zone and DST; a check started at 23:30 could only be resumed for 30 minutes |
| Measured from last activity or last reopen | — | Can be extended indefinitely, so it does not meet policy point 1 |

## 5. Proposed SPEC text (amendment 6.0.0)

Section IDs refer to SPEC 5.0.0. Text marked *unchanged* is quoted only for position.

### 5.1 §3.3 permission table

**Before:**

> | Open/join an inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] any active user with an organization role may begin or join inspection work |
> | Rejoin (reopen) an ended inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] only a recorded participant may reopen; preserves original session and checklist version |

**After:**

> | Open/join an inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] any active user with an organization role may begin or join inspection work; [C: owner decision 2026-11-21, amendment 6.0.0] while a session is in progress on the vehicle, starting always joins it (SES-7) |
> | Rejoin (reopen) an ended inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] only a recorded participant may reopen; preserves original session and checklist version; [amendment 6.0.0] only before its resume deadline and while no other session is in progress on the vehicle (SES-3, SES-7, SES-8) |

The "Manually end" row is unchanged.

### 5.2 §6.2 States

**Before:**

> | **Incomplete** | Auto-closed by inactivity (§6.3), or manually ended with unmet items. Progress preserved. |
>
> Reopening moves Incomplete (or Closed) → Open and is an audited event [C]. Multiple sessions on the same vehicle may be open concurrently [C: concurrent crew members]. **[P]** An active user with an organization role joins an existing open session for the vehicle by default; starting a separate one is allowed even when another session is open. **[C: owner decision 2026-10-24, issue #78]** Only a participant may reopen that same ended session or manually end it; a nonparticipant may start a new session instead.

**After:**

> | **Incomplete** | Auto-closed by inactivity (§6.3), ended at its resume deadline (SES-8), or manually ended with unmet items. Progress preserved. |
>
> A session is **in progress** while it is Open and has reached neither its inactivity limit (SES-1) nor its resume deadline (SES-8). Reopening moves Incomplete (or Closed) → Open, within the limits of SES-3, SES-7 and SES-8, and is an audited event [C]. **[C: owner decision 2026-11-21, amendment 6.0.0]** A vehicle has at most one session in progress (SES-7). Concurrent crew members work in that one session; checking items concurrently is unchanged (§7.4). **[C: owner decision 2026-10-24, issue #78]** Only a participant may reopen that same ended session or manually end it. A nonparticipant joins the session in progress, or starts a new session when none is in progress.

### 5.3 §6.3 (retitled "Inactivity auto-close, resume deadline and one session per vehicle")

SES-1, SES-4, SES-5 and SES-6 are *unchanged*.

**SES-2 before:**

> … Reopening starts a new 6-hour window from the reopen instant.

**SES-2 after:**

> … Reopening starts a new 6-hour window from the reopen instant; that window never extends past the session's resume deadline (SES-8).

**SES-3 before:**

> **SES-3 [C: owner decision 2026-10-24, issue #78]** A participant may reopen a Closed or Incomplete session without supervisor approval. The reopen is audit-logged (actor, instant, session); a nonparticipant starts a new session instead.

**SES-3 after:**

> **SES-3 [C: owner decisions 2026-10-24, issue #78, and 2026-11-21, amendment 6.0.0]** A participant may reopen a Closed or Incomplete session without supervisor approval, before its resume deadline (SES-8) and only while no other session is in progress on the vehicle (SES-7). The reopen is audit-logged (actor, instant, session). A refused reopen changes nothing and is audited as a refused command. A nonparticipant joins the session in progress, or starts a new session when none is in progress.

**New SES-7:**

> **SES-7 [C: owner decision 2026-11-21, amendment 6.0.0] One session in progress per vehicle.** A vehicle has at most one session in progress (§6.2). While one is in progress, a request to start an inspection on that vehicle joins it, whichever checklist the requester chose. Nobody can start a separate or parallel session, and nobody can reopen another session on that vehicle. When none is in progress, starting opens a new session pinned under CHK-4. Session commands for one vehicle are serialized, so two simultaneous starts produce one session, and a start racing a reopen leaves exactly one session in progress.

**New SES-8:**

> **SES-8 [C: owner decision 2026-11-21 (no indefinite resumption); duration [P] pending OQ-81] Resume deadline.** Every session has a resume deadline `R = opened_at + 12 h`, fixed when it opens. Joins, activity and reopens never move it. From `R` on, the session cannot be reopened, joined or recorded into by anyone. If it is still Open, it ends as Incomplete, stamped at `R` (SES-9). The session, its participants, events and checks are kept permanently and stay readable (AUD-1, AUD-5). Work continues in a new session, which pins the checklist version then current (CHK-4) and shows earlier checks through current obligations (SES-4, SES-5, PRN-1). There is no override.

**New SES-9:**

> **SES-9 [P] Enforcement and recording.** Both time limits are enforced whenever a session is read or written, computed from its event history and its checks, whether or not the job has run. Once a limit has passed, the session reads as ended and joins, reopens and checks are refused. The idempotent, restart-safe job (ARC-4) then appends the ending event, stamped by rule rather than at the time the job runs:
>
> - if last activity + 6 h ≤ max(R, last activity), it writes `AUTO_INCOMPLETE` at last activity + 6 h (SES-6);
> - otherwise, it writes `DEADLINE_INCOMPLETE` at max(R, last activity).
>
> For a session opened under amendment 6.0.0, last activity is always earlier than `R`, so the deadline stamp is `R`. The max() applies only to sessions from before 6.0.0 that have activity after their `R`.

### 5.4 §17.2 ARC-4

**Before:** `| ARC-4 | Scheduled/background job execution that is idempotent and restart-safe | TIME-1, SES-6 |`
**After:** `| ARC-4 | Scheduled/background job execution that is idempotent and restart-safe | TIME-1, SES-6, SES-9 |`

### 5.5 §18 logical data model

**Before:** `| | session_event | A | open, reopen, close(auto/manual), roster confirm |`
**After:** `| | session_event | A | open, join, reopen, close(auto inactivity / resume deadline / manual), roster confirm |`

### 5.6 §23 acceptance criteria

**AC-6 before:**

> **Inactivity close [C].** A session with no persisted business activity for 6 h becomes Incomplete with all checks intact; viewing/heartbeat does not reset the timer; the crew member reopens without approval, and the reopen is audit-logged with the original checklist version unchanged.

**AC-6 after:**

> **Inactivity close [C].** A session with no persisted business activity for 6 h becomes Incomplete with all checks intact; viewing/heartbeat does not reset the timer; before its resume deadline and while no other session on the vehicle is in progress, the crew member reopens without approval, and the reopen is audit-logged with the original checklist version unchanged.

**New AC-41:**

> **AC-41 | One session per vehicle and resume deadline [C/P].**
> (1) With session S in progress on vehicle V, a start request on V by any role, for any checklist, joins S; no second session is created.
> (2) Two simultaneous starts on V produce one session.
> (3) A participant's reopen of an ended session T on V is refused while S is in progress, and succeeds after S ends, if it is still before T's deadline.
> (4) At `opened_at + 12 h`, S reads as Incomplete without waiting for the job. Reopen, join and check into S are refused. The job later records `DEADLINE_INCOMPLETE` stamped at the deadline, once, even if it runs late or repeatedly.
> (5) After S's deadline, a new session on V shows S's checks as satisfying current obligations, and S's records are unchanged and readable.
> (6) A session that is Incomplete through inactivity can be reopened before its deadline, and its new 6-hour window ends no later than the deadline.

### 5.7 §24.3 decision register (new rows)

> | OQ-81 | How long may an incomplete inspection session be resumed? | **Owner** | 6.3 (SES-8) | 12 h from the session's open instant; fixed constant; no override | Proposed 2026-11-21 |
> | OQ-82 | Is "one session in progress" per vehicle, or per vehicle checklist? | **Owner** | 6.2, 6.3 (SES-7) | Per vehicle, as directed | Proposed 2026-11-21 |

### 5.8 Other edits made with the amendment

- **§26.3:** the "6 h inactivity, crew reopen, refresh, no summary" row also cites SES-7 to SES-9.
- **§27:** add a 6.0.0 change-log row.
- **§25.6:** add the approval record, quoting the owner's approval.
- **Header:** update the version and status line.
- **§15.3 / OQ-44:** no text change. Several sessions can still exist on one vehicle, either in history or ended. The "latest correction wins" rule still applies to them.

## 6. Enforcement, scheduler, audit and migration

**Where expiry is enforced.** Expiry is enforced both when sessions are read and when they are written, as idle sessions are today. One pure function, generalizing `is_expired`, computes from a session's events, checks and `opened_at` whether it is in progress and, if not, which limit ended it and when. These callers use it:

- the join search;
- reopen and end, which first write the ending event as `_settle_idle` does today;
- `_session_for_check`;
- the vehicle and session pages;
- the home page "In progress" list.

Correctness therefore never waits for the job.

**Database backstops**, following the 0003 and 0005 pattern, so a writer that bypasses the commands is still refused:

- the check insert trigger refuses a session check at or after `opened_at + 12 h`;
- the `session_event` insert trigger refuses JOIN and REOPEN at or after `R`, and requires `DEADLINE_INCOMPLETE` to have no actor, a service receipt, and an instant equal to `GREATEST(R, last activity)`;
- the `session_event` insert trigger refuses an OPEN or REOPEN while another session on the same vehicle is in progress, computed from history and not from the projection. The vehicle row lock already serializes the commands; this trigger is the backstop. Codex should judge whether its per-insert cost is acceptable.

**Scheduler.** `close_inactive_sessions` and the existing `cya-sessions.timer` (every 30 minutes, `Persistent=true`) also select Open sessions whose `opened_at ≤ now − 12 h`. The output gains `expired=`, and a `--dry-run` reports what would close without writing. The design is restart-safe for these reasons:

- state comes from timestamps and immutable history;
- each close is keyed on the session's latest event, so a replay returns the original result;
- a refused close rolls back with no receipt;
- a missed run (worker restart, host down) only delays the audit event, never the enforcement.

No new timer or service is needed.

**Audit.**

- `DEADLINE_INCOMPLETE` is a `session_event` with no actor. It is written by `worker.session_auto_close` under command type `SESSION_DEADLINE_CLOSE`, with a receipt and audit row as for `AUTO_INCOMPLETE`.
- Refused starts, joins, reopens and checks are audited as refused commands by the existing `run_command` path.
- The API returns distinct codes instead of the blanket `SESSION_UNAVAILABLE`:
  - `409 SESSION_IN_PROGRESS`, with the in-progress `session_id` so the page can offer Join. All roles may join, so revealing the ID exposes nothing new.
  - `409 SESSION_RESUME_EXPIRED`.
  - `409 SESSION_NOT_OPEN`, which already exists for checks.
- No notification is sent when a session expires. Reopen notifications are unchanged.

**Migration of existing sessions.** No rows are edited or deleted.

- **Ended sessions older than 12 h** become non-reopenable on deploy. The rule is computed, so no data change is needed.
- **Open sessions opened more than 12 h ago** (including reopened old ones) read as ended from deploy. The first job run writes their ending event at `max(R, last activity)` (§5.3 SES-9). A crew member using one starts or joins the in-progress session and loses no checks. A later reopen is refused.
- **Several sessions open on one vehicle at deploy (earlier "separate" sessions)** are left to finish on their own. Participants can keep recording in them until each one goes idle, is ended, or reaches its deadline, which is at most 12 h after deploy. Meanwhile:
  - a default start joins the newest one, as today;
  - `separate: true` is refused;
  - reopening any other session on that vehicle is refused.

  Ending all but the newest at deploy would cut off crew mid-inspection, for no compliance benefit.
- **Before deploy:** run `close_inactive_sessions --dry-run` to report how many sessions will end and which vehicles have more than one in progress. Deploy at a quiet time.
- **Reversing the migration** stops if any `DEADLINE_INCOMPLETE` event exists, as reversing 0004 does today. A recorded close is never discarded.

**API compatibility.** Browser tabs open from before the deploy still send `separate: false` on every start, so the field stays accepted for one release:

- `separate: false` behaves as before;
- `separate: true` is refused with `SESSION_IN_PROGRESS` while a session is in progress, and otherwise opens normally.

A later PR removes the field.

## 7. Implementation plan

Each step is a separate PR, opened as a draft under `docs/review.md` and reviewed by Codex. Steps 2 and 3 are high-risk (concurrency, migrations, data integrity), so they need cross-family review. They touch the same files and run one after the other. Implementation starts only after the owner approves the SPEC text and an issue is opened for it.

### Step 1: SPEC amendment 6.0.0 (Markdown only)

Apply §5 of this proposal as approved, including the §25.6 record and the §27 row.

Verification: the documentation-only gate in `docs/review.md`:

- `git diff --check`
- `tools/tests`
- `check_markdown_links.py`
- `validate_scenarios.py` (reads SPEC and `tests/TRACEABILITY.md`; add AC-41 there)
- `check_normative_registry.py`
- gitleaks

### Step 2: one session in progress per vehicle (SES-7)

**Code:**

- `commands.py`:
  - add a shared `in_progress_session(org, vehicle_id, now)` helper, used under the vehicle lock;
  - in `open_or_join_session`, refuse `separate: true` while a session is in progress;
  - in `reopen_session`, refuse while another session is in progress.
- `api.py`: distinct refusal codes.
- New migration: the OPEN/REOPEN trigger backstop.
- `views.py`, `vehicle.html`, `vehicle.js`:
  - remove the "Start a separate inspection" disclosure and its confirm;
  - offer Resume only when nothing is in progress.
- `checklist.js`: a refused reopen with `SESSION_IN_PROGRESS` links to Join.
- Docs: `inspection-sessions.md`, `crew-inspection-ui.md`.

**Tests:**

- Remove or rewrite: `test_open_join_and_explicit_parallel_session`, `test_new_session_can_start_while_another_is_open_closed_or_incomplete`, `test_separate_always_opens_another`, `test_an_open_inspection_is_explicitly_joinable_and_separate_start_is_explained`.
- `separate: true` while a session is in progress gets 409 `SESSION_IN_PROGRESS`, with no session rows and an audited refusal.
- `separate: true` with nothing in progress opens a session.
- Start with a different checklist joins the session in progress; the pin is unchanged.
- Reopen by a participant is refused while another session is in progress, and succeeds once that session ends.
- Threaded races (as in `test_simultaneous_default_opens_share_one_session`): start against start, and start against reopen. Each leaves exactly one session in progress.
- Legacy: with two Open sessions on a vehicle, a default start joins the newest; both still accept their own participants' checks; no third session can open.
- The trigger refuses a raw second OPEN or REOPEN.
- The vehicle page has no separate control for any role.
- Browser smoke: start and join still work at every viewport.

### Step 3: resume deadline (SES-8, SES-9)

**Code:**

- `models.py`: `RESUME_HOURS = 12`; add `DEADLINE_INCOMPLETE` to the event type, activity-shape and actor-shape constraints.
- `projections.py`:
  - `fold_session` accepts the new event;
  - one `time_limit(state, opened_at, now)` function replaces `is_expired`.
- `commands.py`:
  - reopen and join check the deadline;
  - add `deadline_close_session`;
  - extend `_settle_idle` and `close_inactive_sessions` (deadline candidates, `expired=` count, `--dry-run`).
- `checks/commands.py`: `_session_for_check` refuses after `R`.
- Migrations:
  - session event trigger branch, in the `cya_inspections` app;
  - check trigger condition, in the `cya_checks` app;
  - both reverse only when no `DEADLINE_INCOMPLETE` event exists.
- Pages:
  - the session page shows an expired session read-only, with no Reopen, plus a link to the vehicle page;
  - the vehicle page offers Resume only before `R`;
  - the home "In progress" filter adds `opened_at > now − 12 h`.
- `checklist.js` maps `SESSION_RESUME_EXPIRED`.
- Docs: `inspection-sessions.md`, and the session cleanup paragraph of `deployment-runbook.md` (also run `test_asgi_server`, as `docs/review.md` requires when the runbook changes).

**Tests:**

- At `opened_at + 12 h − 1 µs` a check, a join and a reopen succeed. At `opened_at + 12 h` all three are refused, before the job runs.
- An idle close at 6 h followed by a reopen at 8 h: the new window ends at 12 h, not 14 h.
- The job:
  - writes one `DEADLINE_INCOMPLETE` stamped at `R` even when it runs hours late;
  - replays idempotently;
  - picks `AUTO_INCOMPLETE` when the idle limit comes first;
  - counts sessions changed underneath it as skipped.
- A legacy session with activity after `R` closes at `max(R, last activity)`, and the trigger accepts exactly that instant.
- The database refuses:
  - a raw check, JOIN or REOPEN after `R`;
  - a `DEADLINE_INCOMPLETE` with an actor or a wrong instant.
- A new session after expiry shows the old session's checks as satisfying current obligations (AC-41 (5)), and the old session's header, participants, events and checks are byte-identical before and after.
- An expired session page has no Reopen control, and its history is still shown.
- Home omits expired sessions.
- `--dry-run` writes nothing.
- The management command validates its arguments and exits 1 on failure.
- Rebuilding the projection from history matches.
- Reversing the migration with a deadline event refuses.

### Step 4: deploy notes (no deploy without the owner)

Record the pre-deploy `--dry-run` and the quiet-time recommendation in the runbook release notes.

Rehearse on the Ubuntu VM only after the owner asks. The Rocky VM is reserved for the issue #70 capacity test.

## 8. Open questions for the owner

1. **Confirm the policy** in §1, as Codex relayed it.
2. **Duration and anchor (OQ-81):** 12 hours from when the session opened? Alternatives are in §4.
3. **Hard cap while active:** should a session that is still being used at 12 hours also end then, so that one rule covers both cases (recommended)? The alternative is to stop only reopening and leave active sessions running. Then an active session never ends until it goes idle, which allows a chain of checks with no limit.
4. **Override:** none (recommended). The alternative is a Supervisor or Administrator "extend once by 12 h" action, audited.
5. **Per vehicle or per checklist (OQ-82):** since 5.0.0 a vehicle can have several checklists. Under "one per truck", a crew that needs checklist B while a checklist-A session is in progress must join it and end it (or wait for it to end) before starting B. Is that acceptable, or should the limit be one in progress per vehicle checklist? Both rule out the parallel duplicates seen on Medic 1. I recommend per vehicle, as directed, unless several Medic 1 checklists are routinely due at the same time.
6. **Ending someone else's inspection:** anyone may join, and any participant may end, so in practice any crew member can end the vehicle's session in progress. Keep this (recommended, since session state is informational), or restrict it?
7. **Separate sessions open at deploy:** let them finish on their own within 12 hours (recommended), or end all but the newest at deploy?
8. **Fixed constant or Administrator setting:** a fixed 12 h (recommended for R1), or a per-organization setting like OQ-79?

## 9. Not changed

The following are unchanged:

- item-level compliance (PRN-1, AC-1, AC-2);
- checks recorded outside any session;
- the 6-hour inactivity rule;
- CHK-4 version pinning;
- who may open, join or end;
- reopen notifications;
- corrections and invalidation;
- roster correction (§15.3);
- every historical record.

I have not seen the Medic 1 screenshots. This proposal relies only on the policy as Codex relayed it.
