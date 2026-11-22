# Proposal: one inspection in progress per vehicle checklist, shared item completion, and a resume deadline

**Status:** Draft proposal for owner and Codex review. It has no authority. It changes no behaviour, and it does not authorize implementation or deployment.
**Author:** Claude, 2026-11-21.
**Revision 2 (2026-11-21):** the owner answered three questions about a per-checklist variant (§1.2). This revision changes the proposal in four ways:

- the limit is one session in progress **per vehicle checklist**;
- a check of an item completes it in every checklist that is due;
- an item has one set of requirements on a vehicle;
- it records a display gap with shared items (§2.4).

The resume deadline, enforcement and migration design from revision 1 are unchanged.

**Proposed SPEC change:** amendment 6.0.0. It is major under §25.4(b), because it reverses approved rules:

- the §6.2 concurrent sessions rule;
- SES-3 "a nonparticipant starts a new session";
- the 1.9.0 decision on [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78);
- the SCH-5 default that a check has exactly one link;
- CHK-5 per-version item requirements.

## 1. Direction

### 1.1 Policy relayed by Codex

Codex relayed the following on 2026-11-21 as the owner's decision, after the owner compared screenshots of Medic 1:

1. An incomplete inspection session cannot be resumed indefinitely.
2. Only one inspection session can be in progress per truck. Crew join that session; they cannot open a parallel or separate one.
3. Historical checks and session records are always kept.

### 1.2 Owner's answers in the Claude session (2026-11-21)

The owner asked what would be required for "one session per truck per checklist", with the condition that an item appearing in more than one active checklist at the same time, once completed in any checklist, is completed in all of them. The owner then answered three questions:

| Question | Owner's answer |
|---|---|
| Does "active" mean a requirement currently open on the vehicle, not a checklist with a session in progress? | "I agree" |
| What if two checklists require different things of the same item (4 against 6)? | "The requirements should be for an item. It should not be possible for one checklist to require 4 and a different checklist for the same item to require 6." |
| Should the check also complete another checklist's overdue requirement within its late limit? | "Complete both, within time limits" |

These are decisions of direction. They are not approval of the SPEC text in §6, which §25.4(e) requires separately. Q1 in §9 asks the owner to confirm that the per-checklist limit replaces the per-truck wording in §1.1.

## 2. How sessions and checklists work today (SPEC 5.0.0, main `cb45f27`)

### 2.1 Session rules in the SPEC

- **§3.3:** all four roles may open or join a session. Only a recorded participant may reopen an ended session or end an open one.
- **§6.2:** "Multiple sessions on the same vehicle may be open concurrently [C]". Starting a separate session "is allowed even when another session is open". A nonparticipant may start a new session instead of reopening.
- **SES-1, SES-2:** after 6 hours with no persisted activity, a session closes as Incomplete. Reopening starts a new 6-hour window.
- **SES-3:** a participant may reopen a Closed or Incomplete session at any time, without approval. There is no age limit.
- **SES-4, SES-5, PRN-1, AC-2:** sessions never decide compliance; item checks satisfy obligations.
- **SES-6:** the auto-close is stamped at last activity plus 6 h, not at the time the job runs.
- **CHK-4:** a session keeps the checklist version it opened with.

### 2.2 Session code

| Action | Where | What happens |
|---|---|---|
| Start or join | `POST /api/v1/inspection-sessions` → `open_or_join_session` ([cya/inspections/commands.py:162](../../cya/inspections/commands.py)) | Takes the vehicle row lock. With `separate: false` (the default), it joins the newest session on the vehicle that is Open and not idle, **whatever checklist was requested**. If nothing is open, it opens a new session pinned to the checklist version current at that moment. JOIN does not reset the activity clock. |
| Separate | same, `separate: true` | Always opens another session, even while one is open. |
| Activity | `fold_session` ([projections.py:30](../../cya/inspections/projections.py)) | OPEN, REOPEN and checks recorded in the session count as activity. JOIN, END, viewing and failed saves do not. |
| Idle | `is_expired` (projections.py:74); `_settle_idle` (commands.py:461) | At last activity plus 6 h, an Open session is treated as ended at once, on read and on write. |
| Auto-close job | `close_inactive_sessions` → `auto_close_session` (commands.py:388); `deploy/rocky9/cya-sessions.timer` every 30 min, `Persistent=true` | A service-identity command appends `AUTO_INCOMPLETE` at last activity plus 6 h. It is idempotent, keyed on the latest event. |
| End | `POST …/{id}/end` → `end_session` (commands.py:342) | A participant appends `END_CLOSED` or `END_INCOMPLETE`. |
| Reopen | `POST …/{id}/reopen` → `reopen_session` (commands.py:283) | A participant appends REOPEN. There is **no age limit and no check for other open sessions**. It notifies the other participants. |
| Checks in a session | `_session_for_check` ([cya/checks/commands.py:502](../../cya/checks/commands.py)); trigger in `cya/checks/migrations/0003_check_session.py` | The session must be on this vehicle and pinned version (otherwise `SESSION_MISMATCH`), the user must be a participant, and the session must be Open and not idle. |
| Vehicle page | `vehicle`, `_session_rows` ([views.py:129–193](../../cya/inspections/views.py)); `vehicle.html`; `vehicle.js` | Shows the newest Open session with Join or Continue, a "Start a separate inspection" disclosure, and the participant's newest Incomplete session as the primary **Resume** button, however old it is. |
| Session page | `session` (views.py:275); `session.html`; `checklist.js` | An ended session shows "Reopen session". |
| Home | `in_progress` ([cya/shell/home.py:58](../../cya/shell/home.py)) | Lists the user's Open sessions that are not idle. |

All session refusals map to `409 SESSION_UNAVAILABLE` (api.py:57).

### 2.3 Items, checklists and obligations

- **CHK-1, CHK-2:** a vehicle owns one or more checklists, and one schedule slot can have several of them.
- **CHK-5:** each checklist version records its own requirements for every item (expected quantity, kind, allowed outcomes, Not-Applicable rules and so on). Two checklists on one vehicle can therefore ask different things of the same item today.
- **CHK-10, §5.7.1:** when two checklists require the same item, kit instance and due instant, only one obligation is created. A precedence order picks whose requirement it pins.
- **SCH-5, SCH-6:** for different due instants with overlapping windows, a check links only to the earliest-due obligation. A further link is allowed only through an Administrator-defined `shared_satisfaction_rule` that names exact requirement-version pairs. The model and the linker implement this (`choose_links`, [cya/checks/linking.py:172](../../cya/checks/linking.py)), but nothing in the application calls `configure_shared_satisfaction_rule`, so no rule can be created today. The database rejects a second link that has no rule (§18.1 item 2).
- **SCH-2:** a late completion links only when no obligation of the item has an on-time window containing the check.
- **Linking ignores the checklist.** `_eligible` (linking.py:39) selects obligations by vehicle, item, kit instance and time only. A check made under one checklist can already satisfy an obligation that another checklist created.

### 2.4 Gap: a shared item shows under only one checklist

The crew checklist builds one row per item. When two checklists list the same item, "the first template by id wins" (`_resolved_items`, [cya/attention/checklist.py:130–157](../../cya/attention/checklist.py)), and the row carries only that checklist's version. The session page shows only rows of its own checklist (`checklistRows()` in `checklist.js`).

So a session on the other checklist does not show the item at all, and the server would refuse a check from it (`SESSION_MISMATCH`). This applies today. It would also block either session rule: under "one per truck", shared items of the losing checklist cannot be reached from any session. This finding comes from reading the code, not from a test; the step 2 tests in §8 confirm it first.

### 2.5 What the current rules allow

- Several sessions open on one vehicle, including duplicates of the same checklist.
- A session from days ago can be reopened and recorded into against an outdated checklist version.
- One physical item checked once can leave another checklist's overlapping obligation unsatisfied, or, under §2.4, not even visible.

Compliance is still derived correctly from checks. The problems are confusion for crew, split participation, outdated version pins and duplicated work.

## 3. Proposed behaviour in short

- **In progress** means the session is Open, has not gone idle (SES-1) and has not reached its resume deadline.
- **One session in progress per vehicle checklist.** Starting checklist B while a B session is in progress joins it. A second B session cannot be started, and an ended B session cannot be reopened while one is in progress. Sessions on different checklists can be in progress at the same time.
- **One check completes the item everywhere it is due.** A check of an item, or of one kit instance's item, on a vehicle counts toward every unsatisfied obligation of that item that the check's time can complete, whichever checklist created it: on time (`E ≤ t ≤ G`) or late within its limit (`G < t ≤ L`). "Active" is decided by those obligations, never by which sessions are open.
- **One set of requirements per item on a vehicle.** Every checklist on the vehicle uses the same expected quantity, kind, allowed outcomes and other requirements for an item. A checklist decides which items it covers, on which schedule and in what order.
- **A shared item appears in every checklist that lists it.** Its status is shared, so a Pass recorded in one session shows as done in the others, live.
- **Resume deadline of 12 h from the session's open instant.** After it, the session is ended and read-only, cannot be reopened, joined or recorded into, and is kept permanently.
- Nothing is deleted or rewritten.

## 4. Resume window: 12 hours from the session's open instant

This section is unchanged from revision 1.

**When it starts:** at `opened_at`, the server instant of the session's OPEN event. Joins, checks and reopens never move it.

**Why from the open instant:** a reopen counts as activity (SES-2), so a window measured from activity would restart with every reopen and could be extended indefinitely.

**Why 12 hours:**

- A routine check takes well under an hour, and SES-1 already tolerates gaps of up to 6 hours.
- 12 hours covers a day or night shift. The next shift starts a fresh session pinned to the current version.
- A new session loses nothing, because checks satisfy obligations. This is why no override is proposed.
- It limits recording against a superseded checklist version to 12 hours.
- A fixed UTC duration needs no shift-time or DST rules.

**On expiry:**

- An Open session ends as Incomplete; the job writes `DEADLINE_INCOMPLETE`.
- Before the job runs, every read and write already treats the session as ended.
- Nobody can reopen, join or record into it afterwards.
- Its page is read-only and points to the vehicle page.
- Its records are kept permanently (AUD-1, AUD-5).

**Override:** none (Q4).

**Alternatives considered:**

| Window | For | Against |
|---|---|---|
| 6 h from open | Simplest | A session idle-closed during a long call usually could not be reopened |
| **12 h from open (recommended)** | Covers a shift | A 24-hour crew starts a second session in the evening, losing nothing |
| 24 h from open | Matches OQ-79 | Spans a shift change and keeps yesterday's version pin |
| End of station day | Matches daily windows | Depends on time zone and DST; a check started at 23:30 could be resumed for only 30 minutes |
| From last activity or reopen | — | Can be extended indefinitely |

## 5. Item requirements and shared completion

### 5.1 One set of requirements per item on a vehicle (owner answer 2)

The owner's words, "the requirements should be for an item", point to moving the requirements out of each checklist version and onto the item on that vehicle.

**Recommended: a vehicle item requirement.** Each vehicle has one effective requirement per item. It is versioned, effective-dated and published like a checklist (CHK-3), and holds:

- kind;
- expected quantity and unit;
- allowed outcomes;
- bulk-pass eligibility;
- Not-Applicable rules;
- conditional rules;
- instructions;
- placement (subject to Q6).

A checklist version then records only which items it covers, their schedule rules and display order, and refers to the vehicle's item requirements for everything else. Editing an item requirement once changes it for every checklist on that vehicle, from its effective instant.

**Alternative: publish-time validation.** Requirements stay in each checklist version, and the system refuses to publish a version that disagrees with another checklist on the same vehicle effective at any overlapping instant. This is smaller, but changing a value (4 to 6) then needs all affected checklists republished together in one action, or the first publish is refused. It also leaves the owner's "requirements for an item" as a check rather than as the structure.

**Scope:** per vehicle, because SPEC 5.0.0 made checklists vehicle-owned so that trucks can differ. Q5 asks whether the owner meant organization-wide instead.

**Placement:** obligations are keyed by vehicle, item, kit instance and due instant, with no placement. If two checklists placed the same item in different compartments, one check would complete both even though they are two physical stocks. I recommend placement belongs to the item requirement (one place per item per vehicle); an item stocked in two places becomes two items (Q6).

**Unchanged:**

- kit contents come from the shared kit definition (KDEF-1 to KDEF-12), which already has one requirement;
- kit presence obligations stay per kit-list line (KDEF-12).

**Migration:**

- Published checklist versions are immutable (CHK-5) and keep their recorded values for history.
- Existing obligations keep their pinned requirement versions.
- The cutover creates each vehicle's item requirements from its checklists in force. Where two checklists on a vehicle disagree about an item, the cutover refuses and lists the conflicts, following §18.2. A Supervisor first publishes matching versions.
- The seed data is synthetic (CHK-7), so few or no conflicts are expected.
- The cutover is idempotent and validates before it commits.

### 5.2 One check completes every eligible obligation (owner answers 1 and 3)

A check of item X on vehicle V, for kit instance K if X is a kit item, recorded at server time `t`, links to **every** unsatisfied obligation of (V, X, K) for which `E ≤ t ≤ G` (on time) or `G < t ≤ L` (late, within its pinned limit). This applies whichever checklist created the obligation. The constraints on this rule:

- **Same due instant:** CHK-10 still creates only one obligation, so there is one link.
- **Outcome:** each linked obligation follows §5.4 for that outcome. A Pass or Fail satisfies all of them. Unable to Inspect satisfies none until a supervisor completes it (5.4.1). A Fail raises one deficiency for the item, as today.
- **Kit limits:**
  - a check never links to another kit instance's obligation (owner decision 1.8.0);
  - a kit presence check links only to obligations of its own kit-list line (KDEF-12).
- **Corrections and invalidation:** these re-derive all of a check's links together (§19.1, INV-2), as they do now for shared links.
- **Concurrency:** the conflict warning (CON-3) compares against the newest effective check for the vehicle, item and kit instance across all its obligations. Two crews checking the same item from different checklist sessions therefore get the warning.
- **Seal substitutions:** unchanged.
- **Shared-satisfaction rules (SCH-5):** these are no longer needed. The tables stay for history; no new rule can be created, and none exists today.

**Same checklist, earlier overdue obligation (Q7).** The rule also applies when the earlier late obligation belongs to the same checklist. For example, yesterday's 18:00 daily is unsatisfied and its late limit is 07:00 today; a check at 06:30 completes it late and today's 06:00 daily on time. Today SCH-2 links only today's. I recommend the uniform rule, because the owner's answer was "complete both, within time limits" and one rule is simpler, but this changes late-completion reporting, so Q7 asks.

### 5.3 Display

The checklist API returns one row per (checklist, item), and per kit instance for kit items. Each row shows the item's shared obligation state and attention state. A session shows the rows of its own checklist, so a shared item appears in every checklist that lists it. Recording it from any of those sessions is allowed, because the check names the session's own pinned version. Live updates are already per vehicle (§17.3), so every open session sees the result within the freshness targets.

## 6. Proposed SPEC text (amendment 6.0.0)

Section IDs refer to SPEC 5.0.0.

### 6.1 §3.3 permission table

**Before:**

> | Open/join an inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] any active user with an organization role may begin or join inspection work |
> | Rejoin (reopen) an ended inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] only a recorded participant may reopen; preserves original session and checklist version |

**After:**

> | Open/join an inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] any active user with an organization role may begin or join inspection work; [C: owner decision 2026-11-21, amendment 6.0.0] while a session of the chosen vehicle checklist is in progress, starting always joins it (SES-7) |
> | Rejoin (reopen) an ended inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] only a recorded participant may reopen; preserves original session and checklist version; [amendment 6.0.0] only before its resume deadline and while no other session of the same vehicle checklist is in progress (SES-3, SES-7, SES-8) |

### 6.2 §4.3 checklists

**CHK-5 before:**

> **CHK-5 [C] Checklist version contents.** A published vehicle checklist version retains its item set; each item's compartment, kit, equipment or system placement; display order; instructions; expected quantity; kind; bulk-pass eligibility; allowed outcomes; schedule rules; conditional rules; and Not-Applicable rules. …

**CHK-5 after:**

> **CHK-5 [C] Checklist version contents.** A published vehicle checklist version retains its item set, display order and schedule rules. Each item's other requirements come from the vehicle's item requirement (CHK-11) in force at the obligation's due instant. A version published before amendment 6.0.0 keeps the values it recorded, for history and for the obligations that pin it. …

The rest of CHK-5 is unchanged.

**New CHK-11:**

> **CHK-11 [C: owner decision 2026-11-21, amendment 6.0.0; scope and contents [P] pending OQ-83, OQ-84] One requirement per item on a vehicle.** Each vehicle has one effective requirement for each item it inspects. The requirement covers kind, expected quantity and unit, allowed outcomes, bulk-pass eligibility, Not-Applicable rules, conditional rules, instructions and placement. It is versioned, effective-dated and published under CHK-3 by the roles that publish that vehicle's checklists. Every checklist on the vehicle uses it, so two checklists can never require different things of one item. Kit contents keep their kit definition's requirements (KDEF-1 to KDEF-12). Vehicle files (IMP-7) carry item requirements alongside the checklists.

### 6.3 §5.3 and §5.5 linking

**SCH-2 before:**

> … It may be linked to an obligation that is still unsatisfied and past `G` only when no unsatisfied obligation of the same vehicle+item has an on-time window containing `t_c` (SCH-6 ordering applies among overdue ones: earliest-due first). …

**SCH-2 after:**

> … It is linked to every obligation of the same vehicle+item that is still unsatisfied, past `G` and not past its `L`, together with every on-time one (SCH-6). …

**SCH-5 before:**

> **SCH-5 [P]** Link cardinality: `check_obligation_link` rows are unique per `(check_id, obligation_id)`. By default a check has **exactly one** link. A check may have more than one only when each additional link carries the `shared_rule_id` of an administrator-defined, audited rule that explicitly declares the sharing (SCH-6); a link without a `shared_rule_id` is rejected if the check already has a link. One check never silently counts toward more than one obligation.

**SCH-5 after:**

> **SCH-5 [C: owner decision 2026-11-21, amendment 6.0.0] Link cardinality.** `check_obligation_link` rows are unique per `(check_id, obligation_id)`. A check links to every obligation SCH-6 makes eligible, and each link records why it was made (on time or late). Shared-satisfaction rules recorded before 6.0.0 remain in history; no new rule can be created.

**SCH-6 before:**

> **SCH-6 [P]** When a check is recorded and several unsatisfied obligations for the same vehicle+item are eligible (windows containing `t_c`, possible only with overlapping schedules; or late-linkable ones under SCH-2), the check links to the earliest-due one. A shared-satisfaction rule (e.g. a shared-parent item) may additionally link it to the others it names. Invalidating or correcting the check re-derives all its links together (§19.1). For an item inside a kit only the obligations of the kit instance the check names are eligible; a check never links to another instance's obligation, not even through a shared-satisfaction rule. …

**SCH-6 after:**

> **SCH-6 [C: owner decisions 2026-11-21, amendment 6.0.0] One check completes the item wherever it is due.** A check links to every unsatisfied obligation of the same vehicle+item for which `E ≤ t_c ≤ G` or `G < t_c ≤ L`, whichever vehicle checklist created it. Whether an obligation is eligible depends only on obligations and time, never on which sessions are open (§6.1). Invalidating or correcting the check re-derives all its links together (§19.1). For an item inside a kit only the obligations of the kit instance the check names are eligible. A kit presence check is eligible only for obligations of its own kit-list line (KDEF-12). …

The required tests are amended to: overlapping windows across two checklists (AC-30), two identical kit instances with one checked, on-time plus late links from one check, and invalidation removing all links.

### 6.4 §6.2 States

**Before:**

> | **Incomplete** | Auto-closed by inactivity (§6.3), or manually ended with unmet items. Progress preserved. |
>
> Reopening moves Incomplete (or Closed) → Open and is an audited event [C]. Multiple sessions on the same vehicle may be open concurrently [C: concurrent crew members]. **[P]** An active user with an organization role joins an existing open session for the vehicle by default; starting a separate one is allowed even when another session is open. **[C: owner decision 2026-10-24, issue #78]** Only a participant may reopen that same ended session or manually end it; a nonparticipant may start a new session instead.

**After:**

> | **Incomplete** | Auto-closed by inactivity (§6.3), ended at its resume deadline (SES-8), or manually ended with unmet items. Progress preserved. |
>
> A session is **in progress** while it is Open and has reached neither its inactivity limit (SES-1) nor its resume deadline (SES-8). Reopening moves Incomplete (or Closed) → Open, within SES-3, SES-7 and SES-8, and is an audited event [C]. **[C: owner decisions 2026-11-21, amendment 6.0.0]** Each vehicle checklist has at most one session in progress on its vehicle (SES-7). Sessions of different checklists may be in progress together, and an item they share is completed by one check (SCH-6). **[C: owner decision 2026-10-24, issue #78]** Only a participant may reopen that same ended session or manually end it. A nonparticipant joins the session in progress for that checklist, or starts a new one when none is in progress.

### 6.5 §6.3 (retitled "Inactivity auto-close, resume deadline and one session per checklist")

SES-1, SES-4, SES-5 and SES-6 are unchanged.

**SES-2 after** (appended): "…; that window never extends past the session's resume deadline (SES-8)."

**SES-3 after:**

> **SES-3 [C: owner decisions 2026-10-24, issue #78, and 2026-11-21, amendment 6.0.0]** A participant may reopen a Closed or Incomplete session without supervisor approval, before its resume deadline (SES-8) and only while no other session of the same vehicle checklist is in progress (SES-7). The reopen is audit-logged (actor, instant, session). A refused reopen changes nothing and is audited as a refused command. A nonparticipant joins the session in progress for that checklist, or starts a new session when none is in progress.

**New SES-7:**

> **SES-7 [C: owner decision 2026-11-21, amendment 6.0.0] One session in progress per vehicle checklist.** For each vehicle checklist (its identity, not a version), at most one session is in progress. A request to start that checklist while one is in progress joins it, even when it pins an earlier version (CHK-4). Nobody can start a second session of that checklist, and nobody can reopen another session of it. Starting a different checklist on the same vehicle is unaffected. Session commands for one vehicle are serialized, so two simultaneous starts of one checklist produce one session, and a start racing a reopen leaves exactly one in progress.

**New SES-8:**

> **SES-8 [C: owner decision 2026-11-21 (no indefinite resumption); duration [P] pending OQ-81] Resume deadline.** Every session has a resume deadline `R = opened_at + 12 h`, fixed when it opens. Joins, activity and reopens never move it. From `R` on, the session cannot be reopened, joined or recorded into by anyone. If it is still Open, it ends as Incomplete, stamped under SES-9. Its records are kept permanently and stay readable (AUD-1, AUD-5). Work continues in a new session, which pins the current version (CHK-4) and shows earlier checks through current obligations (SES-4, SES-5, PRN-1). There is no override.

**New SES-9:**

> **SES-9 [P] Enforcement and recording.** Both time limits are enforced whenever a session is read or written, computed from its event history and its checks, whether or not the job has run. The idempotent, restart-safe job (ARC-4) then appends the ending event, stamped by rule:
>
> - if last activity + 6 h ≤ max(R, last activity), `AUTO_INCOMPLETE` at last activity + 6 h (SES-6);
> - otherwise `DEADLINE_INCOMPLETE` at max(R, last activity).
>
> For a session opened under 6.0.0, last activity is always before `R`, so the deadline stamp is `R`. The max() applies only to older sessions with activity after their `R`.

### 6.6 §6.5 Views

**New VW-5:**

> **VW-5 [C: owner decision 2026-11-21, amendment 6.0.0]** An item appears in every vehicle checklist that lists it, in that checklist's order. Its obligation and attention state are the vehicle's, shared by all of those checklists, so a check recorded in one session shows in the others without a refresh (§17.3).

### 6.7 Other sections

- **§17.2 ARC-4:** cite SES-9.
- **§18:**
  - `session_event` lists "close (auto inactivity / resume deadline / manual)";
  - add `vehicle_item_requirement` (versioned, A);
  - mark `shared_satisfaction_rule` as history only.
- **§18.1 item 2 before:** "`check_obligation_link` unique per `(check_id, obligation_id)`; a second link for one `check_id` requires a non-null `shared_rule_id` (SCH-5)."
- **§18.1 item 2 after:** "`check_obligation_link` unique per `(check_id, obligation_id)`; every link of one check is to an obligation of the same vehicle, item and kit instance (SCH-6)."
- **AC-6:** add "before its resume deadline and while no other session of the same checklist is in progress".
- **AC-30 before:** "**Link cardinality [P].** With two overlapping-window obligations for one item, one check links to the earliest-due only; a second link without a shared-rule id is rejected; invalidating the check voids all its links."
- **AC-30 after:** "**Shared completion [C].** With two overlapping-window obligations for one item from two checklists, one Pass links to both; with one of them past `G` but not past `L`, it links to both (one late); an obligation past its `L` is not linked; a check of kit instance K never links to instance K2; invalidating the check voids all its links."
- **New AC-41 (sessions and deadline):**
  1. With session S of checklist A in progress on V, a start of A by any role joins S, while a start of checklist B opens or joins a B session.
  2. Two simultaneous starts of A produce one session.
  3. A participant's reopen of an ended A session is refused while S is in progress, and succeeds after S ends, before its own deadline.
  4. At `opened_at + 12 h`, S reads as Incomplete without the job. Reopen, join and check into S are refused. The job later records `DEADLINE_INCOMPLETE` at the deadline, once.
  5. After the deadline, a new session shows S's checks through current obligations, and S's records are unchanged.
  6. A session idle-closed can be reopened before its deadline, and its window ends by the deadline.
- **New AC-42 (one requirement, shared display):**
  1. Publishing a checklist cannot give an item a requirement different from the vehicle's item requirement.
  2. An item listed by checklists A and B appears in both sessions with one shared state.
  3. A Pass recorded in the A session shows as done in the B session within §17.3's targets.
- **§24.3:**

  > | OQ-81 | How long may an incomplete inspection session be resumed? | **Owner** | 6.3 (SES-8) | 12 h from the session's open instant; fixed constant; no override | Proposed |
  > | OQ-82 | Is "one session in progress" per vehicle or per vehicle checklist? | **Decided** (owner, 2026-11-21) | 6.2, 6.3 (SES-7) | Per vehicle checklist, with shared completion (SCH-6) | Pending text approval |
  > | OQ-83 | Is an item's requirement per vehicle or organization-wide? | **Owner** | 4.3 (CHK-11) | Per vehicle | Proposed |
  > | OQ-84 | Does placement belong to the item requirement? | **Owner** | 4.3 (CHK-11) | Yes; an item stocked in two places is two items | Proposed |
  > | OQ-85 | Does one check also complete the same checklist's earlier late obligation? | **Owner** | 5.3, 5.5 (SCH-2, SCH-6) | Yes, uniformly within `L` | Proposed |

- **§26.3, §27, §25.6 and the header:** as for any amendment. The OQ-03 register row cites SCH-6 as revised.
- **§15.3, OQ-44:** unchanged. Several sessions can still exist on one vehicle.

## 7. Enforcement, scheduler, audit and migration

**Sessions.** Enforcement of both time limits is unchanged from revision 1:

- **Read and write time:** a pure function computes from history whether a session is in progress and, if not, which limit ended it.
- **Callers:** joins, reopen and end (which write the ending event first), `_session_for_check`, the vehicle and session pages, and home.
- **Database backstops:**
  - the check trigger refuses session checks at or after `R`;
  - the `session_event` trigger refuses JOIN and REOPEN at or after `R`, and fixes the `DEADLINE_INCOMPLETE` instant and service receipt;
  - the `session_event` trigger refuses OPEN or REOPEN while another session **of the same vehicle checklist** is in progress, computed from history.
- **Scheduler:** `close_inactive_sessions` on the existing 30-minute timer adds deadline closes, an `expired=` count and `--dry-run`. Enforcement never waits for it, so a missed run or a restart only delays the audit event.
- **Audit:** `DEADLINE_INCOMPLETE` is written by `worker.session_auto_close` under command type `SESSION_DEADLINE_CLOSE`. Refusals are audited by `run_command`.
- **API codes:**
  - `409 SESSION_IN_PROGRESS`, with the session ID so the page can offer Join;
  - `409 SESSION_RESUME_EXPIRED`;
  - `409 SESSION_NOT_OPEN`.
- **Notifications:** none on expiry.

**Linking.** Linking already runs under the vehicle and item lock (CON-5), so adding links does not add locking. The database constraint that requires `shared_rule_id` on a second link is replaced: every link of one check must be to the same vehicle, item and kit instance, enforced by a trigger. Each link records `ON_TIME` or `LATE`, as `timing` does now. Existing links and checks are not re-linked: SCH-2 never re-links past checks, and the new rule applies to checks recorded after the cutover. The readiness and report projections already count per obligation, so an obligation satisfied by a shared link reads as satisfied with no projection change. AC-36 and RPT-5a tests are rerun to confirm this.

**Migration of existing data.** No row is edited or deleted.

- **Sessions:**
  - ended sessions older than 12 h become non-reopenable by rule;
  - Open sessions older than 12 h read as ended, and the first job run stamps them at `max(R, last activity)`;
  - where several sessions of the **same** checklist are open on a vehicle, they finish on their own within 12 h. Meanwhile a start joins the newest, `separate: true` is refused, and reopening another one is refused;
  - sessions of different checklists need no handling;
  - reversing the migration stops if any `DEADLINE_INCOMPLETE` event exists.
- **Item requirements:** the cutover in §5.1 refuses if any vehicle's checklists disagree about an item, lists the conflicts, and is idempotent.
- **Before deploy:** run `close_inactive_sessions --dry-run` and the item-requirement cutover in its dry-run mode, and deploy at a quiet time.

**API compatibility.** `separate: false` stays accepted for one release, because tabs left open from before the deploy still send it. `separate: true` is refused with `SESSION_IN_PROGRESS` while the chosen checklist has a session in progress, and otherwise opens normally. A later PR removes the field.

## 8. Implementation plan

Each step is a separate draft PR, reviewed by Codex under `docs/review.md`. Steps 3 to 6 change migrations, concurrency or data integrity, so they need cross-family review. Implementation starts only after the owner approves the SPEC text and an issue is opened for it.

### Step 1: SPEC amendment 6.0.0 (Markdown only)

Apply §6 as approved, with the §25.6 record and §27 row, and add AC-41 and AC-42 to `tests/TRACEABILITY.md`.

Verification: the documentation-only gate in `docs/review.md`.

### Step 2: shared items appear in every checklist (VW-5)

**Code:**

- `cya/attention/checklist.py` builds rows per (checklist, item), and per kit instance; `_resolved_items` and `_placed_content` no longer drop an item a later checklist also lists.
- The row key used by `checklist.js` for local state and announcements includes the checklist version.
- Docs: `attention-views.md`, `crew-inspection-ui.md`.

**Tests:**

- First, a test showing the current gap: item X in checklists A and B, and the B session lacks X.
- After the fix, X shows in both sessions with the same obligation and attention state.
- A check of X in the B session is accepted and shows as done in the A view.
- Normal and Priority order stay within each checklist.
- Kit and presence rows are unchanged.
- The query count is bounded with two checklists (`assertNumQueries`).

### Step 3: one check completes every eligible obligation (SCH-2, SCH-5, SCH-6)

**Code:**

- `choose_links` links every on-time and late-eligible obligation and stops consulting shared rules for new checks.
- `configure_shared_satisfaction_rule` refuses new rules.
- A migration replaces the second-link constraint with the same-vehicle-item-kit trigger.
- CON-3 compares across all obligations of the vehicle, item and kit instance.
- Until step 4 lands, links are made only between obligations whose pinned requirement versions have equal check properties. This guard fails closed and is removed in step 4.

**Tests:**

- Overlapping windows across two checklists give two links.
- One on-time and one late (within `L`) obligation give two links; past `L` gives none.
- The same-checklist earlier late obligation follows Q7.
- A kit instance never links to another instance; a presence check only links within its line.
- Unable to Inspect satisfies neither obligation.
- Correction and invalidation re-derive or void all links.
- Concurrent checks from two sessions raise CON-3.
- Replaying a lost response returns the same links.
- The trigger refuses a link to another item.
- Readiness and reports (AC-36, RPT-5a) count each obligation once.
- The interim guard skips mismatched requirements.

### Step 4: one requirement per item on a vehicle (CHK-11)

This is the largest step. It covers:

- the `vehicle_item_requirement` model, versioned and effective-dated;
- obligation generation reading the requirement in force at `D`;
- the checklist editor editing item requirements once per vehicle;
- vehicle file export and import (IMP-7, format version bump);
- the cutover with dry-run, conflict report and idempotent replay;
- docs: `checklists-implementation.md`, `configuration-management.md`, `vehicle-files.md`.

**Tests:**

- Publishing a checklist cannot set a different quantity for an item.
- Changing the item requirement changes both checklists from its effective instant, and earlier obligations keep their pins.
- The cutover refuses on a conflict and succeeds after it is resolved.
- Export and import round-trip.
- A future-effective requirement applies only from its instant.
- Permissions follow checklist publishing.

### Step 5: one session in progress per vehicle checklist (SES-7)

**Code:**

- `in_progress_session(org, vehicle_id, template_id, now)` under the vehicle lock.
- The default start joins the in-progress session of the chosen checklist.
- `separate: true` and reopen are refused while one is in progress.
- API codes.
- The OPEN/REOPEN trigger backstop.
- The vehicle page lists each checklist's session in progress with Join or Continue, removes "Start a separate inspection", and offers Resume only when that checklist has nothing in progress.
- `checklist.js` handles `SESSION_IN_PROGRESS`.
- Docs.

**Tests:**

- Rewrite: `test_open_join_and_explicit_parallel_session`, `test_new_session_can_start_while_another_is_open_closed_or_incomplete`, `test_default_join_is_vehicle_scoped_even_when_requested_template_differs` (now joins only the same checklist), `test_separate_always_opens_another`, `test_an_open_inspection_is_explicitly_joinable_and_separate_start_is_explained`.
- A start of A while A is in progress joins it, even on an older version pin.
- A start of B while A is in progress opens B.
- `separate: true` for A while A is in progress is refused with no writes and an audited refusal.
- Reopen is refused, then allowed after A's session ends.
- Threaded races: two starts of A give one session, and a start racing a reopen of A leaves one in progress.
- Legacy: two Open A sessions continue for their own participants, and no third opens.
- The trigger refuses a raw second A OPEN.
- The vehicle page shows one entry per checklist in progress.
- Browser smoke at every viewport.

### Step 6: resume deadline (SES-8, SES-9)

This is as revision 1 step 3:

- `RESUME_HOURS = 12`;
- `DEADLINE_INCOMPLETE` event type and constraints;
- `time_limit()` replaces `is_expired`;
- reopen, join and check refusals after `R`;
- job extension with `--dry-run`;
- triggers in `cya_inspections` and `cya_checks` that refuse reversal while a deadline event exists;
- a read-only expired session page;
- the home filter;
- runbook and docs (run `test_asgi_server`).

**Tests:**

- At `R − 1 µs` a check, join and reopen succeed; at `R` all are refused, before the job runs.
- Idle at 6 h then a reopen at 8 h: the window ends at 12 h.
- The job stamps `R` when late, replays idempotently, prefers `AUTO_INCOMPLETE` when the idle limit comes first, and skips sessions that changed.
- A legacy session closes at `max(R, last activity)`.
- The database refuses a raw late check, JOIN or REOPEN, and a wrong `DEADLINE_INCOMPLETE`.
- A new session after expiry shows the old checks, and the old records are byte-identical.
- The expired page has no Reopen.
- Home omits expired sessions.
- `--dry-run` writes nothing.
- The projection rebuild matches.
- Reversal refuses.

### Step 7: deploy notes

Record both dry-runs and the quiet-time recommendation in the runbook release notes.

Rehearse on the Ubuntu VM only if the owner asks. The Rocky VM is reserved for issue #70. Deployment needs the owner's authorization.

**Ordering:** steps 5 and 6 touch the same session files and run one after the other. Step 2 is independent of the session steps. Step 3 can land before step 4 because of its fail-closed guard.

## 9. Open questions for the owner

**Answered 2026-11-21** (§1.2):

- "Active" means requirements currently open on the vehicle.
- An item has one requirement.
- Late requirements within their limit are completed too.

**Still open:**

1. **Confirm the direction.** One session in progress per vehicle checklist, with shared completion, replaces "one per truck" in the relayed policy. Also confirm points 1 and 3 of §1.1.
2. **Duration (OQ-81):** 12 h from the session's open instant?
3. **Hard cap while active:** should a session still in use at 12 h also end then (recommended)?
4. **Override:** none (recommended), or a Supervisor or Administrator "extend once" action?
5. **Item requirement scope (OQ-83):** per vehicle (recommended) or organization-wide?
6. **Requirement contents (OQ-84):** I propose kind, expected quantity and unit, allowed outcomes, bulk-pass eligibility, Not-Applicable rules, conditional rules, instructions and placement. In particular, should placement belong to the item, so that an item stocked in two places becomes two items (recommended)?
7. **Same checklist's earlier late obligation (OQ-85):** should one check also complete it, within `L` (recommended, uniform rule)?
8. **Ending someone else's inspection:** any crew member can in practice end a checklist's session by joining it. Keep this (recommended)?
9. **Duplicate sessions of one checklist open at deploy:** let them finish within 12 h (recommended)?
10. **Fixed 12 h or an Administrator setting?** Fixed (recommended for R1).

## 10. Not changed

The following are unchanged:

- item-level compliance (PRN-1, AC-1, AC-2);
- checks outside sessions;
- the 6-hour inactivity rule;
- CHK-4 pinning;
- CHK-10 single obligation per anchor;
- kit-instance and kit-list-line separation;
- who may open, join or end;
- reopen notifications;
- corrections and invalidation;
- roster correction;
- every historical record.

I have not seen the Medic 1 screenshots.
