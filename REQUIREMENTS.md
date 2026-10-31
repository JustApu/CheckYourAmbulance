# Check Your Ambulance — Requirements Baseline

**Version:** Discovery baseline 0.1 • **Updated:** 2026-10-15 • **Status:** Draft for owner review  
**Source:** Owner's extended requirements interview (176 numbered decisions plus follow-up clarifications). This is a synthesis, **not a verbatim transcript**. Confirmed decisions are distinguished from provisional values and unresolved matters in [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).

> **Historical record.** `SPEC.md` (v1.0.0, owner-approved 2026-10-16) is the authoritative product/architecture specification and supersedes this discovery baseline wherever they differ. Do not implement from this file; use it only for the original wording and rationale.

## 1. Purpose and boundaries

Create a mobile-friendly, responsive web application for the one EMS organization to inspect ambulances and movable equipment, identify deficiencies, track operational readiness, manage supplies and assets, and maintain audit-ready maintenance and regulatory records. First release serves one organization; future multi-organization support is a design consideration, not a v1 obligation. At the time this discovery baseline was written, no application had been built or deployed. Use synthetic data for examples and testing.

Principles:
- A specific **item check**, not a crew or an entire vehicle inspection session, satisfies an applicable inspection obligation.
- An EMT may inspect items on multiple vehicles; multiple EMTs may work on one vehicle concurrently, whether on duty together or not.
- Do not confuse an inspection **Pass**, an exemption/suspension, an **Unable to Inspect** outcome, or an unresolved deficiency.
- Keep immutable attribution/history of inspections, corrections, verifications, cancellations and administrative invalidations.
- Alert humans, but do not let defect severity or regulatory overdue status automatically change an ambulance's operational status.

## 2. Ambulances, checklists and scheduling

### 2.1 Templates and versioning
- Centrally managed checklist templates with vehicle-specific overrides. Administrators draft/edit, explicitly publish and optionally schedule an effective date.
- Each inspection session records and retains its original checklist version. Reopening must not silently replace that version with newer published contents. Current due/overdue state is recalculated against applicable requirements.
- Checklist items may belong to compartments, kits, equipment or ambulance-wide systems. Each may have its own recurring schedule and conditional rules. Physical equipment/checklist definitions will be provided later.
- Authorized temporary vehicle-specific stocking deviations show normal quantity, approved alternate quantity, validity dates, audit history and expiry alerts; extension is auditable. EMT-requested early termination requires approval.

### 2.2 Demonstration fleet schedule (configurable)
| Ambulance | Requirement | Early window | Grace ends |
|---|---|---|---|
| Ambulance 1 | 06:00 daily | 05:00 | 07:00 |
| Ambulance 1 | 18:00 daily | 17:00 | 19:00 |
| Ambulance 2 | 09:00 weekdays | 08:00 | 10:00 |
| Ambulance 2 | 12:00 weekends | 11:00 | 13:00 |
| Ambulance 3 | 18:00 daily | 17:00 | 19:00 |
| Ambulance 4 | 18:00 daily | 17:00 | 19:00 |

Initial inspection window is **±1 hour**, configurable. Item-specific scheduling/conditional rules override fleet defaults when relevant. Display at least **Current**, **Due/Grace Period**, and **Overdue**; retain late completions in history. A check in an allowed early window may satisfy the upcoming obligation when all item-specific requirements are met. The precise relationship between deadline/grace and window boundaries needs formalization (see open questions).

Vehicles formally **out of service** are exempt from routine checks while out of service; service status and resumption must be explicit. A change of crew or equipment use after a call does **not** by itself require repeating an entire vehicle inspection.

**Conditional example:** A 06:00 item check may be omitted if *all* EMTs working at 06:00 were already on the immediately preceding shift ending at 06:00 and a qualifying check for that item was completed during that shift. If any EMT begins duty at 06:00, a new check is needed. Other items may use different conditions; the rule is configurable and off by default.

### 2.3 Inspection sessions and individually recorded checks
- Sessions organize a set of checks but never determine item-level compliance by mere “session completed” status.
- A session left inactive for **6 hours** automatically closes as **Incomplete**, preserving every individual check and progress record. An EMT may reopen it without supervisor approval; the reopening is audit logged. Reopening refreshes current requirement statuses; no separate “what changed” summary is needed.
- Reopened sessions retain their original checklist version; completed checks retain their original timestamps and only satisfy a current requirement if still valid.
- Work can be resumed by another EMT without duplicating valid completed checks.
- Every routine ambulance inspection requires an odometer entry; anomalous readings require correction/confirmation. Engine hours can be entered but are **not mandatory yet**.
- Support **Passed, Failed, Unable to Inspect** (mandatory explanation and deficiency linkage), and **Not Applicable** only where an administrator has defined a valid rule. An invalid outcome must not satisfy a check.
- Checking an item saves **immediately**, without a separate Save/Submit button; show pending and failed saves explicitly. Server acknowledgement determines whether the entry is committed. All viewers receive updated status without manual refresh.
- Distinct EMTs can inspect distinct items concurrently. When an EMT is about to record an item that another EMT has just recorded, **warn and offer to proceed or cancel**; do not lock the item. Preserve both independently if the second EMT proceeds.
- Bulk mark selected eligible items Passed in one action; persist separate attributed records for each. Do not bulk-pass items requiring direct measurements or special observations, override deficiencies or bypass suspended items. Exact confirmation UX remains unspecified.
- All results, including repeated results in a window, are retained and count toward individual participation; latest valid checks inform status, without concealing unresolved failures.
- EMTs can correct **only their own** results, with optional explanation and full original/correction attribution and history. Supervisors likewise cannot correct results by another EMT; they may record a fresh independent inspection.
- Correcting one's accidentally entered Failed result to Passed marks its linked deficiency **Reported in Error**, retaining its history. This is not a substitute for verifying a genuine repair.
- Administrators can **invalidate**, but cannot overwrite or delete, anyone's inspection record; mandatory explanation, original data preserved, invalidated result excluded from compliance and statuses recalculated.
- **Critically:** Invalidating a Failed result does **not** automatically close its deficiency. It stays open until an administrator explicitly closes it.
- Store inspection/check history independently from current materialized status, with person, time, result, source version, correction/invalidation and relevant context.

### 2.4 Inspection views
- Switchable **Priority** (attention-needed first) and **Normal** (equipment/compartment order) views; per-EMT preferred view persists across sessions/devices.
- A **Needs Attention Only** filter is available in either view and remembered per EMT across devices/sessions/vehicles. It includes actionable due, overdue, incomplete and unresolved items **except supervisor-suspended routine checks**, which remain visible in full checklists, deficiency reporting and dashboards.
- Show relevant known deficiencies **beside their checklist items before inspection**, plus prominent ambulance operational status.
- QR/barcode entry points for vehicle/kit; manual entry/navigation fallback. The product is **online only**; no offline queue/sync requirement.

## 3. Physical kits, inventory and expirations

- Track physical kits, bags and containers individually, with identifiers, changing locations/vehicle assignment, contents, seals and full movement history.
- Intact valid seals may substitute for inspecting contents until earliest contained expiration; broken/expired seals require full contents check.
- Sealed-container **full-content random/periodic audits** are desired. Tentative concept: every sealed item fully checked at least weekly. **Frequency/scope not final**.
- Quantities have exact configured targets; flag too few **and too many**, with immediate correction in the workflow.
- Track item expirations and configurable advance alerts; **no lot-number tracking** required.
- Station inventory supports storage positions (room/cabinet/shelf), transfers and restocking ambulances with quantity deductions and attribution, receipts with quantity/location/expiration, low-stock alerts and reorder thresholds.
- Generic supply catalog rather than mandatory branded SKUs; purchasing can select equivalent products and record multiple vendors/prices without a separate equivalence approval workflow.
- Consistent units of measure across order, receipt, store and stock processes.
- Purchase orders support vendors, pricing, purchase history, partial receipts, outstanding quantities and completion.

## 4. Deficiency lifecycle and verification

- A deficiency can concern an item, kit, compartment or whole vehicle; report it from an inspection or independently, with category/severity, photos/documents, assignee, deadline and timestamped progress history.
- Show related existing deficiencies when reporting a new issue; suggest possible matches. EMT chooses whether to link; EMTs may add/remove links with audit trail. Links do not merge responsibilities or cause automatic closure.
- Corrections may be performed by the original EMT or another; verify successfully before an authentic deficiency closes. The same EMT may correct and verify. Successful verification automatically closes the linked deficiency.
- For **kit/compartment** defects, verify by rechecking the *entire* affected kit/compartment. For **vehicle-level** defects, verify affected systems rather than the entire truck. Supervisors may expand verification scope with audit history.
- If a supervisor adds required verification after closure, automatically **reopen** the deficiency and preserve the history. Notify only its current assignee, retaining previous assignee by default unless reassigned. Supervisor must set a **new** resolution deadline and retain the former deadline in history.
- An EMT can request a deadline extension (reason + proposed date). Supervisor approves/denies or selects a different date with explanation; approved date governs overdue notices, all revisions retained. Unlimited extensions, but highlight long-running cases.
- Track age since original report separately from deadline. Administrators configure dashboard age thresholds; no independent age-based notifications.
- **Awaiting External Dependency** status: mandatory explanation; one status/dependency record per deficiency, other details in progress notes; optional follow-up date displayed on dashboard **without automatic follow-up reminder**. EMTs can update this status and details, audit logged, but cannot bypass deadline/escalation.
- Authorized acknowledgment may suspend escalation for a configured period, requiring explanation and action plan; expires automatically, renewals audited.
- Deficiency correction/verification/closure records stay separate from the original failed check, and an unresolved failure is not suppressed by a subsequent passing check.
- EMT-author corrections that explicitly change accidental Failed to Passed can mark the linked deficiency **Reported in Error**. Administrator invalidation **never auto-closes** the deficiency.
- Supervisor can suspend repetitive **routine inspection checks for a specific known defect**, with visible “Known Deficiency — Check Suspended” status. Suspension is not a Pass, affects only the explicitly suspended checks and does not change operational status. Unaffected checks remain required (e.g., continue counting remaining masks). Upon successful verification closing the deficiency, suspension ends **automatically**.

## 5. Ambulance operations, maintenance and contracts

- Supervisors set **In Service**, **Limited Service** or **Out of Service** manually with required explanation, history and notification. EMTs can request a change for supervisor decision.
- Severity, deficiency events, expired certifications or failed checks do **not** automatically change operational status.
- Track individual equipment assets/serial numbers, current assignment, transfers, maintenance and repairs linked to deficiencies as appropriate.
- Preventive maintenance rules may be calendar- or usage-based, whichever occurs first. Usage is entered manually; vehicles support mileage and engine hours.
- Record vehicle/equipment service vendors, contacts, parts/labor/total cost, invoices, service reports and work history.
- Warranties/service contracts track provider, covered multiple vehicles/assets, coverage, term/renewals, costs, attached documents and expiration alerts.

## 6. Regulatory and historical compliance

- Admins define recurring regulatory inspections/certifications by vehicle/equipment type, distinct from preventive maintenance.
- Store requirement version, results, effective dates, due dates, documents, reminders and compliance history; overdue items appear on dashboard but do **not** automatically remove a vehicle from service.
- Record only **externally authorized** legal extensions/exemptions, naming issuing authority, evidence and revised deadline; administrators cannot create a legal exemption by fiat.
- Produce auditable historical “as of” views with then-applicable rules, inspections and exemptions, not merely today's current statuses.
- Search/filter completed and incomplete inspections, checks, deficiencies and corrections. Export CSV and Excel; support saved/shareable role-controlled filter definitions.
- **Optional** automated generation and email delivery of reports; schedule/format permissions require design.

## 7. Users, permissions, participation and notifications

- Sign in using **organization-managed Google Workspace identities only**, not personal Google accounts or merely any Google account claiming the domain. Disabling Workspace access must revoke entry while retaining attribution.
- Built-in roles, including EMT, supervisor, supply officer and administrator; a user may hold multiple roles. New valid users initially receive EMT role, elevated roles assigned by administrators.
- Participation reports show check counts, dates, inspected items/categories, ambulance and recency with filters; **no quotas or required minimums**.
- Notifications via email and in-app; admin controls which events/channels are mandatory, optional preferences delegated to users, recipients designated by roles/users and de-duplicated.
- Fleet dashboard shows service status, due/overdue inspection items, inspection progress, deficiencies, compliance and readiness. Suspended items and deferred external dependencies remain visible in appropriate supervisor contexts.

## 8. Humanity roster integration

- Humanity / ShiftPlanning is the **schedule source**, not the payroll timeclock. Refresh/cached schedule **hourly** is enough; API details/credentials will follow.
- Evaluate conditional inspections using scheduled EMT duty/crew continuity, with allowance for mismatches between schedule and actual personnel.
- At inspection time EMTs may confirm/correct the **actual roster for that inspection only**. Retain both scheduled and corrected roster plus who changed it; do not write back to Humanity or alter other inspection sessions.
- Roster does not restrict who can perform equipment checks. Volunteers do not have a timeclock; paid staff may use a separate payroll timeclock, which is out of scope for roster integration.

## 9. Delivery and nonfunctional expectations

- Responsive, touch-friendly web UI optimized for EMTs using phones as well as larger screens.
- Online-only storage and authoritative server-side validation of schedules, corrections, permissions, deadlines and status transitions.
- Near-real-time multi-user sync with trustworthy server timestamps and clear pending/error indicators; no silent overwrites.
- Safe auditable history capable of reconstructing past compliance; role-checked exports, attachments and notifications.
- No predetermined implementation language, database schema, hosting arrangement or API contract has been ratified by the owner in this interview. Existing repository engineering guidance references Perl/Mojolicious/SQLite but **technology adoption is not inferred solely from template instructions**.

## 10. Planned release boundaries (proposal, not an approved commitment)

**First release candidate:** Google Workspace access, fleet/vehicle/kit checklists and versions, per-item inspection windows and conditional rules, auto-save/live sync, concurrent checking, known-deficiency visibility, defect creation/correction/verification, operational status, audit log and supervisor dashboard.

**Later candidates:** supply stock and purchase orders; maintenance/assets/contracts; richer regulatory historical reporting; optional scheduled reports and advanced roster integrations. Foundations for cross-linked IDs and histories should be designed early.

## 11. Review/change control

- Treat this as a discovery record pending owner review. Record any conflicting or unsettled details in [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md), **not as guessed definitive behavior**.
- When promoting to `SPEC.md`, preserve audit-sensitive rules and separate decisions from implementation proposals.
- Future agents: read [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md), [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md), and governing [AGENTS.md](AGENTS.md) before creating implementation issues.
