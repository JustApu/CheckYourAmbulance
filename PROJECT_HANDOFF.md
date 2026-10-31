# Check Your Ambulance — Project Handoff

**Date:** 2026-10-15  
**Status:** Discovery summarized; application **not implemented**. This document helps continue work in a new ChatGPT conversation, with Claude/Codex or with a human contributor.

## Start here

1. Read [AGENTS.md](AGENTS.md) and its linked coordination, review and engineering procedures **before** claiming or modifying work.
2. Read [REQUIREMENTS.md](REQUIREMENTS.md) for consolidated interview decisions and [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) for unresolved items.
3. Inspect GitHub Issues and open PRs before working. One agent per claimed issue/branch, follow signed coordination conventions. Do not merge without owner authorization.
4. **`SPEC.md`** (v1.0.0, owner-approved 2026-10-16) is the authoritative product and architecture source. This handoff and the interview documents are the **historical discovery record**; if they differ from `SPEC.md`, `SPEC.md` wins.

## Product vision

A responsive fleet-readiness platform for one EMS organization in the initial release, with a design that can support other organizations later. It combines scheduled item-level ambulance checks and physical kit/seal workflows with deficiency management, station supplies, equipment/vehicle maintenance, regulatory compliance, audit history and targeted reporting. It is **not** a crew timeclock, ambulance billing platform, patient chart or dispatch/CAD replacement.

## Design invariants — highest importance

1. **Item-level compliance:** independently performed valid equipment checks satisfy the proper obligation; no “all crew must inspect the truck” rule or blanket redo upon shift change.
2. **Concurrent EMT work:** automatic per-item saves, immediate shared visibility, duplicate-item warning without locking, and independent results with attributed audit history.
3. **Preserve evidence:** original inspection records, corrections, supervisor verifications, administrative invalidations and actual historic checklist requirements remain traceable.
4. **No false Pass:** suspended or inapplicable checks cannot be represented as completed successfully. An invalidated record no longer counts for compliance.
5. **Deficiency is independent:** a recent Pass cannot erase a genuine unverified failure; administrator invalidation of a Failed record leaves its associated deficiency open until explicit administrator closure.
6. **Controlled vehicle status:** defects and expired inspections trigger alerts but never automatically take a vehicle out of service; supervisors make audited status decisions.
7. **Scoped verification:** kit/compartment defects require complete relevant reinspection; vehicle defects require affected-system checks, subject to supervisor expansion.
8. **Online-only:** no offline reconciliation promised; errors and pending saves must be obvious.
9. **Correct rosters, not payroll:** Humanity is a schedule feed polled about hourly; EMTs can annotate the actual crew solely for an inspection.
10. **Access control:** managed Google Workspace identity only, audited privileges, no hidden broad edit of other EMTs' inspection results.

## Demonstration schedule (all values configurable)

- Ambulance 1: 06:00 and 18:00 every day.
- Ambulance 2: 09:00 weekdays and 12:00 weekends.
- Ambulance 3 and Ambulance 4: 18:00 every day.
- Default checking window: one hour before to one hour after due time.
- Unfinished session closes Incomplete at 6 hours of inactivity; EMT can reopen; preserve original records and version while refreshing current status.

## Suggested data-model sketch (historical; the authoritative model is SPEC §18)

Separate entities and immutable event history for:
- Organization, authorized users, roles, Workspace identities.
- Ambulances, compartments, physical kits/seals, tracked equipment assets and movements.
- Published/versioned checklist templates and item definitions; ambulance overrides and conditional inspection obligations/windows.
- Inspection sessions and **individual check records** with user/time/outcome/checklist version; author corrections, administrator invalidation, bulk-group metadata.
- Deficiencies, links, assignments, deadlines and extension history, external dependencies, correction actions, verification requirements/results, escalation suppression and closure history.
- Station stock positions, catalog items, counted stock movements, purchase orders/receipts.
- Maintenance schedules/jobs and vendors; warranties/contracts.
- Compliance requirements/versions, evidence, authorized exemptions and historical as-of views.
- Notification routing/delivery/preferences and saved/optionally scheduled reports.

Avoid storing only a single mutable “latest status” without provenance. Any derived/materialized current statuses must be reproducible from authoritative events, rules and effective dates. Concurrency needs explicit server-side transactions, unique/idempotency keys and authorization checks **when architecture is decided**.

## Suggested next work packages (proposals, not claimed implementation issues)

- ~~Owner review of requirements and publication of authoritative `SPEC.md`~~ Done 2026-10-16; remaining open decisions are tracked in `SPEC.md` §24.
- Data model focused on obligation/check separation, history, concurrency and deficiencies.
- Mobile-first inspection UI prototype using synthetic equipment only.
- Authentication/role-permission design, then basic checklist publishing and live checks.
- Deficiency lifecycle, corrections/invalidation, verification and status dashboards.
- Later inventory/purchasing, maintenance/contracts and regulatory/reporting workflows.

Do **not** claim these work packages are accepted milestones, implemented modules or existing backlog issues until recorded in GitHub.

## Current external dependencies and artifacts

- **Checklist inventory:** Owner will provide actual starting checklist items later.
- **Humanity API:** Owner will provide connection details later; **never commit real API credentials**.
- **Periodic sealed-kit full audit frequency:** Tentatively weekly; **undecided**.
- **Technology/deployment:** Not formally agreed in interview. The starter repo's agent guidance refers to Perl, Mojolicious and SQLite; verify intent before making that an immutable product constraint.
- **Regulatory and station inventory examples:** Await validated operational definitions and sanitization.

## Agent handoff prompt

> Work on the Check Your Ambulance project in `JustApu/CheckYourAmbulance`. Read `AGENTS.md`, required linked procedures, `REQUIREMENTS.md`, `OPEN_QUESTIONS.md` and `PROJECT_HANDOFF.md`. Treat those three files as the historical discovery record and `SPEC.md` as authoritative; no application exists yet. Follow the repo's issue claiming and review workflow. Read the approved `SPEC.md` before implementation. Preserve item-level compliance, simultaneous auto-saved EMT checks, author-only corrections, explicit administrator invalidations, independent deficiency lifecycles and historical auditability. Do not assume undecided checklist contents, APIs or technology choices. Make small reviewable changes on a claimed issue branch.

## How to resume with the owner

Instead of continuing from question 177 indefinitely, propose a **single prioritized owner review** of the highest-impact unresolved decisions and a prototype. Record decisions in the proper specification and in GitHub issue acceptance criteria. Avoid allowing a ChatGPT thread's length to become the only source of project memory.
