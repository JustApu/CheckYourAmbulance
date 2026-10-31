# Crew and supervisor deficiency workflows

[issue #86](https://github.com/JustApu/CheckYourAmbulance-archive/issues/86). Non-normative implementation notes for SPEC §3.3, §5.4.1, §7.7–§7.8 and §8.3.
The [crew inspection screen](crew-inspection-ui.md) remains the live shared checklist.
All mutations use the existing audited [check](check-records.md) and
[deficiency](deficiencies.md) command endpoints; these pages add no tables or write commands.

## Pages and access

| Route | Purpose |
|---|---|
| `/deficiencies/` | The 100 most recent reports, including cancelled history; active vehicle links to independent reporting. |
| `/deficiencies/{id}/` | Report details, lifecycle history, current informational links, explicit add/remove link forms. |
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
flow for an instance-scoped report. Independent kit-wide reports and session-linked independent
reports remain unsupported by the existing command. The UI does not send those fields.

Related reports are explicit multi-select choices; they are never automatically linked.
Adding or removing a link only changes that informational relationship. It does not merge,
close, reassign or transfer responsibility for either report. This UI creates unassigned
reports; assignment/deadline, repair, verification and closure controls remain later work.

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

## Save feedback and verification

Workflow forms send same-origin JSON with CSRF headers. Each form permits one request at a
time. Only a `COMMITTED` response shows Saved. Definite refusals show a reason; ambiguous
transport/5xx/invalid JSON is Unconfirmed. Unconfirmed forms lock their fields and offer Retry
with the identical request and operation id. After acknowledgement the form is single-use;
a link reloads current history, avoiding stale-tip resubmissions.

`cya/inspections/tests/test_workflow_ui.py` drives authenticated synthetic HTTP requests against
real audited commands: report replay, explicit link/unlink, own correction, administrator
invalidation, supervisor completion, denied forged requests, tenant boundaries, labels,
escaping, unavailable evidence and configuration failures. The existing crew and attention
suites cover the underlying shared UI contracts.

`cya/inspections/tests/workflow_script_harness.js` executes the actual scripts with a synthetic
DOM and deferred fetch. It covers explicit matches, reasons, kit payloads, frozen retries,
full Failed/Unable conflict payloads, removed rows, state labels and single-flight forms. Run
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
