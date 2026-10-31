# Check Your Ambulance — Open Questions and Decisions

**Historical record.** The live decision register is [SPEC.md §24](SPEC.md#24-decision-register), which supersedes this file; entries here that are not marked decided were open at 2026-10-15 and their current status is in the register. **As of:** 2026-10-15. These entries were **unresolved**, **tentative**, or pending artifacts. They are not approved implementation specifications. Priority means when the answer is needed, not an order to resume a 1-question-at-a-time interview.

## Blocking before the first production inspection release

| ID | Priority | Question / needed artifact | Safe interim approach (proposal only) |
|---|---|---|---|
| OQ-01 | High | Supply actual vehicle/kit/compartment checklists: item definitions, quantities, locations, instructions, inspection intervals and any exceptions. | Seed **synthetic** examples only; do not treat as official stocking requirements. |
| OQ-02 | High | Formalize the semantics of ±1h: is the period before due 'early valid' and the hour after due 'grace'; when exactly does an unmet check become Due vs Overdue, including crossing day boundaries and clock changes? | Model separate due instant, earliest qualifying instant and grace deadline in the spec. |
| OQ-03 | High | How do per-item windows overlap, especially Ambulance 1's example 06:00/18:00 and other conditional or additional checks? Can one physical check satisfy multiple obligations? | Use explicit obligation/check links, never accidentally count one record twice without rule authorization. |
| OQ-04 | High | Define “no activity” for 6-hour session auto-close: only committed check/correction, or navigation/heartbeat/roster update too? How does re-opening reset the timer? | Count persisted business activity, not passive viewing; verify with owner. |
| OQ-05 | High | When a past checklist version is retained on reopened inspections but new schedules/rules are published, which requirement version governs *current* due/overdue status? | Display original session version and separately calculate current obligations; never inject new fields into old session silently. |
| OQ-06 | High | Provide per-role permissions matrix (EMT, supply officer, supervisor, administrator), including who can publish checklists, correct/invalidate, assign defects, and approve exceptions. | Start from explicitly approved permissions only and deny ambiguous privileged actions. |
| OQ-07 | High | Are there cases where a deficiency may be marked Reported in Error independent of its author's mistaken Failed→Passed correction? Who can do so and what supporting documentation is needed? | Do not broaden cancellation power without a decision. |
| OQ-08 | High | How to handle one deficiency supported by multiple failed inspection records when one fails correction/invalidation? | Preserve unresolved deficiency; avoid canceling while supporting evidence remains. Owner's explicit rule for admin invalidation: **keep deficiency open until admin explicitly closes**. |
| OQ-09 | High | What authorizes a correction to a genuinely failed test versus an accidental data-entry fix, and how is that distinguished in UI? | Show separate “Correct entry” and “Record repair/verification” flows; do not assume correction implies repair. |
| OQ-10 | High | Define exact behavior of concurrent edits, backend idempotency and transient saves: what counts as “just recorded” for duplicate warning and how do concurrent bulk pass and fail interact? | Record independently with server-side conflict warnings; never suppress a failure or silently replace data. |
| OQ-11 | High | Must bulk passing require confirmation for all batches, large batches only, or never? Which items are categorically ineligible? | Use explicit eligibility per item; default to conservative confirmation pending decision. |
| OQ-12 | High | Mandatory odometer entry on every *routine ambulance inspection* vs independent item checks: which event requires it when a session has no single completer? | Model a separate odometer-check obligation associated with scheduled inspection, rather than requiring each EMT to re-enter it. |
| OQ-13 | High | Define user identities and the actual Workspace verification model/organization domain configuration and emergency access procedures. | Avoid trusting email suffix alone; no production authentication until identity checks are verified. |

## Decisions that can wait for a prototype / later release

| ID | Area | Outstanding point |
|---|---|---|
| OQ-14 | Sealed kit audits | Tentative minimum: each sealed item fully inspected once each week. Confirm period, random vs scheduled selection, seal resealing and any equipment exemption. |
| OQ-15 | Real-time UI | Exact warning wording, stale-view handling, connectivity failures, and whether/how to indicate another EMT currently viewing an item. |
| OQ-16 | Reporting | Initial standard report templates; who can schedule/receive automated emails, formats, retention and safe distribution. Automated reporting is optional, not mandatory for initial release. |
| OQ-17 | Humanity | Actual API docs, credentials, authentication, mapping of shifts/users/ambulances, refresh/backoff and behavior when stale/unavailable. Hourly refresh frequency is decided. |
| OQ-18 | Engines | Future triggers for making engine-hour input mandatory, and whether equipment usage counters need validation. Engine hours currently optional. |
| OQ-19 | Regulatory | Real-world regulatory inspections, authorities, certification rules and proof/evidence requirements. |
| OQ-20 | Roles/notifications | Mandatory notification events, escalation timings, role recipient mappings, email safety and thresholds for age highlights. |
| OQ-21 | Operational status | Explicit limited-service categories, who can approve status-change requests, resumption and fleet alert rules. |
| OQ-22 | Inventory | Starting station storage positions, catalog units, purchase-order workflow and vendor fields. |
| OQ-23 | Attachments | Allowed types/sizes, retention and visibility by role; avoid sensitive patient data and accidental identifiers. |
| OQ-24 | Deployment | Hosting, database, backup/recovery, application stack, Google Workspace integration and operational ownership. Repository boilerplate refers to Perl/SQLite, but no stack has been specifically approved in discovery. |
| OQ-25 | Time | Local station timezone, daylight saving edge cases, session closure timing, as-of reporting temporal versioning. |
| OQ-26 | Requirements governance | How and when to promote these requirements into the repo's normative `SPEC.md`, including review/version/approval process. |
| OQ-27 | MVP | Confirm priority/release boundaries and whether purchasing, maintenance, regulatory evidence or report emails must be included in initial launch. |

## Raised while drafting `SPEC.md` (OQ-28–OQ-45)

Proposed during the draft in [SPEC.md](SPEC.md); classes, spec locations and safe defaults are in its [decision register](SPEC.md#243-questions-raised-while-drafting-new). Entries marked **Decided** record owner decisions; the rest are not owner decisions.

| ID | Question |
|---|---|
| OQ-28 | **Decided 2026-10-15:** a Failed check counts as performed (satisfies the inspection; deficiency handled separately). |
| OQ-29 | **Decided 2026-10-15:** Unable to Inspect requires a reason; a supervisor may mark the obligation complete. |
| OQ-30 | **Decided 2026-10-15:** invalidating the verifying check reopens the deficiency (owner accepted proposal). |
| OQ-42 | **Decided 2026-10-15 (owner approved as written):** Provisional extra conditions on "Reported in Error" via author correction: approve or revise? |
| OQ-43 | **Decided 2026-10-15 (owner approved as written):** Invalidating a correction chain excludes the whole chain with no revival of earlier outcomes — approve? |
| OQ-44 | **Decided 2026-10-15 (owner approved as written):** Which actual-roster correction governs a shared obligation when concurrent sessions disagree? |
| OQ-45 | **Decided 2026-10-15 (owner approved as written):** Effect of later correction/invalidation of an Unable-to-Inspect check on a supervisor completion. |
| OQ-41 | May a supervisor mark complete an Unable-to-Inspect obligation they recorded themselves? |
| OQ-31 | Is an inspection required when a vehicle returns to service? |
| OQ-32 | Are there time or state limits on an author correcting their own check? |
| OQ-33 | Can an administrator invalidate their own check? |
| OQ-34 | Is an explanation mandatory for explicit administrator closure of a deficiency? |
| OQ-35 | Retention periods for audit and record data. |
| OQ-36 | First-administrator bootstrap and minimum administrator count. |
| OQ-37 | Display of an obligation whose conditional waiver is withdrawn after the grace deadline. |
| OQ-38 | Per-EMT vs per-EMT-and-device persistence of view preference/filter (source documents differ). |
| OQ-39 | In as-of views, are later invalidations shown or hidden by default? |
| OQ-40 | Device/glove/lighting constraints for the UI. |

## Explicitly **decided** — do not reopen casually

- In the demonstration configuration, Ambulance 4 follows Ambulance 3: **18:00 daily**.
- Default inspection window **±1 hour**, administrator configurable.
- Incomplete sessions auto-close after **6 inactive hours**, **EMTs may reopen**, and current due/overdue requirements refresh; **no separate changes summary**.
- Preserve original inspection checklist version and all prior checks/attribution when reopening.
- Individual results auto-save, live synchronize to other inspectors, and allow selected-item bulk Pass; warn if another EMT just checked that item, let second EMT proceed or cancel; do not lock.
- EMT may correct **only own** inspection entries; reason optional. Supervisors may not correct others' entries; they may make a new check.
- Accidental Failed→Passed correction by original EMT marks linked deficiency Reported in Error with history; genuine failures require verification.
- **Only administrators** may invalidate someone else's inspection result, with mandatory explanation and immutable original history. Invalidated Failed results **leave their deficiency open pending explicit administrator closure**.
- Preferred Priority vs Normal view and Needs Attention Only filter persist per EMT and device. Suspended checks are **excluded** from the attention-only filter but visible elsewhere.
- Supervisor-suspended routine checks are **not passes**, and suspension ends after linked verified deficiency closure.
- Engine hours optional; odometer mandatory per routine inspection.
- Humanity hourly polling adequate; API details pending.
- Automated report generation/email is **optional**.
- Weekly sealed-kit full audits **not yet confirmed**.
- **2026-10-20:** owner decided the application stack for OQ-24: Python with Django, and MariaDB 10.11 on Linux. Hosting, TLS, secrets and backups remain open (see [SPEC.md §17.5](SPEC.md#175-hosting-database-engine-backup-oq-24)).
- **2026-10-16:** owner approved the proposed safe defaults for OQ-02 (edge cases with OQ-25), OQ-03, OQ-04, OQ-05, OQ-08, OQ-09, OQ-10, OQ-12, OQ-24 (working direction only; final selection by architecture spike), OQ-25, OQ-26 and OQ-36 as recorded in [SPEC.md §24](SPEC.md#24-decision-register).

## Work intake guidance

Resolve a blocking question when it affects an issue's data model, security, compliance or irreversible behavior. Otherwise prototype with a clearly labeled reversible assumption; seek owner decisions in one focused review, not another long chain of hypotheticals. Maintain issue-specific acceptance criteria in GitHub Issues and authoritative behavior in the approved `SPEC.md`.
