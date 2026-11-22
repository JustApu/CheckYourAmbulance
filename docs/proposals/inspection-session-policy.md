# Proposal: one inspection in progress per vehicle checklist, shared completion per item and place, and a resume limit

**Status:** Draft proposal for owner and Codex review. It has no authority. It changes no behaviour, and it does not authorize implementation or deployment.
**Author:** Claude, 2026-11-21.

**Revisions:**

- **Revision 2 (2026-11-21).** After the owner's first answers (§1.2), the limit became one session per vehicle checklist, with shared item completion and one set of requirements per item.
- **Revision 3 (2026-11-21).** After the owner's second answers (§1.3), the changes are:
  - the resume limit is **6 hours by default, set per organization**;
  - requirements are kept per truck **and per place** when an item is kept in more than one place on the truck;
  - one check also completes the same checklist's earlier late obligation;
  - there is no override.
- **Revision 4 (2026-11-21).** This revision follows Codex review 6010089090 at `0d48bfa`:
  - the owner confirmed "One per checklist" over one per truck (§1.3);
  - §4 now explains how idle close and the deadline interact for any setting;
  - SES-9 uses one effective end instant for reads, refusals, the projection, the triggers and the job, including older sessions with activity after their deadline.

**Proposed SPEC change:** amendment 6.0.0. It is major under §25.4(b), because it reverses approved rules:

- the §6.2 concurrent sessions rule;
- SES-3 "a nonparticipant starts a new session";
- the 1.9.0 decision on [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78);
- the SCH-5 default that a check has exactly one link;
- the SCH-2 rule that a late link is made only when nothing is on time;
- CHK-5 per-version item requirements;
- the obligation key of §5.1 and CHK-10, which gains the place.

## 1. Direction

### 1.1 Policy relayed by Codex

Codex relayed the following on 2026-11-21 as the owner's decision, after the owner compared screenshots of Medic 1:

1. An incomplete inspection session cannot be resumed indefinitely.
2. Only one inspection session can be in progress per truck. Crew join that session; they cannot open a parallel or separate one.
3. Historical checks and session records are always kept.

### 1.2 Owner's first answers in the Claude session (2026-11-21)

The owner asked what would be required for "one session per truck per checklist", where completing an item that appears in several active checklists in any one of them completes it in all of them. The owner then answered:

| Question | Owner's answer |
|---|---|
| "Active" means a requirement currently open on the vehicle, not a checklist with a session in progress? | "I agree" |
| Two checklists requiring different things of one item (4 against 6)? | "The requirements should be for an item. It should not be possible for one checklist to require 4 and a different checklist for the same item to require 6." |
| Also complete another checklist's late requirement? | "Complete both, within time limits" |

### 1.3 Owner's second answers in the Claude session (2026-11-21)

| Question | Owner's answer | How this proposal reads it |
|---|---|---|
| Does one session per checklist replace "one per truck"? | "I believe so"; then, asked to reconcile this with the earlier direction of a single inspection per truck (Codex review 6010089090): "One per checklist" | One session in progress per vehicle checklist. Different checklists on one truck may be in progress at the same time. This supersedes the per-truck wording in §1.1 |
| Resume limit of 12 h from the start? | "6 hours is a good default. We should make it configurable per organization." | Default 6 h, an organization setting |
| Override? (Claude recommended none) | "I agree with you." | No override |
| Requirement scope, and is place part of the requirement? | "Yes, and yes." and "Per truck (and per location if it appears in more than one place in the truck)" | Requirements are per truck. An item kept in several places on a truck has a separate requirement for each place, so a check in one place never completes another place. §5.1 |
| Should one check also complete the same checklist's earlier late obligation? | "Yes." | Uniform rule within `L` |
| Can any crew member end a checklist's open inspection? Should duplicate sessions open at deploy finish on their own? Fixed limit or setting? | "Yes, yes, and configurable" | Kept, kept, organization setting |
| If gloves are missing in cabinet 1, does only that place turn red and carry the problem, or the item on the whole truck? | "Only that place" | Attention and deficiencies are per item-place (§5.1) |
| Should an inspection still in use end when it reaches the resume limit? | "Yes, end it" | An Open session ends at `R` even if it is active, so an inspection lasts at most the limit (SES-8) |
| Is 1 to 24 hours, in whole hours, the right range for the setting? | "Yes, 1–24 hours" | SES-8 range |
| When an item moves while a check is due, does a check at its new place complete it? (Codex finding 4656644872 on #255) | "Yes, same stock" | A move keeps the item requirement; a check at the new place completes its open obligations; history keeps each place (CHK-11, SCH-6) |

These are decisions of direction, not approval of the SPEC text in §6, which §25.4(e) requires separately. The model for place-specific requirements (§5.1) is Claude's reading of the owner's answer and is confirmed with the text.

## 2. How sessions and checklists work today (SPEC 5.0.0, main `cb45f27`)

### 2.1 Session rules in the SPEC

- **§3.3:** all four roles may open or join a session. Only a recorded participant may reopen an ended session or end an open one.
- **§6.2:** "Multiple sessions on the same vehicle may be open concurrently [C]". A separate session "is allowed even when another session is open". A nonparticipant may start a new session instead of reopening.
- **SES-1, SES-2:** after 6 hours with no persisted activity, a session closes as Incomplete. Reopening starts a new 6-hour window.
- **SES-3:** a participant may reopen a Closed or Incomplete session at any time.
- **SES-4, SES-5, PRN-1, AC-2:** sessions never decide compliance.
- **SES-6:** the auto-close is stamped at last activity plus 6 h.
- **CHK-4:** a session keeps the checklist version it opened with.

### 2.2 Session code

| Action | Where | What happens |
|---|---|---|
| Start or join | `POST /api/v1/inspection-sessions` → `open_or_join_session` ([cya/inspections/commands.py:162](../../cya/inspections/commands.py)) | Takes the vehicle row lock. With `separate: false`, it joins the newest Open, non-idle session on the vehicle **whatever checklist was requested**. Otherwise it opens a new session pinned to the current version. JOIN does not reset the activity clock. |
| Separate | same, `separate: true` | Always opens another session. |
| Activity | `fold_session` ([projections.py:30](../../cya/inspections/projections.py)) | OPEN, REOPEN and checks in the session count. JOIN, END, viewing and failed saves do not. |
| Idle | `is_expired` (projections.py:74); `_settle_idle` (commands.py:461) | At last activity plus 6 h, an Open session is treated as ended on read and on write. |
| Auto-close job | `close_inactive_sessions` → `auto_close_session` (commands.py:388); `deploy/rocky9/cya-sessions.timer` every 30 min, `Persistent=true` | Appends `AUTO_INCOMPLETE` at last activity plus 6 h. It is idempotent. |
| End | `end_session` (commands.py:342) | A participant appends `END_CLOSED` or `END_INCOMPLETE`. |
| Reopen | `reopen_session` (commands.py:283) | A participant appends REOPEN. There is **no age limit and no check for other open sessions**. |
| Checks in a session | `_session_for_check` ([cya/checks/commands.py:502](../../cya/checks/commands.py)); trigger in `cya/checks/migrations/0003_check_session.py` | The session must be on this vehicle and pinned version (otherwise `SESSION_MISMATCH`), the user must be a participant, and the session must be Open and not idle. |
| Vehicle page | `vehicle`, `_session_rows` ([views.py:129–193](../../cya/inspections/views.py)); `vehicle.html`; `vehicle.js` | Shows the newest Open session, a "Start a separate inspection" disclosure, and the participant's newest Incomplete session as **Resume**, however old it is. |
| Session page | `session` (views.py:275); `session.html`; `checklist.js` | An ended session shows "Reopen session". |
| Home | `in_progress` ([cya/shell/home.py:58](../../cya/shell/home.py)) | Lists the user's Open, non-idle sessions. |

All session refusals map to `409 SESSION_UNAVAILABLE` (api.py:57).

### 2.3 Items, checklists and obligations

- **CHK-1, CHK-2:** a vehicle owns one or more checklists, and one schedule slot can have several of them.
- **CHK-5:** each checklist version records its own requirements and placement for every item. Two checklists can disagree today.
- **§5.1, CHK-10:** an obligation is keyed by vehicle, item, kit instance and due instant. **Placement is not part of the key.** When two checklists require the same anchor, only one obligation is created.
- **SCH-5, SCH-6:** a check links only to the earliest-due eligible obligation. A further link needs an Administrator-defined `shared_satisfaction_rule`. The linker supports this (`choose_links`, [cya/checks/linking.py:172](../../cya/checks/linking.py)), but nothing in the application calls `configure_shared_satisfaction_rule`. The database rejects a second link without a rule (§18.1 item 2).
- **SCH-2:** a late link is made only when no obligation of the item is on time.
- **Linking ignores the checklist:** `_eligible` (linking.py:39) selects by vehicle, item, kit instance and time.
- **Attention:** the item attention state (§9) and deficiencies apply to the item on the vehicle, not to a place.

### 2.4 Gap: a shared item shows under only one checklist

The crew checklist builds one row per item, and "the first template by id wins" (`_resolved_items`, [cya/attention/checklist.py:130–157](../../cya/attention/checklist.py)). The session page shows only rows of its own checklist (`checklistRows()` in `checklist.js`). A session on the other checklist therefore cannot see the item, and the server would refuse a check from it (`SESSION_MISMATCH`).

This comes from reading the code. Plan step 2 first adds a test that confirms it.

### 2.5 What the current rules allow

- Several sessions on one vehicle, including duplicates of one checklist.
- Reopening a days-old session against an outdated version.
- Shared items being checked twice, or being invisible in one session.
- One check of an item in one place satisfying the item's obligation even if the item is also kept in another place.

## 3. Proposed behaviour in short

- **In progress** means the session is Open and has reached neither its inactivity limit (SES-1) nor its resume limit.
- **One session in progress per vehicle checklist.** Starting checklist B while a B session is in progress joins it. A second B session cannot be started, and an ended B session cannot be reopened while one is in progress. Different checklists can be in progress together.
- **One requirement per item and place on a truck.** For each truck, each item has one requirement for each place it is kept. The requirement covers quantity, kind, allowed outcomes, instructions and the like, and every checklist that lists that item in that place uses it. Checklists choose which item-places they cover, on what schedule and in what order.
- **One check completes that item-place wherever it is due.** It counts toward every unsatisfied obligation of the same truck, item-place and kit instance that the check's time allows: on time (`E ≤ t ≤ G`) or late within its limit (`G < t ≤ L`). This applies across checklists and to the same checklist's earlier late obligation. A check in one place never completes another place.
- **A shared item-place appears in every checklist that lists it**, with one shared status.
- **Resume limit.** Each session can be resumed only until its open instant plus the organization's resume limit (default **6 hours**, set by an Administrator). The value is captured when the session opens. At the limit an Open session ends, and from then on it is read-only and kept permanently. There is no override.
- Nothing is deleted or rewritten.

## 4. Resume limit: organization setting, default 6 hours from the open instant

**Setting:**

- An Administrator sets it per organization, like OQ-79's assignment limit.
- The range is 1 to 24 hours, in whole hours (owner, §1.3).
- The current value is captured on each session when it opens, as SCH-3 captures `w_late` on each obligation. Changing the setting never moves the deadline of a session already open.
- The change is audited like other administrative settings (AUD-4).

**When it starts:** `R = opened_at + resume limit captured at open`. Joins, checks and reopens never move it, because a reopen counts as activity (SES-2) and a moving window could be extended indefinitely. For sessions opened before 6.0.0, SES-9 defines an effective deadline; for every session opened afterwards it is exactly `R`.

**How the two limits interact.** An Open session ends at whichever comes first:

- its idle close, at last activity + 6 h (SES-1);
- its deadline `R`.

An ended session, however it ended, can be reopened by a participant until `R` (SES-3). The setting decides which limit usually comes first:

- **Limit of 6 h or less (including the default).** The last activity is never before `opened_at`, so last activity + 6 h is never before `R`. An Open session therefore always reaches `R` first, and the idle close never happens. Reopening is possible only for a session someone ended by hand, until `R`.

  Example with the default: a crew starts at 07:00, is called out at 07:20 and returns at 14:00. The session ended at 13:00 (`R`), so they start a new one. Nothing is lost, because their checks still satisfy obligations and show as done in the new session.
- **Limit over 6 h.** A session left idle can close before `R` and can then be reopened until `R`.

  Example with a 12 h limit: a session opened at 07:00 with no activity after that idle-closes at 13:00, and a participant can reopen it until 19:00, if no other session of that checklist is in progress. Each reopen starts a new 6-hour idle window, which never extends past `R` (SES-2).

**On reaching the limit:**

- An Open session ends as Incomplete; the job writes `DEADLINE_INCOMPLETE`.
- Before the job runs, every read and write already treats the session as ended.
- Nobody can reopen, join or record into it afterwards.
- Its page is read-only and points to the vehicle page.
- Its records are kept permanently (AUD-1, AUD-5).

**Override:** none (owner, §1.3).

## 5. Item requirements and shared completion

### 5.1 One requirement per item and place on a truck

**Model (recommended).** A **vehicle item requirement** is a stable record of one stock of an item on one truck, kept in one place at a time; the place belongs to each version, not to the identity. At any instant a truck has at most one requirement in force for one item in one place. Its versions are effective-dated and published under CHK-3. Each version holds:

- the place: a compartment or system of that truck, or **Not placed** (CHK-9);
- kind;
- expected quantity and unit;
- allowed outcomes;
- bulk-pass eligibility;
- Not-Applicable rules;
- conditional rules;
- instructions.

The rules for item-places:

- An item kept in two places on a truck has two requirements, one for each place.
- Moving the item publishes a new version of the same requirement with the new place. Adding a second place creates a second requirement.
- A move is the same stock in a new place (owner, §1.3). A check at the new place completes the requirement's open obligations, including one due when the move took effect. The obligation keeps the old place and the check records the new one. The "never completes another place" rule applies to two requirements for the same item at the same time, not to one requirement that moved.
- A truck cannot have two requirements for the same item in the same place.
- A checklist version lists requirements (item-places) with its own schedule rules and display order, and never stores quantities or other requirements of its own.

Because every checklist that lists an item-place uses the same record, two checklists can never require 4 and 6 of it. Changing 4 to 6 is one edit, effective from its instant, for every checklist on that truck.

**Why a separate requirement and not "two items":** the owner's answer keeps one item and makes the requirement specific to the place. The item record (its name and identity) stays organization-wide as today. Reports can still group by item across places and trucks.

**What becomes per place.** The place must be part of what a check and an obligation refer to, or a check in one place would complete the other. So:

- **Obligation key (§5.1, CHK-10):** `(vehicle, item requirement, kit instance, D)` instead of `(vehicle, item, kit instance, D)`. For an item with one place this changes nothing visible.
- **Check record (§7.2):** a check of an item outside a kit names the item requirement shown to the inspector, taken from the row and never inferred, as a kit presence check names its kit-list line (KDEF-12).
- **Attention and deficiencies (owner, "Only that place"):** these are per item-place.
  - A missing item in cabinet 1 turns only that item-place red. The same item in cabinet 4 is unaffected.
  - A deficiency names the item and its place, as KDEF-12 does for kit-list lines. Its verification scope (VER-3) and any check suspension (§8.9) apply to that item-place only.
  - PRN-3, PRN-6, §8 and §9 change from "item" to "item-place" (item requirement). Kit items keep their per-kit-instance targets.
  - Existing deficiencies were raised against an item. They map to the item requirement in the checklist version their originating check pinned. Where that mapping is ambiguous (the item now has several places), the deficiency is reported in the cutover dry-run and a Supervisor assigns its place before cutover. A deficiency is never silently attached to every place.

**Unchanged:**

- kit contents keep their kit definition's requirements (KDEF-1 to KDEF-12), and their place is the kit instance's;
- kit presence obligations stay per kit-list line;
- sealable cabinets (CAB-*) cover the item-places in the cabinet.

**Migration:**

- Published checklist versions are immutable (CHK-5) and keep their values for history.
- Existing obligations, checks, links and deficiencies keep their meaning: each is mapped to the requirement for the item and the place recorded in the checklist version its obligation pins, and the map is kept as §18.2 keeps the 5.0.0 map.
- The cutover creates each truck's item requirements from the checklists in force. Where two checklists place the same item in the same place with different requirements, the cutover refuses and lists the conflicts. A Supervisor first publishes matching versions.
- An item placed in different places by different checklists becomes two requirements, which is the owner's per-place rule.
- The cutover is idempotent, validates before it commits and has a dry-run. The seed data is synthetic (CHK-7).

**Alternative (smaller, not recommended):** keep requirements in checklist versions and refuse to publish disagreeing ones. This does not express "per place" in the obligation key, so it does not meet the owner's per-place answer.

### 5.2 One check completes the item-place wherever it is due

A check of item requirement Q on truck V (for kit items: item and kit instance K), recorded at server time `t`, links to **every** unsatisfied obligation of (V, Q, K) for which `E ≤ t ≤ G` or `G < t ≤ L`. This holds whichever checklist created the obligation, and includes the same checklist's earlier late obligation (owner, §1.3). For example, yesterday's 18:00 daily (late limit 07:00 today) and today's 06:00 daily are both completed by a check at 06:30. The other constraints:

- **Same due instant:** CHK-10 still creates one obligation per anchor, so there is one link.
- **Outcomes:** each linked obligation follows §5.4. A Pass or Fail satisfies all of them. Unable to Inspect satisfies none until a supervisor completes it (5.4.1). A Fail raises one deficiency for the item-place.
- **Kits:**
  - a check never links to another kit instance's obligation (owner decision 1.8.0);
  - a kit presence check links only to its own kit-list line (KDEF-12).
- **Corrections and invalidation:** these re-derive all links together (§19.1, INV-2).
- **Concurrency:** CON-3 compares against the newest effective check of the vehicle, item-place and kit instance across all its obligations.
- **Seal substitutions:** unchanged.
- **Shared-satisfaction rules (SCH-5):** no longer needed. They remain in history, and no new ones can be created; none exists today.
- **Reports:** with the uniform late rule, the earlier obligation shows "Completed late" where it now stays Overdue until missed. The RPT-5a and AC-36 counts are rechecked in step 3.

### 5.3 Display

The checklist API returns one row per (checklist, item requirement), and per kit instance for kit items. Each row shows the item-place's shared obligation and attention state. Two places of one item are two rows in their own compartments. A session shows its own checklist's rows, so a shared item-place appears in every checklist that lists it. Live updates are already per vehicle (§17.3).

## 6. Proposed SPEC text (amendment 6.0.0)

Section IDs refer to SPEC 5.0.0.

### 6.1 §3.3 permission table

**Before:**

> | Open/join an inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] any active user with an organization role may begin or join inspection work |
> | Rejoin (reopen) an ended inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] only a recorded participant may reopen; preserves original session and checklist version |

**After:**

> | Open/join an inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] any active user with an organization role may begin or join inspection work; [C: owner decision 2026-11-21, amendment 6.0.0] while a session of the chosen vehicle checklist is in progress, starting always joins it (SES-7) |
> | Rejoin (reopen) an ended inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, issue #78] only a recorded participant may reopen; preserves original session and checklist version; [amendment 6.0.0] only before its resume deadline and while no other session of the same vehicle checklist is in progress (SES-3, SES-7, SES-8) |

**New row:**

> | Set the organization's inspection resume limit | No | No | No | Yes | [C: owner decision 2026-11-21, amendment 6.0.0] SES-8; default 6 hours |

### 6.2 §4.3 checklists and §5.1 obligations

**CHK-5 before:**

> **CHK-5 [C] Checklist version contents.** A published vehicle checklist version retains its item set; each item's compartment, kit, equipment or system placement; display order; instructions; expected quantity; kind; bulk-pass eligibility; allowed outcomes; schedule rules; conditional rules; and Not-Applicable rules. …

**CHK-5 after:**

> **CHK-5 [C] Checklist version contents.** A published vehicle checklist version retains the vehicle item requirements it inspects (CHK-11), their display order and its schedule rules. Placement and every other requirement come from the item requirement's version in force at the obligation's due instant. A version published before amendment 6.0.0 keeps the values it recorded, for history and for the obligations that pin it. …

The rest of CHK-5 is unchanged.

**New CHK-11:**

> **CHK-11 [C: owner decisions 2026-11-21, amendment 6.0.0] One requirement per item and place on a vehicle.** A vehicle item requirement is a stable record of one item kept in one place on one vehicle: a compartment or system of that vehicle, or Not placed (CHK-9). Its versions record the place, kind, expected quantity and unit, allowed outcomes, bulk-pass eligibility, Not-Applicable rules, conditional rules and instructions. They are effective-dated and published under CHK-3 by the roles that publish that vehicle's checklists. An item kept in several places on a vehicle has one requirement for each place. A vehicle cannot have two requirements for one item in one place. Moving an item publishes a new version of its requirement. Every checklist on the vehicle that inspects that item in that place uses the same requirement, so two checklists can never require different things of it. Kit contents keep their kit definition's requirements, and kit presence its kit-list line (KDEF-1 to KDEF-12). Vehicle files (IMP-7) carry item requirements.

**§5.1 and CHK-10 obligation key:** replace "`(vehicle, item, kit instance, due instant D)`" with "`(vehicle, item requirement, kit instance, due instant D)`". For a kit item, the requirement is the kit definition's content entry, as today.

**§7.2 check record:** add `item_requirement_id`. It is required for a check of an item outside a kit and taken from the row shown to the inspector, never inferred (cf. `kit_requirement_id`).

### 6.3 §5.3 and §5.5 linking

**SCH-2 before:**

> … It may be linked to an obligation that is still unsatisfied and past `G` only when no unsatisfied obligation of the same vehicle+item has an on-time window containing `t_c` (SCH-6 ordering applies among overdue ones: earliest-due first). …

**SCH-2 after:**

> … It is linked to every obligation of the same vehicle and item requirement that is still unsatisfied, past `G` and not past its `L`, together with every on-time one (SCH-6). …

**SCH-5 before:**

> **SCH-5 [P]** Link cardinality: `check_obligation_link` rows are unique per `(check_id, obligation_id)`. By default a check has **exactly one** link. A check may have more than one only when each additional link carries the `shared_rule_id` of an administrator-defined, audited rule that explicitly declares the sharing (SCH-6); a link without a `shared_rule_id` is rejected if the check already has a link. One check never silently counts toward more than one obligation.

**SCH-5 after:**

> **SCH-5 [C: owner decision 2026-11-21, amendment 6.0.0] Link cardinality.** `check_obligation_link` rows are unique per `(check_id, obligation_id)`. A check links to every obligation SCH-6 makes eligible, and each link records whether it was on time or late. Shared-satisfaction rules recorded before 6.0.0 remain in history; no new rule can be created.

**SCH-6 before:**

> **SCH-6 [P]** When a check is recorded and several unsatisfied obligations for the same vehicle+item are eligible (windows containing `t_c`, possible only with overlapping schedules; or late-linkable ones under SCH-2), the check links to the earliest-due one. A shared-satisfaction rule (e.g. a shared-parent item) may additionally link it to the others it names. …

**SCH-6 after:**

> **SCH-6 [C: owner decisions 2026-11-21, amendment 6.0.0] One check completes the item-place wherever it is due.** A check links to every unsatisfied obligation of the same vehicle and item requirement (CHK-11) for which `E ≤ t_c ≤ G` or `G < t_c ≤ L`, whichever vehicle checklist created it, including an earlier obligation of the same checklist. Eligibility depends only on obligations and time, never on which sessions are open (§6.1). A check never links to another item requirement's obligation, so a check in one place never completes another place. Invalidating or correcting the check re-derives all its links together (§19.1). For an item inside a kit only the named kit instance's obligations are eligible. A kit presence check is eligible only for its own kit-list line (KDEF-12). …

### 6.4 §6.2 States

**Before:**

> | **Incomplete** | Auto-closed by inactivity (§6.3), or manually ended with unmet items. Progress preserved. |
>
> Reopening moves Incomplete (or Closed) → Open and is an audited event [C]. Multiple sessions on the same vehicle may be open concurrently [C: concurrent crew members]. **[P]** An active user with an organization role joins an existing open session for the vehicle by default; starting a separate one is allowed even when another session is open. **[C: owner decision 2026-10-24, issue #78]** Only a participant may reopen that same ended session or manually end it; a nonparticipant may start a new session instead.

**After:**

> | **Incomplete** | Auto-closed by inactivity (§6.3), ended at its resume deadline (SES-8), or manually ended with unmet items. Progress preserved. |
>
> A session is **in progress** while it is Open and has reached neither its inactivity limit (SES-1) nor its resume deadline (SES-8). Reopening moves Incomplete (or Closed) → Open, within SES-3, SES-7 and SES-8, and is an audited event [C]. **[C: owner decisions 2026-11-21, amendment 6.0.0]** Each vehicle checklist has at most one session in progress (SES-7). Sessions of different checklists may be in progress together, and an item-place they share is completed by one check (SCH-6). **[C: owner decision 2026-10-24, issue #78]** Only a participant may reopen that same ended session or manually end it. A nonparticipant joins the session in progress for that checklist, or starts a new one when none is in progress.

### 6.5 §6.3 (retitled "Inactivity auto-close, resume deadline and one session per checklist")

SES-1, SES-4, SES-5 and SES-6 are unchanged.

**SES-2 after** (appended): "…; that window never extends past the session's resume deadline (SES-8)."

**SES-3 after:**

> **SES-3 [C: owner decisions 2026-10-24, issue #78, and 2026-11-21, amendment 6.0.0]** A participant may reopen a Closed or Incomplete session without supervisor approval, before its resume deadline (SES-8) and only while no other session of the same vehicle checklist is in progress (SES-7). The reopen is audit-logged (actor, instant, session). A refused reopen changes nothing and is audited as a refused command. A nonparticipant joins the session in progress for that checklist, or starts a new session when none is in progress.

**New SES-7:**

> **SES-7 [C: owner decision 2026-11-21, amendment 6.0.0] One session in progress per vehicle checklist.** For each vehicle checklist (its identity, not a version), at most one session is in progress. Starting that checklist while one is in progress joins it, even when it pins an earlier version (CHK-4). Nobody can start a second session of that checklist or reopen another one of it. Any participant may end it (§3.3). Starting a different checklist on the same vehicle is unaffected. Session commands for one vehicle are serialized: two simultaneous starts of one checklist produce one session, and a start racing a reopen leaves exactly one in progress.

**New SES-8:**

> **SES-8 [C: owner decisions 2026-11-21, amendment 6.0.0] Resume limit.** Each organization has a resume limit that an Administrator sets in whole hours from 1 to 24 (default 6). The current value is captured on each session when it opens, so a later change never moves an existing session's deadline. The session's resume deadline is `R = opened_at + captured limit`. Joins, activity and reopens never move it. From `R` on, the session cannot be reopened, joined or recorded into by anyone. For a session opened before 6.0.0, read `R` as its effective deadline `R*` (SES-9). If it is still Open, it ends as Incomplete at `R` even if it is in active use, recorded under SES-9. Its records are kept permanently and stay readable (AUD-1, AUD-5). Work continues in a new session, which pins the current version (CHK-4) and shows earlier checks through current obligations (SES-4, SES-5, PRN-1). There is no override.

**New SES-9:**

> **SES-9 [P] Enforcement and recording.** Let `A` be the session's last persisted activity, and let the **effective deadline** be `R* = max(R, A)`.
>
> - For a session opened under 6.0.0, no activity can be recorded at or after `R`, so `A < R` and `R* = R`.
> - For a session opened before 6.0.0, activity recorded after `R` under the earlier rules remains valid history, and `R*` is the later of `R` and that activity.
>
> **One instant for every purpose.** The session's end instant is `min(A + 6 h, R*)`. Reads, refusals, the projection, the database triggers and the job all use this same instant:
>
> - The session is in progress only while it is Open and the current time is before its end instant.
> - A participant may reopen an ended session only before `R*`.
> - No check, join or reopen is accepted at or after `R*`.
>
> **Recording.** Enforcement never waits for the job. The idempotent, restart-safe job (ARC-4) appends the ending event at exactly that end instant, never at the time it runs:
>
> - `AUTO_INCOMPLETE` if `A + 6 h ≤ R*` (SES-6);
> - otherwise `DEADLINE_INCOMPLETE` at `R*`.

### 6.6 §6.5 Views

**New VW-5:**

> **VW-5 [C: owner decision 2026-11-21, amendment 6.0.0]** An item requirement appears in every vehicle checklist that lists it, in that checklist's order. Its obligation and attention state are shared by all of those checklists, so a check recorded in one session shows in the others without a refresh (§17.3). An item kept in two places appears as two rows, each in its own place.

### 6.7 Other sections

- **§8, §9, PRN-3, PRN-6 (owner decision 2026-11-21, "Only that place"):** for items outside kits, the attention state and deficiency target are the item requirement (item and place), not the item on the vehicle. VER-3 scope and §8.9 suspension follow the same target. This changes §25.5 principle wording, so the owner's approval of the 6.0.0 text must name it explicitly.
- **§17.2 ARC-4:** cite SES-9.
- **§18:**
  - `session_event` lists "close (auto inactivity / resume deadline / manual)";
  - `inspection_session` gains the captured resume limit;
  - add `vehicle_item_requirement` and its versions (V/A);
  - `check_record` gains `item_requirement_id`;
  - `shared_satisfaction_rule` is history only.
- **§18.1 item 2 after:** "`check_obligation_link` unique per `(check_id, obligation_id)`; every link of one check is to an obligation of the same vehicle, item requirement and kit instance (SCH-6)."
- **AC-6:** add "before its resume deadline and while no other session of the same checklist is in progress".
- **AC-30 after:** "**Shared completion [C].** With two overlapping-window obligations of one item requirement from two checklists, one Pass links to both; with one of them past `G` but not past `L` (from either checklist, or the same one), it links to both, one late; an obligation past its `L` is not linked; a check of the item in place P1 never links to the place-P2 requirement; a check of kit instance K never links to K2; invalidating the check voids all its links."
- **New AC-41 (sessions and limit):**
  1. With session S of checklist A in progress on V, a start of A by any role joins S, while a start of checklist B opens or joins a B session.
  2. Two simultaneous starts of A produce one session.
  3. A participant's reopen of an ended A session is refused while S is in progress, and succeeds after S ends, before its own deadline.
  4. With the default 6-hour limit, at `opened_at + 6 h` S reads as Incomplete without the job; reopen, join and check are refused; the job later records `DEADLINE_INCOMPLETE` at that instant, once.
  5. Changing the setting to 8 h leaves S's deadline at 6 h, and a session opened afterwards gets 8 h.
  6. After the deadline, a new session shows S's checks through current obligations, and S's records are unchanged.
- **New AC-42 (item-place requirements and display):**
  1. Publishing cannot give one item-place two different requirements.
  2. An item-place listed by checklists A and B appears in both sessions with one state, and a Pass in A shows as done in B within §17.3's targets.
  3. An item kept in cabinets 1 and 4 has two rows and two obligations; a Pass in cabinet 1 leaves cabinet 4 due.
  4. Changing the quantity of an item-place changes it for both checklists from its effective instant, and earlier obligations keep their pins.
- **§24.3:**

  > | OQ-81 | How long may an incomplete inspection session be resumed? | **Decided** (owner, 2026-11-21) | 6.3 (SES-8) | Organization setting, 1–24 whole hours, default 6 h from the open instant, captured per session; an active session also ends at the limit; no override | Pending text approval |
  > | OQ-82 | One session in progress per vehicle or per vehicle checklist? | **Decided** (owner, 2026-11-21) | 6.2, 6.3 (SES-7) | Per vehicle checklist, with shared completion (SCH-6) | Pending text approval |
  > | OQ-83 | Where do an item's requirements live? | **Decided** (owner, 2026-11-21) | 4.3 (CHK-11) | Per vehicle and per place | Pending text approval |
  > | OQ-84 | Are attention and deficiencies per item-place? | **Decided** (owner, 2026-11-21) | 8, 9, PRN-3, PRN-6 | Yes: only that place turns red and carries the problem | Pending text approval |
  > | OQ-85 | Does one check also complete the same checklist's earlier late obligation? | **Decided** (owner, 2026-11-21) | 5.3, 5.5 (SCH-2, SCH-6) | Yes, within `L` | Pending text approval |

- **§26.3, §27, §25.6 and the header:** as for any amendment. The OQ-03 row cites SCH-6 as revised.
- **§15.3, OQ-44:** unchanged.

## 7. Enforcement, scheduler, audit and migration

**Sessions.**

- **Read and write time:** a pure function computes from history and the captured limit whether a session is in progress and, if not, which limit ended it.
- **Callers:** joins, reopen and end (which write the ending event first), `_session_for_check`, the vehicle and session pages, and home.
- **Database backstops:**
  - the check trigger refuses session checks at or after `R*`, computed from the session row's captured limit and its recorded activity, by the same formula as the reads (SES-9);
  - the `session_event` trigger refuses JOIN and REOPEN at or after `R*`. It requires the ending event's instant to equal the SES-9 end instant, and requires a service receipt;
  - the `session_event` trigger refuses OPEN or REOPEN while another session of the same vehicle checklist is in progress, computed from history.
- **Captured limit:** the header row is immutable, so it can never change after the session opens. The insert trigger checks that it equals the organization setting in force at the open instant.
- **Scheduler:** `close_inactive_sessions` on the existing 30-minute timer adds deadline closes, an `expired=` count and `--dry-run`. With a 6-hour limit, a session ends up to 30 minutes before the job records it. Enforcement never waits for the job, and a missed run or a restart only delays the audit event.
- **Audit:** `DEADLINE_INCOMPLETE` is written by `worker.session_auto_close` under command type `SESSION_DEADLINE_CLOSE`. Refusals are audited by `run_command`. A setting change is an audited, versioned administrative change (AUD-4).
- **API codes:**
  - `409 SESSION_IN_PROGRESS`, with the session ID;
  - `409 SESSION_RESUME_EXPIRED`;
  - `409 SESSION_NOT_OPEN`.

**Linking.** Linking runs under the vehicle and item lock (CON-5); the lock key becomes the item requirement. The second-link constraint is replaced by a trigger requiring every link of one check to share its vehicle, item requirement and kit instance. Each link records on time or late. Checks recorded before the cutover are not re-linked (SCH-2).

**Migration of existing data.** No row is edited or deleted.

- **Sessions:**
  - each existing session's captured limit is set to 6 h by migration, as 1.31.0 gave existing obligations `w_late`;
  - ended sessions older than the limit cannot be reopened;
  - an Open session whose end instant (SES-9) has passed reads as ended at that instant, and the first job run records its ending event at that same instant. For an older session with activity after `R`, the end instant is its last activity. That activity remains valid history and stays linked as it was;
  - duplicate sessions of one checklist open at deploy finish on their own within 6 h (owner, §1.3);
  - reversal stops if any `DEADLINE_INCOMPLETE` exists.
- **Item requirements:** the cutover in §5.1 builds the requirements and the map from old (item, place in pinned version) to requirement. It refuses on conflicts with a list and is idempotent. Existing obligations, checks, links and deficiencies keep their rows; readers use the map, as §18.2 does for 5.0.0.
- **Before deploy:** run both dry-runs, and deploy at a quiet time.

**API compatibility.** `separate: false` stays accepted for one release. `separate: true` is refused with `SESSION_IN_PROGRESS` while that checklist has a session in progress. A check request without `item_requirement_id` from a page loaded before the deploy is refused with a reload message, never guessed.

## 8. Implementation plan

Each step is a separate draft PR, reviewed by Codex under `docs/review.md`. Steps 3 to 6 change migrations, concurrency or data integrity, so they need cross-family review. Implementation starts after the owner approves the SPEC text and an issue is opened.

### Step 1: SPEC amendment 6.0.0 (Markdown only)

Apply §6 as approved, and add AC-41 and AC-42 to `tests/TRACEABILITY.md`. Verification: the documentation-only gate.

### Step 2: shared items appear in every checklist (VW-5, first part)

**Code:** rows per (checklist, item) in `cya/attention/checklist.py`; the row key in `checklist.js` includes the checklist version.

**Tests:**

- First, a test showing the current gap.
- After the fix, the shared item shows in both sessions with one state.
- A check from the second session is accepted and shows in the first.
- Order stays within each checklist.
- The query count is bounded.

### Step 3: one check completes every eligible obligation (SCH-2, SCH-5, SCH-6, at item level)

**Code:** `choose_links` links every on-time and late-eligible obligation; new shared rules are refused; the second-link trigger is replaced; CON-3 compares across obligations.

**Interim guard (removed in step 4):** links are made only between obligations whose pinned requirement versions have equal check properties and the **same placement**. A check in one place therefore never completes another place even before CHK-11.

**Tests:**

- Two checklists with overlapping windows give two links.
- On time plus late within `L`, from another checklist or the same one, gives two links; past `L` gives none.
- Different placements are not linked.
- Kit instances and presence lines stay separate.
- Unable to Inspect satisfies neither.
- Correction and invalidation re-derive or void all links.
- CON-3 fires across sessions.
- Replay returns the same links.
- The trigger refuses a link to another item.
- AC-36 and RPT-5a counts.

### Step 4: item requirements per truck and place (CHK-11)

This is the largest step. It covers:

- `vehicle_item_requirement` and its versions;
- the obligation key and generator moving to the requirement;
- `item_requirement_id` on checks;
- per-place attention and deficiencies, including verification scope, suspension, and the cutover report of deficiencies whose place is ambiguous;
- the editor editing requirements once per truck and place;
- vehicle files (IMP-7, format version bump);
- the cutover with dry-run, conflict report, map and idempotent replay;
- step 3's guard removed;
- docs: `checklists-implementation.md`, `configuration-management.md`, `vehicle-files.md`, `check-records.md`, `obligation-generation.md`.

**Tests:**

- Two checklists cannot disagree on one item-place.
- One edit changes both checklists from its effective instant.
- An item in cabinets 1 and 4 has two obligations, and a cabinet-1 Pass leaves cabinet 4 due.
- Moving an item keeps its history.
- The cutover refuses on a conflict, splits an item placed differently by two checklists into two requirements, and replays idempotently.
- Old checks and deficiencies read the same through the map.
- Export and import round-trip.
- A check without a requirement ID is refused.
- A Fail in cabinet 1 turns only cabinet 1 red and opens a problem naming that place; cabinet 4 stays clear.
- Verification and suspension of that problem apply to cabinet 1 only.
- An existing deficiency with an ambiguous place blocks the cutover until it is assigned.

### Step 5: one session in progress per vehicle checklist (SES-7)

This is as revision 2 step 5.

**Code:** `in_progress_session(org, vehicle_id, template_id, now)`, joins, refusals, API codes, the trigger backstop, the vehicle page with one entry per checklist in progress and no separate control, `checklist.js`, docs.

**Tests:**

- Rewrite: `test_open_join_and_explicit_parallel_session`, `test_new_session_can_start_while_another_is_open_closed_or_incomplete`, `test_default_join_is_vehicle_scoped_even_when_requested_template_differs`, `test_separate_always_opens_another`, `test_an_open_inspection_is_explicitly_joinable_and_separate_start_is_explained`.
- Same-checklist join, including on an older pin.
- Different-checklist start.
- Refused separate starts and reopens.
- Threaded races.
- Legacy duplicates.
- The trigger.
- The vehicle page.
- Browser smoke.

### Step 6: resume limit (SES-8, SES-9)

**Code:**

- the organization setting (Administrator only, 1 to 24 h, default 6, audited);
- the captured limit on the session header (migration sets 6 h on existing rows);
- `DEADLINE_INCOMPLETE` and its constraints;
- `time_limit()`;
- refusals;
- the job with `--dry-run`;
- triggers;
- a read-only expired page;
- the home filter;
- the settings page;
- runbook and docs (run `test_asgi_server`).

**Tests:**

- At `R − 1 µs` a check, join and reopen succeed; at `R` all are refused, before the job runs.
- A setting change does not move an open session's `R`, and a new session gets the new value.
- Out-of-range values are refused; a non-Administrator is refused and audited.
- The header trigger refuses a captured value that does not match.
- With a 12 h setting, idle at 6 h then a reopen at 8 h ends at 12 h.
- The job stamps `R` when late, replays idempotently, and skips sessions that changed.
- A legacy session with a check after its `R`:
  - reads as ended at that check's instant (`R*`);
  - the projection and the job's `DEADLINE_INCOMPLETE` use the same instant;
  - a reopen or check after it is refused;
  - its existing checks and links are unchanged.
- The database refuses a raw late check, JOIN, REOPEN or a wrong deadline event.
- The old records stay byte-identical.
- The expired page has no Reopen.
- `--dry-run` writes nothing.
- The projection rebuild matches.
- Reversal refuses.

### Step 7: deploy notes

Record both dry-runs and the quiet-time recommendation in the runbook. Rehearse on the Ubuntu VM only if the owner asks; the Rocky VM is reserved for issue #70. Deployment needs the owner's authorization.

**Ordering:**

- Steps 5 and 6 touch the same session files and run one after the other.
- Step 2 is independent of the session steps.
- Step 3 lands before step 4 because of its guard.
- Step 4 is the long pole; steps 5 and 6 do not depend on it.

## 9. Open questions for the owner

**Answered 2026-11-21** (§1.2, §1.3):

- "active" means open requirements on the vehicle;
- requirements are per truck and per place;
- late obligations are completed within `L`, including the same checklist's;
- one session per vehicle checklist (to confirm with the text);
- the resume limit defaults to 6 h and is set per organization;
- there is no override;
- any participant may end a session;
- duplicates open at deploy finish on their own;
- attention and problems are per place;
- a session still in use ends at the limit;
- the setting range is 1 to 24 whole hours.

**Still open:** no direction questions remain. The owner confirmed one session per checklist over one per truck. What remains is the owner's explicit approval of the concrete SPEC 6.0.0 text (§25.4(e)), naming the change to PRN-3 and PRN-6 (§25.5), and confirmation of the §5.1 model.

## 10. Not changed

The following are unchanged:

- item-level compliance (PRN-1, AC-1, AC-2), now at item-place level;
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
