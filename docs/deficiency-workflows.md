# Crew and supervisor deficiency workflows

[issue #86](https://github.com/JustApu/CheckYourAmbulance-archive/issues/86) and
[issue #15](https://github.com/JustApu/CheckYourAmbulance/issues/15) and
[issue #16](https://github.com/JustApu/CheckYourAmbulance/issues/16). Non-normative implementation
notes for SPEC §3.3, §5.4.1, §7.7–§7.8 and §8.3–§8.9.
The [crew inspection screen](crew-inspection-ui.md) remains the live shared checklist.
All mutations use the existing audited [check](check-records.md) and
[deficiency](deficiencies.md) command endpoints; these pages add no tables or write commands.

## Pages and access

| Route | Purpose |
|---|---|
| `/deficiencies/` | The 100 most recent reports, including cancelled history; active vehicle links to independent reporting. |
| `/deficiencies/queue/` | Supervisor queue (Supervisors and Administrators): unassigned, overdue and waiting extension requests. |
| `/deficiencies/{id}/` | Report details, deadlines and age, lifecycle history, crew and supervisor action forms, check suspensions, verification scope and informational links. |
| `/deficiencies/{id}/verification/` | The existing checklist screen in verification mode, without creating a session. |
| `/deficiencies/{id}/verification-items/` | Read-only JSON: current outstanding scope rows available on the vehicle checklist, deficiency status and unavailable-row count. |
| `/inspection/vehicles/{id}/deficiencies/new/` | Independent item, compartment or selected vehicle-systems report. |
| `/inspection/vehicles/{id}/check-history/` | Complete chains for the 200 most recent check entries recorded in the last 14 days; own corrections, administrator invalidation and supervisor Unable completion. |

Pages require an active signed-in user with an organization role, like the shell. Missing,
malformed and other-organization resources answer 404. A user without a permitted role gets
an audited denial. History remains readable after vehicle deactivation; new independent
reports require an active vehicle. History pages are snapshots at page load: refresh to see
concurrent changes, and stale correction tips are rejected by the command rather than overwritten.

Read models cap chain/link evidence at 10,000 rows, deficiency events and link history at 1,000,
and each report-option revision collection at 1,000. Exceeding a complete-history cap answers
503 rather than showing an incomplete result. The index shows up to 100 reports and up to 200
active vehicles. Larger historical browsing/pagination is not implemented here.
Unavailable evidence or configuration returns a sanitized 503 page: independent reporting
uses “Reporting temporarily unavailable”; history pages keep “History temporarily unavailable”.

## Failed and Unable checks

The checklist offers only the outcomes allowed by the item definition. A mismatching quantity
opens the Failed flow before sending; explicit Fail can also describe a condition failure at
a matching count. Measurement Fail retains the measured value and unit. Unable sends no
quantity or measurement and requires a nonblank reason.

The modal lists possible matches from the existing scoped suggestion endpoint, with no
match automatically selected. The reporter chooses an existing active deficiency or a new
report. A new report requires category, summary, description and severity. Every offered severity
shows its current label and concise criteria; retired revisions are not offered. A failed
lookup is labelled unavailable, never as “no matches”. Report choices failing to load on the
session page leave a visible warning; ordinary Pass and selection of an existing report can
still work. The check and selected/created deficiency commit atomically through `POST /api/v1/checks`.
Kit-instance checks keep their `kit_id`; no other instance is satisfied by that check.

The full request is frozen for Retry. A conflict Proceed creates a new operation id while
preserving the entire outcome/reason/observation/deficiency payload and the warned-about check
as both basis and acknowledgement, including if a live reload removed the row. A further
check produces another warning. Simultaneous warnings for different in-flight items are queued,
so a Failed request cannot be overwritten by another dialog. A hidden row announces the acknowledged outcome in the
status line. Fresh actions on an Unconfirmed item are disabled until its Retry resolves.

The checklist JSON adds `obligation_outcome` so Failed satisfaction is labelled
“Inspected — failed” and a supervisor completion is labelled
“Completed by supervisor — unable to inspect”, distinct from Passed.

## Independent reports and informational links

Independent reports resolve their affected scope on the server. Item reports are not tied to
one kit instance and therefore apply across instances; use the specific kit's Fail/Unable
flow for an instance-scoped report. Independent kit-wide reports name a specific physical kit
instance. Session-linked independent reports remain unsupported by the existing command; the UI does not send a session id.

Related reports are explicit multi-select choices; they are never automatically linked.
Adding or removing a link only changes that informational relationship. It does not merge,
close, reassign or transfer responsibility for either report. This UI creates unassigned
reports. Assignment, deadline decisions and explicit closure screens are separate work;
crew extension requests, dependency updates, notes, repairs and verification are described below.

## Corrections, invalidation and supervisor completion

History retains every row of the selected chains with Effective, Superseded or Invalidated
state. Only the author gets Correct entry. It is labelled as fixing accidental data entry,
never repair or verification. The form sends the expected current tip and only correction
outcomes the existing command supports: Passed, plus Unable-to-Inspect to Failed. Quantity
and measurement corrections take an explicit corrected observation. A stale tip is a refusal
requiring a refresh. Other users have a link to start/join an inspection and record a fresh
independent check instead.

Only administrators get another author's chain-invalidation control. The explanation is
mandatory. Invalidation excludes the entire chain without reviving any earlier result and
does not close a deficiency; invalidating a chain that cancelled one reopens it under INV-7.
Reported in Error and reopening remain visible in deficiency lifecycle history.
Lifecycle events use readable labels: Reported, Reported in Error, Reopened, Severity changed,
and “Entry corrected — supervisor review needed” for a notice that leaves the deficiency open.
The event label and resulting status are shown separately; descriptions and explanations stay escaped.

Supervisors and administrators get an explanation-required completion control for linked,
effective Unable checks. The command rechecks eligibility/current selection/satisfaction;
the UI cannot grant authorization. Completion never becomes Passed and leaves the deficiency
open. Self-completion is flagged. Administrators may invalidate a current supervisor completion
with an explanation. Correction or invalidation of its supporting check keeps the existing
server-side supersession/voiding semantics. Linked-obligation determination history remains
visible after invalidation or supersession; current controls follow the supersession chain,
not UUID ordering for events that share a timestamp.

## Crew deficiency actions (issue #15)

The detail page shows the assignee (or Unassigned), base deadline, governing deadline,
overdue state, age in whole days since the original report, extension count and the configured
long-running highlight. It reads these facts through the existing bounded deadline/dependency
readers, and retains every assignment, date, explanation, extension decision and lifecycle event.
Closed, reopened and Reported in Error states keep their existing readable labels. Missing
projection/history/scope evidence fails closed with the sanitized unavailable page.

The current permission policy and server-loaded category/assignee control form visibility:
Crew, Supervisors and Administrators may request an extension, update a dependency and add
notes; a Supply Officer alone may request an extension only when assigned, and update
dependencies/notes only on supply-related deficiencies. Roles combine. All four roles may
record a repair while In Progress. Closed and Reported in Error deficiencies have none of
these mutation forms. Every existing command independently rechecks roles and state; a forged
request cannot gain permission from the page.

Extension requests require a reason and proposed date. Dependency entry/revision requires an
explanation and optionally a future follow-up instant; omitting it on revision clears it.
Dependency entry is offered in Open/In Progress; while waiting, revision and exit are offered.
Exit can name Open/In Progress or return to the status before waiting. Progress notes require
nonblank text. Date controls are explicitly labelled UTC and sent as UTC instants. Follow-up
never schedules an automatic reminder. Each lifecycle form sends the event seen at page load;
a concurrent change or another successful form on the same page requires refreshing before a
new decision (`DEFICIENCY_CHANGED`). The backend's refusal is shown, including empty repair
scope, absent deadline, invalid dates, transition changes and permission denial.

Pending Verification shows each materialized item/physical-kit row, its required-after instant
and Verified/Outstanding status, using the command layer's effective-Pass and scope-lineage
reader. “Perform verification checks” opens the shared checklist renderer and script. Its
read-only list refreshes on vehicle hints, reconnect and tab visibility just like the crew
checklist. It offers the outstanding rows from every applicable current checklist version,
without the routine Needs Attention Only filter or session/odometer controls. A scoped item
removed from the current checklist stays outstanding and is reported as unavailable; the
screen directs the user to a supervisor rather than claiming the scope is complete. Vehicle
reading rows also explain that an item check cannot verify them; they remain outstanding.

Each check sends `purpose=VERIFICATION` and `verification_for_deficiency_id`, with its current
checklist version and physical `kit_id` when present. Pass and Fail use the ordinary observation
controls, save states and conflict protocol; Unable to Inspect is not offered. A Fail directly
adds evidence to the verified deficiency, without a new-report/match dialog. Routine Passes
never count toward this scope. A committed verification response reports whether it counts,
how many scope rows remain, automatic closure, a return to In Progress after Fail, or withheld
automatic closure requiring supervisor attention. Saved means the check committed, not that
the deficiency necessarily closed.

Unconfirmed verification rows remain reachable for Retry even if a refresh removes them from
the outstanding list or the last check closes the deficiency. Retry keeps the exact payload
and operation id; conflict Proceed keeps the verification purpose, deficiency, observations,
physical kit and warned-about basis with a new operation id. Deficiency state and scope are
rechecked by the existing command on every check; stale screens cannot force acceptance.

## Supervisor and administrator actions (issue #16)

The deficiency page adds a **Supervisor actions** group for the M4 commands that need a supervisor
or administrator. Each form posts to the existing audited endpoint with the event seen at page load,
so a concurrent change answers `DEFICIENCY_CHANGED` and asks for a refresh. The page decides only
what to offer; the command rechecks the permission, the status and every choice.

| Form | Offered when (permission, status) | Fields |
|---|---|---|
| Assign, Set deadline | `assign_deficiency_deadline` (Supply Officer only on supply-related deficiencies); active | Assignee from active Crew, Supervisor and Supply Officer users; deadline in UTC |
| Start work | same; Open | none |
| Decide extension request | `decide_extension`; active; one form per request still pending under the current deadline | Approve, approve a different date (UTC) or deny; explanation |
| Acknowledge escalation | `acknowledge_escalation`; active | Explanation, action plan, period in days up to the administrator maximum (the form says when none is set) |
| Re-grade severity | `regrade_deficiency_severity`; active | Current severity levels with their criteria; explanation |
| Expand verification scope | `expand_verification`; Pending Verification | Items and physical kits from the vehicle's current checklist; explanation |
| Add verification after closure | `expand_verification`; Closed Verified | Items and physical kits, a verification deadline (UTC) and an explanation; it reopens the deficiency |
| Suspend routine checks | `suspend_routine_checks`; active | Items and physical kits within the deficiency's current affected scope; optional explanation |
| Close without verification | `explicit_deficiency_close` (Administrator); active | Explanation |
| Reopen | `explicit_deficiency_close` (Administrator); Closed by Administrator or with no current verification items | Explanation |

Roles combine, so a Crew member who is also a Supervisor sees both groups. A forged request from a
role the page hid is refused by the command (403); the tests post a forged crew extension
decision and a forged supervisor reopening.

**Check suspensions.** The page lists every suspension of the deficiency as "Known Deficiency —
Check Suspended" with its start, explanation and, once ended, when and by which event (SUS-1,
SUS-4). There is no "end suspension" control: SPEC §8.9 ends suspensions only automatically, when
the deficiency is verified closed, closed by an administrator or Reported in Error (SUS-3, SUS-4),
and the server has no other way to end one. Closing the deficiency is how a supervisor ends them.

**Supervisor queue** (`/deficiencies/queue/`, linked from the index for those who may decide
extensions). Three lists of deficiencies still being worked, 200 each at most (the heading shows
`+` when cut short): unassigned, oldest report first; overdue by the deadline in force (the same
governing-deadline reader the overdue notice job uses, so an approved extension that has not
passed is not overdue), oldest deadline first; and those with an extension request still waiting
for a decision, oldest request first. Each list is selected by the database query itself (for the
last: no decision answers the request and no later deadline revision superseded it, the rule the
deadline replay applies), so rows that no longer qualify can never fill the 200 ahead of ones that do. Others get the audited 403 of a refused section. The page is a snapshot; refresh
to see changes. It adds no business rule.

History labels now cover every event type, including "Escalation acknowledged" with its action
plan and acknowledged-until time.

`cya/inspections/tests/test_supervisor_ui.py` covers form visibility by role, role union, supply
context and status; assignment, deadline, extension decision (including a forged crew decision),
acknowledgement with the maximum, re-grade, suspension (scope choices, repeat refusal),
expansion while Pending Verification (adding verification after closure is covered by the
harness and the existing command tests), administrator close and reopen (including a forged
supervisor reopen), escaping, and the queue's access and three lists. The Node harness covers each
new form's payload, UTC conversion, required fields and refusal messages.

## Save feedback and verification

Workflow forms send same-origin JSON with CSRF headers. Each form permits one request at a
time. Only a `COMMITTED` response shows Saved. Definite refusals show a reason; ambiguous
transport/5xx/invalid JSON is Unconfirmed. Unconfirmed forms lock their fields and offer Retry
with the identical request and operation id. After acknowledgement the form is single-use;
a link reloads current history, avoiding stale-tip resubmissions.

`cya/inspections/tests/test_deficiency_actions_ui.py` covers the new crew forms, role/context
visibility, stale requests, extension replay, kit scope, routine-pass isolation, verification
closure/Fail and unavailable evidence. `cya/inspections/tests/test_workflow_ui.py` drives
authenticated synthetic HTTP requests against real audited commands: report replay, explicit link/unlink, own correction, administrator
invalidation, supervisor completion, denied forged requests, tenant boundaries, labels,
escaping, unavailable evidence and configuration failures. The existing crew and attention
suites cover the underlying shared UI contracts.

`cya/inspections/tests/workflow_script_harness.js` executes the actual scripts with a synthetic
DOM and deferred fetch. It covers explicit matches, reasons, kit payloads, frozen retries,
full Failed/Unable and verification conflict payloads, removed rows, state labels, UTC form
payloads, closure/failure/withheld feedback, and single-flight forms. Run
it with `node cya/inspections/tests/workflow_script_harness.js`; the Django wrapper runs it
when Node is installed and explicitly skips it otherwise. Node is optional developer tooling,
not a new application dependency or build step. No network or external services are used.

[issue #141](https://github.com/JustApu/CheckYourAmbulance-archive/issues/141)'s CI assessment confirmed that the Django wrapper was skipped in the pinned Python
container during [PR #165's handoff run](https://github.com/JustApu/CheckYourAmbulance-archive/actions/runs/40617552539)
because Node was unavailable. The direct command above passed locally with Node 22.23.4.
The smallest proposed CI follow-up is to provision a pinned Node runtime in the existing
stack-neutral job and run that command as a required, non-skipping step. It needs no browser,
npm packages or application build. That separately scoped workflow proposal is not implemented
by this housekeeping batch; the optional Django-wrapper skip remains unchanged.

Not browser-verified: a real DOM/browser rendering or input enforcement, a real server with
WebSocket reconnect, devices, screen-reader/contrast tooling, and glove/low-light use. The
synthetic DOM harness is behavioral test evidence, not a browser or accessibility certification.
