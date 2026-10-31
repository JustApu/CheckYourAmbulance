# Check Your Ambulance — Specification

**Spec version:** 1.22.1 • **Status:** APPROVED; original approval 2026-10-16; behavioral amendments through 1.22.0 and the nonbehavioral identity clarification 1.22.1 are recorded in §25.6. Amendment 1.22.0 was approved by the owner on 2026-10-30 ([issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156); see §25.6). • **Updated:** 2026-10-31
**Baseline:** [REQUIREMENTS.md](REQUIREMENTS.md) (discovery baseline 0.1, interview decisions through question 176), [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) (OQ-01–OQ-27), [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md)

> **Authority.** This file is the authoritative product-behavior and architecture source. It overrides [REQUIREMENTS.md](REQUIREMENTS.md), [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md), [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md) and GitHub issue scope wherever they differ; those three are now the historical discovery record. Changes follow [§25 Approval and amendment process](#25-approval-and-amendment-process). Implementation issues must not treat an **[OQ]** item, or a **[P]** item whose §24 register row is not **Decided**, as settled; see §24 for what remains open.

## 0. How to read this document

### 0.1 Status tags

Every normative statement carries exactly one tag so that decisions, proposals and unknowns cannot be confused.

| Tag | Meaning | May implementation rely on it? |
|---|---|---|
| **[C]** Confirmed | Taken from a decision the owner made in the discovery interview (`REQUIREMENTS.md`, or the "Explicitly decided" list in `OPEN_QUESTIONS.md`). Cites its source. | Yes. |
| **[P]** Proposed | A design choice or safe default written by the spec author to make [C] items implementable. Reversible unless §24 says otherwise. | Yes where its §24 row is **Decided**. Otherwise it may guide prototypes and development, but needs a separate owner decision before production use (§25.6). |
| **[OQ-nn]** Unresolved | Depends on an owner decision or an artifact not yet supplied. Shown with the safe interim behavior. | No. Implement only the stated safe default, and only where §24 permits it. |

**Register overrides tags.** Where a §24 register row reads **Decided**, the safe default described for it in the body (even if still tagged **[P]**) is owner-approved. Body tags are not individually rewritten; the register is authoritative for decided rows.

"Requirement" language: **must** = mandatory, **should** = strong default, **may** = optional.

### 0.2 Identifiers

Requirements carry stable IDs of the form `AREA-n` (e.g. `CHK-4`). IDs are never reused or renumbered; retired requirements are marked *Withdrawn*. Acceptance criteria use `AC-n` (§23). Decision-register entries use `OQ-nn` (§24); OQ-01–OQ-27 come from `OPEN_QUESTIONS.md`, and OQ-28 onward were raised while writing this spec.

Source citations such as `(REQ §2.3)` point at sections of `REQUIREMENTS.md`; `(OQ list: decided)` points at the "Explicitly decided" list in `OPEN_QUESTIONS.md`; `(HO inv. 5)` points at design invariant 5 in `PROJECT_HANDOFF.md`.

### 0.3 Data hygiene

All examples in this spec use synthetic names and values. Real checklists, roster data, credentials, and API responses must never be committed (see [AGENTS.md](AGENTS.md#github-data-hygiene)).

## 1. Purpose, scope and non-goals

**1.1 Purpose [C]** (REQ §1). A mobile-friendly, responsive web application for an EMS organization to inspect ambulances and movable equipment, identify and resolve deficiencies, track operational readiness, manage supplies and assets, and keep audit-ready maintenance and regulatory records.

**1.2 Tenancy [C]** (REQ §1). The first release serves one organization. **[P]** The data model carries an `organization_id` on organization-owned entities so multi-organization support is not precluded, but v1 provides no cross-organization administration, billing or isolation features.

**1.3 Non-goals [C]** (HO "Product vision"). The system is not a crew timeclock, ambulance billing platform, patient chart (ePCR), or dispatch/CAD replacement. It must not store patient-identifying data (see §16.5).

**1.4 Core principles [C]** (REQ §1; HO invariants). These govern every other section; where a later rule appears to conflict, the principle wins and the conflict is a spec defect to be fixed by amendment.

| ID | Principle |
|---|---|
| PRN-1 | A specific **item check**, not a crew or whole-vehicle session, satisfies an inspection obligation. |
| PRN-2 | A crew member may inspect items on multiple vehicles; multiple crew members may work on one vehicle concurrently. |
| PRN-3 | Pass, exemption/suspension, Unable to Inspect, and unresolved deficiency are distinct states and are never conflated. |
| PRN-4 | History is immutable and attributed: inspections, corrections, verifications, cancellations and administrative invalidations. |
| PRN-5 | Alerts go to humans; defect severity and regulatory overdue status never change ambulance operational status automatically. |
| PRN-6 | A recent Pass never erases an unresolved genuine failure. |
| PRN-7 | The product is online-only; pending and failed saves are always visible. |

## 2. Release scope

**2.1 Release plan [P]** (REQ §10 is itself a "proposal, not an approved commitment"; see OQ-27). Release boundaries below were confirmed by the owner on 2026-10-16 (OQ-27).

| Release | Contents | Spec sections |
|---|---|---|
| **R1** (first release candidate) | Google Workspace sign-in; roles; fleet/vehicle/kit checklists with versions; per-item inspection windows and conditional rules; sessions; auto-save and live sync; concurrent checking; minimum sealed-kit workflow (§10.1); corrections and invalidation; known-deficiency visibility; deficiency creation/correction/verification; operational status; audit log; supervisor dashboard; in-app and email notifications (minimum set); odometer. | §3–§9, §13, §15 (Google), §16, §17–§20 |
| **R2** (later candidate) | Physical kit/seal tracking beyond the R1 minimum (movement history, prelabeled seal serials, scheduled or random audits), supply stock, station inventory, purchase orders. | §10 |
| **R3** (later candidate) | Maintenance, assets, service vendors, warranties/contracts. | §11 |
| **R4** (later candidate) | Richer regulatory/as-of reporting, optional scheduled reports and emails, advanced Humanity roster integration. | §12, §14 |

**2.1a Deferred from R1 [C]** (owner decision 2026-10-20, [issue #33](https://github.com/JustApu/CheckYourAmbulance-archive/issues/33)). **Bulk Pass** (§7.6) is deferred to a later release; R1 relies on one-tap individual checks and sealed-kit substitution (§10.1). "Expiring soon" alerts and reorder flags are also deferred (a future release, with the supply and inventory work in §10).

**2.2 Foundation rule [C]** (REQ §10). Even for R1, identifiers and histories of later-release entities (kits, assets, vendors, regulatory requirements) must be designed so that later releases can cross-link without migration of R1 history.

**2.3 No unrelated features [P].** Anything not traced to `REQUIREMENTS.md` is out of scope until added through §25.

**2.4 Roster dependency [OQ-27, OQ-17].** The conditional-check rule in §5.6 depends on the Humanity roster. R1 behavior must work with the roster unavailable (§15.2): conditional omissions are simply not granted and the check is required.

## 3. Roles and authorization

**3.1 Built-in roles [C]** (REQ §7): **Crew**, **Supervisor**, **Supply Officer**, **Administrator**. A user may hold several. A newly provisioned valid user receives **Crew** only; elevated roles are assigned by an administrator, and every grant/revocation is audit-logged. Crew members performing checks may hold various clinical certifications (e.g. EMT, EMR, Paramedic, Driver) or none (e.g. probationary members/trainees), and may be volunteer agency members ("members") or paid employees ("employees"); all authorized personnel performing inspections share the baseline Crew role. Inspection records capture individual attribution (actor, instant, session) without requiring clinical titles; proof of clinical qualification and driver status is established by cross-referencing external roster (Humanity) and organization personnel/training records.

**3.2 Authorization model [P].** Authorization is enforced **server-side on every request**, deny-by-default, using the union of the actor's roles. A UI control being hidden is never the enforcement mechanism. Role grants are evaluated at request time (not cached beyond the session in a way that outlives revocation).

**3.3 Permission matrix [OQ-06].** The table below was completed from the owner's stated rules and decisions on 2026-10-15 (OQ-06); any action not listed, and any cell later marked "?", is denied until decided. Staff must not infer permissions from convention.

| Action | Crew | Supervisor | Supply Officer | Administrator | Basis |
|---|---|---|---|---|---|
| Record a check on any item, any vehicle | Yes | Yes | Yes | Yes | [C] roster does not restrict who may check (REQ §8) |
| Correct a check | Own only | Own only | Own only | Own only | [C] (REQ §2.3) |
| Record a fresh independent check on an item another user checked | Yes | Yes | Yes | Yes | [C] |
| Invalidate another user's check | No | No | No | **Yes** | [C] (OQ list: decided) |
| Create/report deficiency | Yes | Yes | Yes | Yes | [C] (REQ §4) |
| Link/unlink related deficiencies | Yes | Yes | Yes | Yes | [C] Crew members may (REQ §4); Supply Officer per owner decision 2026-10-15 |
| Assign a deficiency / set deadline | No | Yes | **Supply-related deficiencies only** | Yes | [C] owner decisions 2026-10-15 (Crew members may still request extensions) |
| Update Awaiting External Dependency | Yes | Yes | Supply-related deficiencies only | Yes | [C] (REQ §4); Supply Officer per owner decision 2026-10-15 |
| Add a progress note to a deficiency | Yes | Yes | Supply-related deficiencies only | Yes | [C] owner decision 2026-10-27 ([issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149)): the same roles as updating Awaiting External Dependency. Roles combine (§3.2), so a Supply Officer who also holds another role may add notes on any deficiency that other role allows |
| Record a correction (repair) on a deficiency | Yes | Yes | Yes | Yes | [C] owner decision 2026-10-27 ([issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153)): VER-1, the original crew member or another crew member |
| Request deadline extension | Yes | Yes | Only on deficiencies assigned to them | Yes | [C]; Supply Officer per owner decision 2026-10-15 |
| Approve/deny deadline extension | No | Yes | No | Yes | [C] "Supervisor" (REQ §4) |
| Expand verification scope / require added verification | No | Yes | No | Yes | [C] (REQ §4) |
| Acknowledge (suspend escalation) | No | Yes | No | Yes | [C] owner decision 2026-10-15 |
| Mark an Unable-to-Inspect obligation complete | No | Yes | No | Yes | [C] owner decision 2026-10-15 |
| Invalidate a supervisor-completion determination | No | No | No | **Yes** | [C: owner-approved 2026-10-25, UTI-3]: administrator invalidates with an explanation; original determination is retained |
| Suspend routine checks for known defect | No | Yes | No | Yes | [C] (REQ §4) |
| Close deficiency by verification | auto | auto | auto | auto | [C] automatic on successful verification |
| Close deficiency explicitly (no verification) | No | No | No | **Yes** | [C] (OQ list: decided) |
| Close deficiency explicitly when current correction-time scope is empty | No | Yes | No | Yes | [C: owner-approved amendment 1.19.0 (2026-10-30, [issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153))]; separate event and status; Administrator-only general closure is unchanged |
| Mark deficiency Reported in Error via own correction | author only | author only | author only | author only | [C]; further cases OQ-07 |
| Require an ad hoc inspection | No | Yes | No | Yes | [C] owner-approved [issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158) amendment; §5.9 |
| Set vehicle operational status | Request only | Yes | No | Yes | [C] (REQ §5) |
| Draft/edit checklist templates | No | Yes | No | Yes | [C] owner decision 2026-10-15 (supersedes administrator-only baseline wording) |
| Publish checklist / schedule effective date | No | Yes | No | Yes | [C] owner decision 2026-10-15 |
| Approve temporary stocking deviation | No | Yes | No | Yes | [C] owner decision 2026-10-15 |
| Approve early termination of deviation | No | Yes | No | Yes | [C] owner decision 2026-10-15 |
| Verify an intact kit seal; break a seal (reason required); reseal after a full contents inspection | Yes | Yes | Yes | Yes | [C] owner decision recorded on [issue #18](https://github.com/JustApu/CheckYourAmbulance-archive/issues/18) (2026-10-20): any authorized agency member, no second approver; see §10.1 |
| Request a full-content audit of a sealed kit | No | Yes | No | Yes | [C] owner decision 2026-10-20: supervisors for now; to become permission-based later |
| Add, move or deactivate a kit instance | No | Yes | Yes | Yes | [C] owner decision 2026-10-24 ([issue #97](https://github.com/JustApu/CheckYourAmbulance-archive/issues/97), OQ-54): supervisors, supply officers and administrators; crew members may not |
| Open/join an inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)] any active user with an organization role may begin or join inspection work |
| Rejoin (reopen) an ended inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)] only a recorded participant may reopen; preserves original session and checklist version |
| Manually end an inspection session | Yes | Yes | Yes | Yes | [C: owner decision 2026-10-24, [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)] only a recorded participant may end; session state remains informational |
| Manage users and role grants | No | No | No | Yes | [P] |
| Manage inventory/catalog/purchase orders | No | No (view only) | Yes | Yes | [C] owner decision 2026-10-15; R2 |
| Configure schedules, windows, thresholds, notifications | No | No | No | Yes | [C] "administrator configurable" |
| Configure deficiency categories and severity levels | No | No | No | Yes | [C] owner decision 2026-10-25 ([issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80), OQ-56): administrator-configurable and versioned |
| Re-grade a deficiency's severity | No | Yes | No | Yes | [C] owner decision 2026-10-25 ([issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80), OQ-56): an available, logged permission; **[P]** supervisors and administrators until the owner names other roles |
| Export/saved filters | role-controlled | role-controlled | role-controlled | role-controlled | [C] (REQ §6); matrix OQ-06/OQ-16 |

**3.4 Safe default [P].** An action absent from the matrix, or marked "?", is denied and logs a denied-action audit entry. As of 2026-10-15 the owner has resolved every cell in §3.3 (OQ-06); new actions added by amendment must be listed in the matrix or stay denied.

**3.5 Administrator accountability [P].** Administrators are subject to the same attribution rules. An administrator cannot invalidate a check *without* an explanation; an administrator's own check can only be corrected by that administrator as author. Whether an administrator may invalidate their *own* check (rather than correcting it) is OQ-33.

**3.6 Bootstrap and lifecycle [OQ-36].** How the first administrator is designated, the minimum number of administrators, and removal of the last administrator are undecided. **[P]** Safe default: the first administrator is seeded by the deployer through a documented operator procedure (not through the web UI), and the system refuses to remove or demote the last remaining administrator.

## 4. Fleet, equipment and checklist model

### 4.1 Fleet [C] (REQ §2.2)

Vehicles are identified by a stable internal ID and a display name. The demonstration fleet shows four ambulance units (Ambulance 1 through Ambulance 4); names, numbers and VINs are synthetic until supplied, and the application does not limit an organization to four vehicles. Each vehicle has an operational status (§9), an optional "Out of service" routine-check exemption period (§5.9), an odometer, and optional engine hours (§6.8).

### 4.2 Location hierarchy [C] (REQ §2.1, §3)

Checklist items may belong to **compartments**, **kits**, **equipment**, or **ambulance-wide systems**. **[P]** Logical hierarchy:

```
Vehicle
 ├─ Compartment (e.g. "Driver-side exterior compartment 1")
 │   ├─ Kit/Container instance (physical, §10)     ← may move between vehicles/stations
 │   │   └─ Item
 │   └─ Item
 ├─ Equipment asset instance (serialized, §11)     ← may move between vehicles
 └─ Vehicle system (e.g. "Lights and sirens") ─ Item
```

Kits and tracked equipment are physical objects that can change vehicle assignment. A checklist item therefore binds to a **location slot** (vehicle compartment/system) or to a **kit/asset type**; which one is OQ-01 (it depends on the real checklists). **[P]** Safe default for equipment assets and for items outside a kit: bind to the slot for R1 and record the asset identity that was present at check time on each check record, so movement (R2) never rewrites history. **[C: owner-decided 2026-10-24, OQ-53]** A **kit is a template** (a kit type), and a vehicle keeps **zero, one or several instances** of it. Every instance needs its own checks (quantities, expirations and the rest), so an item inside a kit yields one obligation **per kit instance** (§5.1): checking one instance never satisfies the requirement for another, and a kit that is not on the vehicle is not required. A check names the kit instance it covers (§7.2).

### 4.3 Checklist templates and versions [C] (REQ §2.1)

- **CHK-1** Templates are centrally managed with vehicle-specific overrides.
- **CHK-2** Administrators draft and edit; a template version is **immutable once published**. Editing creates a new draft version.
- **CHK-3** Publishing is explicit and may specify a future **effective date-time**. The effective instant determines which obligations the version governs (§5.7).
- **CHK-4** An inspection session records and retains its original checklist version. Reopening never replaces it with newer contents.
- **CHK-5** A published version retains: the set of items; for each item its compartment/kit/equipment/system placement, display order, instructions, expected quantity, kind (see 4.4), bulk-pass eligibility, allowed outcomes, schedule rules, conditional rules and Not-Applicable rules.
- **CHK-6 [P]** Vehicle overrides are expressed as an overlay on a template version (add/remove/replace item, change quantity/schedule), versioned and published the same way. The effective checklist for a vehicle at an instant is `template version ⊕ override version`, each resolved as-of that instant.
- **CHK-7 [OQ-01]** The real items, quantities, locations, instructions, intervals and exceptions have not been supplied. Seed data in any environment must be synthetic and labeled non-authoritative. The spec defines the model only; it asserts no stocking requirement.

### 4.4 Item kinds [P]

To make bulk-pass eligibility (§7.6) and verification scoping (§8.7) decidable, every item definition has a **kind**:

| Kind | Examples (synthetic) | Result entry |
|---|---|---|
| Presence/condition | "Suction unit present and clean" | Pass / Fail / Unable / NA |
| Quantity | "Oxygen masks: 6" | Observed count + Pass/Fail derived against exact target (§10.4) |
| Measurement | "Cylinder pressure ≥ X" | Recorded value; outcome derived or confirmed |
| Special observation | Items requiring seal/expiry inspection | As defined by item |
| Vehicle reading | Odometer | Value (§6.8) |

Quantity, Measurement and Special-observation items are **never bulk-pass eligible** (relevant only if Bulk Pass is restored; deferred from R1, §7.6) (REQ §2.3: "Do not bulk-pass items requiring direct measurements or special observations").

### 4.5 Temporary stocking deviations [C] (REQ §2.1)

Authorized, vehicle-specific, time-bounded deviations record: normal quantity, approved alternate quantity, validity start/end, approver, explanation, and full history. The system alerts before expiry. Extension is an audited new revision. A crew member may request early termination; a request requires approval (approver role OQ-06). While valid, the alternate quantity replaces the normal target for quantity checks on that vehicle; outside validity the normal target applies.

## 5. Inspection obligations, windows and scheduling

### 5.1 Concepts

- **Requirement** — a rule saying "this item on this vehicle must be checked on this schedule" (from a published checklist version plus fleet schedule configuration).
- **Obligation** — one concrete, dated instance of a requirement: `(vehicle, item, kit instance, due instant, requirement version)`, where the kit instance exists only for an item inside a kit (§4.2). Obligations are what checks satisfy.
- **Check** — an individually attributed record of one outcome for one item (§7).

**5.1.1 [C]** Compliance is determined at the **obligation × item** level (and, for an item inside a kit, per kit instance). A session being complete, or all crew having participated, is never itself evidence of compliance (REQ §2.3, HO inv. 1).

### 5.2 Initial demonstration fleet schedule [C] (REQ §2.2, OQ list: decided)

All values are configuration, not fixed product behavior. **[C]** The table below preserves the current demonstration seed values (first recorded 2026-10-21). Unit labels, due times, weekday and weekend patterns, and window edges are configurable by an administrator without a code change; these examples do not prescribe a universal organization schedule.

| Unit | Due | Early-window opens | Grace ends |
|---|---|---|---|
| Ambulance 1 | 06:00 daily | 05:00 | 07:00 |
| Ambulance 1 | 18:00 daily | 17:00 | 19:00 |
| Ambulance 2 | 09:00 weekdays | 08:00 | 10:00 |
| Ambulance 2 | 12:00 weekends | 11:00 | 13:00 |
| Ambulance 3 | 18:00 daily | 17:00 | 19:00 |
| Ambulance 4 | 18:00 daily | 17:00 | 19:00 |

Default window **±1 hour**, administrator-configurable per schedule. Item-specific schedules and conditional rules override fleet defaults where defined.

**[C]** (owner, 2026-10-21) Whether an obligation can be omitted by a conditional rule is also agency configuration. The `CARRY_OVER_FROM_PRIOR_SHIFT` rule below is an agency-configurable example, not a universal rule or enabled default: it is enabled or disabled per organization, schedule or item, and is off wherever it has not been enabled. The rule's notion of a "preceding shift" comes from the agency's configured shift boundaries and roster source, so an organization without shifts or a roster source simply never has an obligation omitted this way.

### 5.3 Window semantics [OQ-02: working model accepted by owner 2026-10-15; detailed edge cases remain provisional]

The owner has not defined exactly when an unmet check is "Due" vs "Overdue". **[P] working model**, to be confirmed:

| Symbol | Meaning |
|---|---|
| `D` | due instant (e.g. 06:00 local) |
| `E = D − w_early` | earliest qualifying instant (w_early default 1 h) |
| `G = D + w_grace` | grace deadline (w_grace default 1 h) |

| Obligation state | Condition (server time `t`, obligation unsatisfied) | Display |
|---|---|---|
| Upcoming | `t < E` | Not shown as actionable |
| **Current** | `E ≤ t < D` | "Current" — may be checked now |
| **Due / Grace Period** | `D ≤ t ≤ G` | "Due" (grace running) |
| **Overdue** | `t > G` | "Overdue" |
| Satisfied | a qualifying check exists (§5.4) | "Satisfied" with who/when |

Rules **[P]**:
- **SCH-1** A check is **on time** for an obligation if its server-recorded time `t_c` satisfies `E ≤ t_c ≤ G`. An on-time check made before `D` counts as an early check for that obligation.
- **SCH-2** A check at `t_c > G` is a **late completion**: it may be linked to an obligation that is still unsatisfied and past `G` only when no unsatisfied obligation of the same vehicle+item has an on-time window containing `t_c` (SCH-6 ordering applies among overdue ones: earliest-due first). Late Pass, Fail, and Unable follow the same §5.4 rules as on-time ones, so a late Pass or Fail satisfies the *past* obligation, which stays permanently recorded as *Overdue → Completed late* with `t_c` and the lateness interval (history and reports). It clears the *current* overdue indicator because the item has now been checked. A check before `E` of every unsatisfied obligation and not late-linkable links to nothing and counts only as an ordinary recorded check (participation, history).
- **SCH-3** `w_early` and `w_grace` are separately configurable; the ±1 h default sets both to 1 h.
- **SCH-4** State is always derived from `(rule, checks, server time)`, never stored as authoritative.

### 5.4 What satisfies an obligation [C: owner decisions of 2026-10-15 for Failed and Unable to Inspect]

| Outcome | Satisfies the obligation? | Notes |
|---|---|---|
| Passed (valid) | **Yes** — on time, or as a late completion under SCH-2 | [C] |
| Not Applicable | **Yes, only if** an administrator-defined NA rule applies | [C] (REQ §2.3). Displayed as "Not applicable (rule …)", not as Passed. |
| Failed | **Yes — the check was performed [C, owner decision 2026-10-15].** The obligation shows "Inspected — failed", the deficiency is raised, and the item's attention state stays red until verified (PRN-6). | A failed item is not a missed inspection. |
| Unable to Inspect | **Not by itself [C, owner decision 2026-10-15].** A reason (and deficiency linkage) is mandatory. The obligation stays unsatisfied until a **supervisor marks it complete** (see 5.4.1). | Never displayed as Passed. |
| Invalidated check | **No** — excluded from compliance [C] | Statuses recalculated. |
| Superseded by author correction | The *correction's* outcome is the effective outcome [C] | Original retained. |
| Supervisor-suspended check | **No** — shown "Known Deficiency — Check Suspended"; not a Pass [C] | §8.9. |

#### 5.4.1 Supervisor completion of an Unable-to-Inspect obligation [C intent; P mechanics]

- **UTI-1 [C]** A crew member recording Unable to Inspect must give a reason; the obligation then remains open and visible to supervisors.
- **UTI-2 [C]** A supervisor (or administrator) may mark that obligation **complete**. **[P]** Mechanics: an append-only `obligation_determination` of type `SUPERVISOR_COMPLETED` carrying actor, instant, mandatory explanation, and a reference to the Unable-to-Inspect check(s). It is distinct from a Pass: reports and the UI show "Completed by supervisor — unable to inspect" (RPT-7). It does not close or alter the linked deficiency and does not change operational status.
- **UTI-3 [C: owner-approved 2026-10-15, OQ-45]** The determination is itself invalidatable by an administrator with an explanation; the original is retained. If the underlying Unable-to-Inspect check is later **invalidated**, the supervisor determination is voided with it and the obligation returns to unsatisfied. If its author **corrects** it to Pass/Fail, the corrected outcome governs per §5.4 and the supervisor determination remains in history as superseded. Either way the linked deficiency is unaffected (INV-3). **[C: owner-decided 2026-10-25, OQ-41]** A supervisor may complete an obligation whose Unable-to-Inspect check they recorded themselves; reports flag that case.

### 5.5 Overlaps and multiple obligations [OQ-03]

- **SCH-5 [P]** Link cardinality: `check_obligation_link` rows are unique per `(check_id, obligation_id)`. By default a check has **exactly one** link. A check may have more than one only when each additional link carries the `shared_rule_id` of an administrator-defined, audited rule that explicitly declares the sharing (SCH-6); a link without a `shared_rule_id` is rejected if the check already has a link. One check never silently counts toward more than one obligation.
- **SCH-6 [P]** When a check is recorded and several unsatisfied obligations for the same vehicle+item are eligible (windows containing `t_c`, possible only with overlapping schedules; or late-linkable ones under SCH-2), the check links to the earliest-due one. A shared-satisfaction rule (e.g. a shared-parent item) may additionally link it to the others it names. Invalidating or correcting the check re-derives all its links together (§19.1). For an item inside a kit only the obligations of the kit instance the check names are eligible; a check never links to another instance's obligation, not even through a shared-satisfaction rule. Required tests: overlapping windows (AC-30), two identical kit instances with one checked, late vs on-time precedence, shared-rule links, and rejection of an unruled second link.
- **SCH-7 [P]** Repeated checks in one window are all retained; the earliest valid qualifying check satisfies the obligation, and later ones contribute to participation counts and history (REQ §2.3).

### 5.6 Conditional obligations [C / P] (REQ §2.2)

Some obligations are omitted when stated conditions hold. **[P]** Conditions are expressed as a closed, versioned set of rule types, **not** free-form expressions (no `eval`; see [engineering standards](docs/engineering-standards.md#core-principles)). The initial rule type is:

**`CARRY_OVER_FROM_PRIOR_SHIFT`** — the obligation at `D` is *not required* if (a) every person on the effective roster at `D` was also on the effective roster for the immediately preceding shift that ends at `D`, **and** (b) a qualifying, valid, non-invalidated check for the same item was completed during that preceding shift. Example (synthetic): the 06:00 glucometer check is omitted when all crew working at 06:00 were on the midnight–06:00 shift and the midnight glucometer check passed. If **any** crew member begins duty at `D`, the obligation is required. [C] example from REQ §2.2.

Rules **[P]**:
- **CND-1** The evaluation records its inputs (rule version, roster snapshot and its source/age, the check relied on) and its result as an immutable **obligation determination** event, so that an audit can reconstruct *why* an obligation was waived.
- **CND-2** "Effective roster" = scheduled roster (Humanity, §15.2) overlaid with the actual-roster correction(s) that govern the time (§15.3, including how concurrent corrections are resolved).
- **CND-3** Fail closed: if the roster is unavailable/stale beyond the staleness limit (§15.2), or the determination cannot be computed, the obligation is **required**.
- **CND-4** A waived obligation displays as "Not required — carried over (rule …)", never as Passed.
- **CND-5** A later roster change that invalidates the waiver reinstates the obligation (re-evaluation is an event, not an in-place edit). If that reinstatement occurs after `G`, it is shown overdue from `G`; the timing consequence is OQ-37.
- **CND-6** New rule types require a spec amendment (§25).
- **CND-7 [C: owner-decided 2026-10-23, OQ-51]** Re-evaluating an existing waiver (CND-5) reinstates the obligation only when something newer than the waiver's evidence invalidates it within its dependency window (the preceding shift through `D`): a roster change, including an actual-roster correction, that breaks condition (a) or leaves no preceding shift ending at `D`; a refresh reporting roster data unavailable for that window; disagreeing corrections (§15.3); or the relied-on check being invalidated or no longer qualifying for condition (b). Roster data that has only aged past the staleness limit (HUM-3) without changing does **not** reinstate a waiver granted on fresh data; the validity of the relied-on inspection is what matters. The staleness limit still governs new determinations: data older than the limit at evaluation time cannot create a waiver (CND-3).

### 5.7 Which requirement version governs [OQ-05]

**[P]** An obligation is created bound to the requirement version **effective at its due instant `D`**. Consequences:
- Publishing a version with a future effective date does not change obligations already due before that date.
- A session's pinned checklist version (CHK-4) governs what the crew member *sees and records* in that session; the obligations used for *current* due/overdue status are the ones bound at creation, resolved as in this rule. The UI shows both (original version and current obligations) and never injects new items into an old session silently.
- An immediately-effective publish takes effect for obligations whose `D` is at or after the publish instant; for obligations already created and in-window it does not apply: the administrator option "apply to open obligations" is **not offered** in this release. **[C: owner-decided 2026-10-23, OQ-52]** The owner may reconsider this in a future revision; offering the option would need an amendment (§25.4) that also settles its audit and its effect on checks already recorded against the open obligation.

### 5.8 Generation and time basis [P / OQ-25]

- **TIME-1** Obligations are materialized by a scheduled job ahead of time (default horizon: through the next 48 h) and are idempotent on `(vehicle, item, kit instance, D, requirement_version)`; a missed job run is repaired by the next run, never by duplicate rows.
- **TIME-1a [C: owner-decided 2026-10-24, OQ-53, OQ-54]** For an item inside a kit the generator materializes one obligation per kit instance that is active on the vehicle, in the item's kit slot, at that `D`; with no such instance it materializes none. Which instances those are is read from the kit assignment record (`kit_assignment_event`, §18), never from the mutable `kit_instance` row, so the result is the same when recomputed as-of (REG-6). An instance added later is required only for due instants at or after its recorded activation: an inspection whose window is already open at that moment is not required for it, and earlier instants are not backfilled (as for return to service, §5.9). An instance removed, deactivated or moved to another vehicle stops being required from the recorded removal instant: its obligations with a due instant after the removal are retired, and earlier ones keep their state, so a removal can never erase a missed inspection; the history is kept as-of. A move between vehicles is a removal from one vehicle plus an addition to the other, and a temporary absence (for example restocking) is a gap with no backfill. An assignment takes effect at or after the instant it is recorded and is never backdated, as for requirement bindings (owner-approved 2026-10-24). A missing kit instance (an item outside a kit) is compared as a value, so two obligations cannot differ only by a missing instance.
- **TIME-2** All instants are stored in UTC; schedules are defined in the station's IANA time zone (single configured zone; exact zone OQ-25). `D` is computed from local wall-clock time.
- **TIME-3** Daylight-saving rules **[P]**: a nonexistent local time (spring forward) resolves to the first valid instant after the gap; an ambiguous local time (fall back) resolves to its **first** occurrence. Window arithmetic is on absolute instants, so a window may span 59–61 minutes' wall time on a transition day.
- **TIME-4** Server time is authoritative for every timestamp used in compliance; client clocks are never trusted (REQ §9).

### 5.9 Out-of-service exemption [C] (REQ §2.2)

While a vehicle's operational status is **Out of Service**, routine obligations are not generated for that interval; the interval is displayed as "Exempt — out of service", distinct from Pass. Service status and resumption are explicit, audited supervisor actions (§9). A change of crew, or equipment use after a call, does **not** itself create an obligation to repeat an entire vehicle inspection [C].

**[OQ-31]** Whether returning to service requires an inspection before the next scheduled obligation. **[P]** Safe default: no automatic obligation; the supervisor may require an ad hoc inspection, recorded as an audited, scoped obligation.

**ADH-1 [C: owner-approved 2026-10-30, [issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158)].** A Supervisor or Administrator may explicitly require a one-off inspection of selected checklist items on a vehicle, with a mandatory explanation and future due instant `D`. Each selected item names its physical kit instance, or no kit. The request pins the published requirement effective at `D`, using only configuration already recorded when requested. Its window opens when the request is recorded (`E`), ends at `D` (`G = D`, no additional grace), and is never retroactive. Each request has a separate identity, even at the same vehicle/item/kit/due instant as a routine obligation or another request; it is not in the routine selection stream. Subsequent generation, publication reconciliation and kit changes do not replace or retire the explicit request. It is non-routine, so an Out-of-Service interval does not exempt it; carry-over waivers do not apply. Satisfaction and link cardinality follow §5.3–§5.5, including one check satisfying only one obligation unless an explicit shared rule allows more. Returning to service alone creates nothing.

## 6. Inspection sessions

### 6.1 Role of a session [C]

A session organizes work; it **never determines compliance** (PRN-1). Compliance derives only from valid checks linked to obligations. Sessions group a vehicle, a checklist version, participants, an optional actual-roster correction, and the checks/odometer entries made within them.

### 6.2 States [P]

| State | Meaning |
|---|---|
| **Open** | Active; checks may be recorded. |
| **Incomplete** | Auto-closed by inactivity (§6.3), or manually ended with unmet items. Progress preserved. |
| **Closed** | Ended by a participant when all items in its checklist version have a valid current outcome, or ended explicitly. Informational only. |

Reopening moves Incomplete (or Closed) → Open and is an audited event [C]. Multiple sessions on the same vehicle may be open concurrently [C: concurrent crew members]. **[P]** An active user with an organization role joins an existing open session for the vehicle by default; starting a separate one is allowed even when another session is open. **[C: owner decision 2026-10-24, [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)]** Only a participant may reopen that same ended session or manually end it; a nonparticipant may start a new session instead.

### 6.3 Inactivity auto-close [C / OQ-04]

- **SES-1 [C]** A session with no activity for **6 hours** closes automatically as **Incomplete**, preserving every check and progress record.
- **SES-2 [OQ-04 → P]** "Activity" means a **persisted business event** in the session: a committed check, correction, odometer entry, deficiency report or link made from the session, roster confirmation, or reopen. Passive viewing, scrolling, websocket/heartbeat traffic, and failed/unacknowledged saves do **not** reset the timer. Reopening starts a new 6-hour window from the reopen instant.
- **SES-3 [C: owner decision 2026-10-24, [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)]** A participant may reopen a Closed or Incomplete session without supervisor approval. The reopen is audit-logged (actor, instant, session); a nonparticipant starts a new session instead.
- **SES-4 [C]** Reopening refreshes current requirement statuses; no "what changed" summary is required. Completed checks keep their original timestamps and satisfy a *current* obligation only if still valid and within that obligation's window.
- **SES-5 [C]** Work may be resumed by another crew member without duplicating valid completed checks.
- **SES-6 [P]** The auto-close job is idempotent and tolerant of delay: closing is stamped with the *activity-based* close instant (last activity + 6 h), not the job's wake time, so the audit record is correct even if the job runs late.

### 6.4 Odometer and engine hours [C / OQ-12, OQ-18]

- **ODO-1 [C]** Every routine ambulance inspection requires an odometer entry.
- **ODO-2 [OQ-12 → P]** Because no single crew member "completes" a session, odometer is modeled as a **vehicle-level odometer obligation attached to each scheduled routine inspection** (same window as that inspection's obligations; any crew member may satisfy it; one entry per obligation; others see it satisfied). Crew members are not each required to re-enter it.
- **ODO-3 [C]** Anomalous readings (**[P]** lower than the previous valid reading; or exceeding the previous by more than a configurable plausibility threshold) require correction or explicit confirmation, recorded with who confirmed. **[P]** Until an administrator sets one, the plausibility threshold is **1,000 miles (1,600 km)** between consecutive valid readings (a reading exactly at the limit is normal); every change is kept as a numbered revision (AUD-4). A vehicle's readings keep one unit (mi or km). Engine hours (ODO-4) have no threshold: only a value lower than the previous valid one needs confirmation (OQ-55). When an author corrects an earlier odometer or engine-hours reading, if the replacement value would exceed the next effective reading in the vehicle's sequence (making that later reading lower than the corrected value), the correction requires explicit confirmation, recorded with who confirmed and retained in the append-only history. Both readings and their sequence positions remain unchanged; later readings are not silently rewritten or invalidated (**[C: owner decision 2026-10-26, [issue #125](https://github.com/JustApu/CheckYourAmbulance-archive/issues/125), OQ-58]**).
- **ODO-4 [C]** Engine hours may be entered but are **not mandatory** in the initial release [OQ-18 for when this changes].
- **ODO-5 [P]** Odometer entries are append-only records subject to the same author-only correction and administrator-invalidation rules as checks.

### 6.5 Views and filters [C] (REQ §2.4)

- **VW-1** Switchable **Priority** (attention-needed first) and **Normal** (equipment/compartment order) views. Each crew member's preferred view persists across sessions **and devices**. (The decided list in OQ says "per EMT and device"; REQ §2.4 says "across devices". **[P]** per-crew-member, synchronized across devices; reconcile if the owner meant a per-device override — OQ-38.)
- **VW-2** A **Needs Attention Only** filter works in either view and is remembered per crew member. It includes actionable due, overdue, incomplete and unresolved items **excluding** supervisor-suspended routine checks, which stay visible in full checklists, deficiency reporting and dashboards.
- **VW-3** Relevant known deficiencies are shown **beside their checklist items before inspection**, and the vehicle's operational status is prominent.
- **VW-4** QR/barcode entry points for vehicle and kit, with manual navigation fallback. QR payloads **[P]** carry only an opaque identifier, never a credential, and entry still requires authentication.

## 7. Recording checks, concurrency and corrections

### 7.1 Outcomes [C] (REQ §2.3)

**Passed**, **Failed**, **Unable to Inspect**, **Not Applicable** (only where an administrator-defined rule is valid). Unable to Inspect requires an explanation and deficiency linkage (create or link). An outcome that is invalid for the item or that violates its rule must be rejected server-side and does not satisfy anything.

### 7.2 The check record [P]

An append-only record:

```
check_id (server-generated), operation_id (client UUID, unique per org), organization_id,
vehicle_id, item_id, checklist_version_id, session_id (nullable),
obligation_id (nullable, via link), kit_instance_id (required for an item inside a kit, else null), asset identity at time (nullable),
outcome, observed_value/quantity (nullable), explanation (nullable),
actor_user_id, actor_roles_at_time, roster_context_ref,
server_recorded_at, client_observed_at (informational),
bulk_group_id (nullable; reserved, unused in R1), basis_check_id (see 7.4),
acknowledges_check_id (nullable, CON-3), purpose (ROUTINE | VERIFICATION), verification_for_deficiency_id (nullable),
state: EFFECTIVE | SUPERSEDED | INVALIDATED  (derived from event rows, see 7.8)
```

Rows are never updated or deleted by application code; state changes are separate append-only event rows (correction, invalidation).

### 7.3 Auto-save and acknowledgement [C] (REQ §2.3, §9)

- **REC-1** Tapping an outcome saves immediately; there is no Save/Submit button.
- **REC-2** The UI shows each item as **Pending** (sent, unacknowledged), **Saved** (server acknowledged), or **Failed** (error, with retry). Only a **server acknowledgement** means the entry is committed.
- **REC-3** All viewers receive updated statuses without manual refresh (§17.3). If the live channel is lost, the UI says so prominently and shows data as possibly stale [OQ-15 for wording].
- **REC-4 [P]** Each client write attempt carries an **operation id** (UUID) — the idempotency key. A client retries an unacknowledged write **only with the same operation id**. The server enforces `UNIQUE(organization_id, operation_id)` and returns the originally committed result for a replay. A transport failure is treated as ambiguous (per [engineering standards](docs/engineering-standards.md#safe-administrative-automation)): the UI shows "unconfirmed" and retries with the same id until the server answers. **Lost response:** the replay returns the stored committed result; if nothing had committed, the server evaluates the write fresh. A conflict warning (CON-3) is **not** a commit and does not consume the operation id; an identical retry is re-evaluated against current state.

### 7.4 Concurrent checking and duplicate warning [C / OQ-10]

- **CON-1 [C]** Different crew members may check different items concurrently. There is **no locking** of items.
- **CON-2 [C]** If a crew member is about to record an item that another crew member has just recorded, the system **warns and offers to proceed or cancel**. If they proceed, **both** records are preserved independently.
- **CON-3 [OQ-10 → P]** "Just recorded" is defined by *the viewer's basis*, not a wall-clock guess: each write carries `basis_check_id` = the newest effective check the client had displayed for that item+obligation. If the server's newest effective check for that item+obligation differs (a newer one exists), the server **rejects the attempt with a conflict warning** containing the newer check (who, when, outcome). The user's decision to proceed is a *new decision* and a *new* operation id, submitted with `acknowledges_check_id` = the id of the newer check shown in the warning. At commit the server compares `acknowledges_check_id` with the then-newest effective check: if still equal, the independent record commits; if a **further** check landed between warning and acknowledgment, the server returns a fresh warning for that newer check and commits nothing. A check from the same actor as the newer one is not a conflict. For an item inside a kit the comparison is per kit instance: a newer check on another instance is not a conflict. Required tests: lost response on first attempt, replay after warning, and a concurrent check between warning and acknowledgment (AC-31).
- **CON-4 [P]** The conflict warning applies to every outcome including Fail; a Fail is never silently dropped because a Pass raced it (PRN-6). If a Pass and a Fail for the same item both commit, both are retained: the obligation is satisfied/inspected per §5.4, the item's attention state remains red while the failure's deficiency is unresolved.
- **CON-5 [P]** Server commits are serialized per `(vehicle, item)` by a database transaction so that CON-3's comparison and the insert are atomic.
- **CON-6 [OQ-15]** Exact warning text, stale-view handling, and whether to indicate another crew member currently viewing an item are open; presence indicators are not part of R1 unless the owner decides otherwise.

### 7.5 Pass-then-fail and fail-then-pass [P]

Statuses use **two independent dimensions** (PRN-3, PRN-6):
- **Obligation state** (§5.3/§5.4): Satisfied / Inspected–failed / Current / Due / Overdue …
- **Item attention state**: *Clear* only if no unresolved (open or pending-verification) deficiency applies to the item, and the latest effective check is not Failed/Unable. Otherwise *Attention*, regardless of later passing checks.

A later Pass does not close a deficiency. Only §8 transitions do.

### 7.6 Bulk pass [C / OQ-11] — deferred from R1

**Deferred [C]** (owner decision 2026-10-20, OQ-11). Bulk Pass is not part of R1: no R1 UI, API or acceptance test is required, and `bulk_group_id` / item `bulk_eligible` are reserved and unused in R1. BLK-1–BLK-5 are retained unchanged as the design to use if a later release restores the feature. The requirement text below is not withdrawn.

- **BLK-1 [C]** A crew member may select eligible items and mark them Passed in one action. The system persists **separate attributed check records** per item (sharing a `bulk_group_id`).
- **BLK-2 [C]** Ineligible: items requiring direct measurement or special observation, items with an open/pending deficiency (bulk pass must not override a deficiency), and items under supervisor suspension (must not bypass suspended items).
- **BLK-3 [OQ-11 → P]** Eligibility is an explicit boolean on the item definition (default **false**). Confirmation is **required for every bulk action** (summary: vehicle, number of items, listed names) until the owner decides otherwise.
- **BLK-4 [P]** Bulk submission is processed per item inside one request: items with a newer effective check than the client's basis (CON-3) are **excluded and returned** as conflicts for individual decision; the rest commit. The response lists committed, conflicted and rejected items. A concurrent Fail on an item always wins attention state (§7.5).
- **BLK-5 [P]** A bulk request carries one idempotency key per item (derived deterministically from the request key), so replay cannot duplicate any item.

### 7.7 Corrections by the original author [C] (REQ §2.3)

- **COR-1** Only the original author may correct a check. Supervisors and administrators cannot correct another user's check; a supervisor may record a **fresh independent** check instead. Reason is optional.
- **COR-2** A correction is a **new linked record**; the original is retained and marked superseded. The effective outcome is the correction's. Attribution of both is kept.
- **COR-3 [C]** If the author corrects an *accidentally entered* Failed to Passed, the linked deficiency is marked **Reported in Error** (history retained). This baseline rule is confirmed; the extra conditions in COR-4 are owner-approved (OQ-42, 2026-10-15).
- **COR-4 [OQ-07, OQ-08, OQ-09; additional conditions approved by owner 2026-10-15 (OQ-42)]** Two distinct user flows: **"Correct entry"** (fix a data-entry mistake) and **"Record repair/verification"** (§8.6). Correction never implies repair. Additional conditions (owner-approved 2026-10-15): "Reported in Error" is applied only when the correction is a Failed→Passed through *Correct entry*, no *other* effective Failed or Unable to Inspect check supports the deficiency (an item that could not be inspected is unverified, not a resolved defect), and no corrective action or verification has been recorded for it. Otherwise the correction is still recorded, the deficiency stays open, and a supervisor is notified to resolve it.
- **COR-5 [OQ-32]** Whether corrections are time-limited (for example, only until the obligation window closes or until deficiency work begins) is undecided; no limit is applied by default.
- **COR-6 [P]** A correction itself may be corrected by the same author; the chain is preserved.

### 7.8 Administrator invalidation [C] (REQ §2.3)

- **INV-1** Only administrators may invalidate another user's check (or odometer entry). **Mandatory explanation.** The original data is preserved, never overwritten or deleted.
- **INV-2** An invalidated check is excluded from compliance and all statuses are recalculated (derivation is reproducible, §19).
- **INV-2a [C: owner-approved 2026-10-15, OQ-43]** *Chain semantics.* A correction chain (original plus the author's corrections) is one logical check. An administrator invalidates **the chain**, whichever row they select; the entire chain is excluded and **no earlier outcome is revived** (reviving an old, possibly accidental Failed or Passed would fabricate a result no one currently stands behind). If the chain was invalidated, a later correction by the author is refused (they record a fresh check). If an administrator wants only to disregard a correction, they invalidate the chain and a fresh check is recorded. Downstream recompute: obligation links of the chain are voided (obligations re-derived); deficiencies supported by the chain stay open (INV-3) with the chain marked invalid in their evidence list; if the chain was a verification, INV-4 applies; any as-of report at `T` before the invalidation shows the chain as then effective (REG-6).
- **INV-3 (critical) [C]** Invalidating a Failed check **does not close or cancel its deficiency**. The deficiency stays open until it is explicitly closed under §8.8: an Administrator may close it generally, and a Supervisor may close it only when its current correction-time verification scope is empty.
- **INV-4 [C: owner accepted proposal 2026-10-15]** Invalidating a *verification* check that had caused automatic closure **reopens** the deficiency (fail toward open) and notifies its assignee and supervisors.
- **INV-5 [OQ-33]** Whether an administrator can invalidate their own check is undecided; **[P]** default: no — they use correction.
- **INV-6 [P]** Invalidation is not undoable; a mistaken invalidation is addressed by a new fresh check and a supervisor/administrator note, never by editing the invalidation row.
- **INV-7 [C: owner-approved 2026-10-20, OQ-48, [issue #31](https://github.com/JustApu/CheckYourAmbulance-archive/issues/31)]** If an author's Failed→Passed Correct-entry marked a deficiency **Reported in Error** under COR-3/COR-4, later administrator invalidation of that correction chain **reopens that deficiency to Open**. The reopening is an attributed lifecycle event in the same transaction as the invalidation; the invalidated chain remains in its evidence list marked invalid. No earlier check outcome revives, no repair is inferred, and operational status is unchanged. This applies to the deficiency still in Reported in Error from that chain, not an independently administrator-closed deficiency. The owner selected "Reopen" and explicitly approved amendment 1.3.0 on 2026-10-20 (§25.6).

## 8. Deficiency lifecycle

### 8.1 Entity [C] (REQ §4)

A deficiency concerns an item, kit, compartment, or the whole vehicle; may be reported from an inspection or independently; has category/severity, photos/documents, assignee, deadline, and a timestamped progress history. A deficiency is an independent record: it is **separate from the failed check** that may have spawned it, and correction/verification/closure records are separate from that check (REQ §4, HO inv. 5).

### 8.2 Status model [P]

Status is derived from the latest lifecycle event; events are append-only.

| Status | Meaning |
|---|---|
| **Open** | Reported, not yet being worked or newly reopened. |
| **In Progress** | Assigned/worked. (Folded into Open if the owner prefers fewer states.) |
| **Awaiting External Dependency** | Waiting on a third party (§8.5). |
| **Pending Verification** | A correction was recorded; verification not yet passed. |
| **Closed — Verified** | Closed automatically by successful verification. |
| **Closed — Administrator** | Closed explicitly by an administrator without verification (explanation required). |
| **Closed — No Current Verification Items** | Closed explicitly because checklist or kit changes left no current item to verify (explanation required). |
| **Reported in Error** | Cancelled via COR-3/COR-4 (or later-defined OQ-07 paths). |
| **Reopened** | Event; status returns to Open (§8.7). |

Allowed transitions **[P]**:

```
Open ──assign/start──▶ In Progress ──correction recorded──▶ Pending Verification
Open/InProgress ◀──▶ Awaiting External Dependency
Pending Verification ──verification pass (all scope)──▶ Closed—Verified   (automatic)
Pending Verification ──verification attempt fails──▶ In Progress
any open status ──administrator close (+explanation)──▶ Closed—Administrator
Open/In Progress/Awaiting External Dependency ──supervisor/administrator close when current scope is empty (+explanation)──▶ Closed—No Current Verification Items
Open/InProgress/PendingVerification ──author Correct-entry (COR-4)──▶ Reported in Error
Closed—Verified ──added verification required (§8.7) / INV-4──▶ Open (Reopened)
Closed—Administrator ──administrator reopen──▶ Open (Reopened)
Closed—No Current Verification Items ──administrator reopen──▶ Open (Reopened)
Reported in Error ──invalidation of its COR-3/COR-4 chain (INV-7)──▶ Open (Reopened)
```
Any transition not listed is rejected server-side.

### 8.3 Creation, links and suggestions [C] (REQ §4)

- **DEF-1** Reporting a new deficiency shows related existing deficiencies and suggests possible matches; the crew member decides whether to link.
- **DEF-2** Crew members may add and remove links with an audit trail. Links are informational: they do **not** merge responsibilities, transfer assignees, or trigger closure of any other deficiency.
- **DEF-3 [OQ-08 → P]** One deficiency may be supported by **several** failed checks (evidence list). Correcting/invalidating one supporting check never closes the deficiency while another effective failed check or other evidence remains; and per INV-3, invalidation never closes it in any case.

- **DEF-8 [C: owner decision 2026-10-25, OQ-56]** *Severity.* Every deficiency has a **severity level**, chosen by the crew member who reports it from the organization's current levels. Levels are administrator-configured data, not a fixed list: each has a code, a label, a rank (least to most severe) and a **concise criteria description**, kept as numbered revisions (AUD-4). An administrator may add a level, reword or reorder one, or retire it with a new revision; a retired level is not offered, and a deficiency keeps the revision it was reported under. Wherever a reporter chooses a severity, the label and criteria of every offered level are shown beside the choice so the selection can be made accurately. **[P]** The starting levels are: **Low** (cosmetic or minor; does not affect patient care or safe operation; fix at convenience), **Medium** (reduces convenience or a safety margin and a workaround exists; fix in normal scheduling), **High** (impairs equipment or capability needed on a call; fix soon, supervisor should review) and **Critical** (prevents safe patient care or safe driving, or is a safety or regulatory failure; tell a supervisor now). Severity informs people only (OPS-2). A deficiency's severity may be **re-graded** after it is reported by a role holding the §3.3 permission (**[P]** supervisors and administrators): to another current level, with a mandatory explanation, as an append-only deficiency event that keeps the previous and the new level, the actor and the instant, and an audit record (AUD-1). The original grade is never overwritten, and a re-grade does not reassign, extend, close or otherwise change the deficiency.

### 8.4 Deadlines, extensions, age [C] (REQ §4)

- **DEF-4** Each deficiency has an assignee and a deadline, with all revisions retained. The assignee must be an active user who holds the Crew, Supervisor or Supply Officer role [C: owner decision 2026-10-27, [issue #146](https://github.com/JustApu/CheckYourAmbulance-archive/issues/146)].
- **DEF-5** A crew member may request an extension (reason + proposed date). A supervisor approves, denies, or chooses a different date with explanation. The approved date governs overdue notices. Extensions are unlimited; long-running cases are highlighted.
- **DEF-6** Age since original report is tracked separately from deadline. Administrators configure dashboard age thresholds. There are **no** independent age-based notifications.
- **DEF-7 [C]** Authorized *acknowledgment* may suspend escalation for a configured period, requiring explanation and an action plan; it expires automatically; renewals are audited. Who is authorized: OQ-06/OQ-20.

### 8.5 Awaiting External Dependency [C] (REQ §4)

Mandatory explanation; **one** status/dependency record per deficiency (further detail goes in progress notes); optional follow-up date shown on the dashboard **without** an automatic reminder. Crew members may update the status and details (audit logged) but cannot bypass the deadline or escalation. Each revision of the record is an appended event, never overwritten text. When the dependency ends the deficiency returns to Open or In Progress as the person chooses; if they choose neither, it returns to the status it was in before it began waiting [C: owner decision 2026-10-27, [issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149)]. A progress note (§8.1) is a timestamped, attributed, append-only entry on an active deficiency, added by the roles who may update the dependency record (§3.3) [C: owner decision 2026-10-27, [issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149)].

### 8.6 Correction and verification [C] (REQ §4)

- **VER-1** Correction may be done by the original crew member or another crew member. The same crew member may both correct and verify.
- **VER-2** A genuine deficiency closes only after **successful verification** or an explicit closure allowed by §8.8. Successful verification closes the linked deficiency automatically.
- **VER-3 [C]** Verification scope: for **kit/compartment** defects, re-check the **entire affected kit/compartment**, resolving current checklist membership and physical kit instances when the correction is recorded, including items added since the report; for **vehicle-level** defects, re-check the **affected systems**, not the whole truck. For a vehicle-level deficiency, affected systems are the affected items recorded when it was reported, plus any supervisor expansion [C: owner decision 2026-10-27, [issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153)]. Supervisors may **expand** scope with audit history. For an item-level deficiency the scope is one row for each active physical kit instance of a kit-defined item, or one row for an item that is not in a kit, and each row needs its own qualifying pass. Supervisor expansions are kept with the deficiency: every later correction's scope also includes each expanded item, and each expanded physical kit instance while it still holds that item on the vehicle's current checklist, resolved when the correction is recorded like the rest of the scope [C: owner-approved amendment 1.22.0, [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)].
- **VER-4 [P]** Scope is materialized as append-only, revisioned rows when the correction is recorded. Each row identifies the item, its checklist definition, the physical kit instance (or no-kit item), and the instant after which a qualifying check must be recorded. A verification check is an ordinary check with `purpose=VERIFICATION` and `verification_for_deficiency_id` set; it counts only for the matching physical kit instance, if any, and only if recorded **after** the correction instant, effective (not invalidated/superseded to a non-Pass), and Passed. When every scope row is satisfied, the deficiency transitions to Closed—Verified in the same transaction (idempotent). A verification check is accepted only for a deficiency on the check's vehicle that is Pending Verification, and only for an item and kit instance in the newest scope revision; it is Passed or Failed (Unable to Inspect does not verify). A Pass in an older revision never satisfies a newer one, except across a supervisor expansion: an expansion appends a new revision that repeats every row of the revision it expands with that row's instant and adds the new rows with the expansion instant as their instant, and an effective Pass that satisfied a repeated row still satisfies it. Passes never carry over to the revision of a new correction [C: owner-approved amendment 1.22.0, [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)]. A Failed verification check is evidence for the deficiency it verifies and for no other (VER-5).
- **VER-5 [C: owner decision 2026-10-27]** A verification attempt that Fails does not close the deficiency. It records new evidence (§8.3) and returns the deficiency from Pending Verification to In Progress. The recorded correction and its verification scope remain in the history; a later correction creates a new correction event and a new scope revision.
- **VER-6 [P]** Ordinary routine checks can double as verification only if recorded with `purpose=VERIFICATION` against that deficiency; this prevents accidental closure and honors "no false pass".

### 8.7 Added verification after closure [C] (REQ §4)

If a supervisor adds required verification after closure, the deficiency **reopens automatically** with history preserved; only its **current assignee** is notified (previous assignee is retained by default unless reassigned). The supervisor must set a **new** resolution deadline; the former deadline stays in history. The supervisor names the items (and physical kit instances) to add; they are recorded as a supervisor expansion and are verified after the next correction [C: owner-approved amendment 1.22.0, [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)].

### 8.8 Explicit closure without verification [C]

General closure of a deficiency without verification is **Administrator-only** and always an explicit act. **[OQ-34 → P]** An explanation is mandatory; the closing administrator is recorded as the actor; closure type is distinguishable from verified closure in all reports.

**[C: owner-approved amendment 1.19.0 (2026-10-30, [issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153))] Empty current scope.** Checklist publication or kit-assignment changes can remove every item from a deficiency's correction-time verification scope after it was reported. Before a correction has materialized a verification scope, correction is refused with `EMPTY_SCOPE` and records no correction event; the system does not fall back to report-time items. A Supervisor or Administrator may instead explicitly close the deficiency through a distinct, attributed empty-scope closure event, with a mandatory explanation and the status Closed — No Current Verification Items. The command re-resolves scope under the checklist and vehicle locks and refuses if any current verification item remains. A deficiency already in Pending Verification keeps its materialized scope and does not use this exception. The existing Administrator-only closure of any active deficiency remains unchanged. Only an Administrator may reopen either explicit-closure status.

### 8.9 Known-deficiency check suspension [C] (REQ §4)

- **SUS-1** A supervisor may suspend *specific* repetitive routine inspection checks for a known defect, explicitly choosing which checks. Display: "Known Deficiency — Check Suspended".
- **SUS-2** Suspension is **not** a Pass, affects only the explicitly suspended checks, and never changes operational status. Unaffected checks remain required (e.g. continue counting the remaining masks).
- **SUS-3** Suspension **ends automatically** when verification closes the deficiency.
- **SUS-4 [P]** If the deficiency is closed by administrator instead of verification, or is Reported in Error, suspension also ends (a suspension with no open deficiency is invalid); the closing event lists the suspensions ended. Suspended obligations are shown as "Suspended", never "Satisfied".

## 9. Operational status

- **OPS-1 [C]** Statuses: **In Service**, **Limited Service**, **Out of Service**. Only supervisors/administrators set status, **manually**, with a required explanation, history, and notification. Crew members may *request* a change for supervisor decision.
- **OPS-2 [C]** Severity, deficiency events, expired certifications, failed checks and regulatory overdue status **never** change status automatically. The system may raise alerts and recommendations only.
- **OPS-3 [OQ-21 → P]** Limited-service categories (e.g. which functions are restricted) are undefined; R1 records limited service as free-text restrictions plus the mandatory explanation. Who may approve crew change requests: supervisor (as above) pending OQ-21.
- **OPS-4 [P]** A status-change request is a record with requester, reason, target status, decision, decider and decision explanation; denial requires an explanation.
- **OPS-5 [C]** While Out of Service, routine obligations are exempt (§5.9).
- **OPS-6 [P]** The status is an audited, append-only event stream; "current status" is the latest event.

## 10. Kits, inventory and expirations (primarily R2; minimum sealed-kit workflow in R1, §10.1)

- **STK-A [C]** Physical kits, bags and containers are tracked individually with identifiers, location/vehicle assignment, contents, seals, and full movement history (REQ §3).
- **STK-B [C]** An **intact valid seal** may substitute for inspecting contents until the **earliest contained expiration**. A broken or expired seal requires a full contents check. Seal breaks/reseals are recorded events with actor and time. **[P]** The substitution is a rule in the obligation engine (a `SEAL_SUBSTITUTION` rule type, subject to §5.6's closed-rule policy) and records which seal was relied upon.
- **STK-C [OQ-14]** Periodic/random full-content audits of sealed items are desired; tentative weekly cadence is **not decided** (frequency, random vs scheduled, reseal handling, exemptions). R1 implements only a **manual, supervisor-requested** full audit (SEAL-7); scheduled or random selection is not implemented in R1.
- **STK-D [C]** Quantities have **exact** configured targets; both too few **and too many** are flagged, with immediate correction available in the workflow.
- **STK-E [C]** Item expirations with configurable advance alerts. **No lot-number tracking.**
- **STK-F [C]** Station inventory: storage positions (room/cabinet/shelf), transfers, restocking ambulances with quantity deductions and attribution, receipts (quantity/location/expiration), low-stock alerts and reorder thresholds.
- **STK-G [C]** Generic supply catalog (no mandatory branded SKU); purchasing may choose equivalent products and record multiple vendors/prices without an equivalence-approval workflow; consistent units of measure across order, receipt, store and stock.
- **STK-H [C]** Purchase orders: vendors, pricing, history, partial receipts, outstanding quantities, completion.
- **STK-I [OQ-22]** Starting storage positions, catalog units, PO workflow and vendor fields are undefined.

### 10.1 R1 minimum sealed-kit workflow [C/P] (owner decisions, [issue #18](https://github.com/JustApu/CheckYourAmbulance-archive/issues/18), 2026-10-20)

Prototype reference: `prototype/sealed-kit.html` ([PR #29](https://github.com/JustApu/CheckYourAmbulance-archive/pull/29)), non-normative.

- **SEAL-1 [C]** R1 includes, for each sealed kit, its identity (`kit_instance`), listed contents with exact expected quantities and expirations (STK-D), and attributed seal events (`kit_seal_event`). Full movement workflows (transfers between stations) and scheduled audits remain R2; R1 keeps only the kit assignment record that TIME-1a needs (§18). The actual list of sealed kits and contents is owner-supplied (OQ-01).
- **SEAL-2 [C]** Physical seals have **no unique serial**. The crew member who seals a kit handwrites their crew ID and the **earliest expiration among all listed contents** on the seal. Repeated crew IDs are valid. A seal event is identified by the kit plus its attributed event sequence. An optional seal-identifier field may be added later for prelabeled seals; it is not required in R1.
- **SEAL-3 [C]** Every item listed as inside the kit is covered by the seal; there is no hidden exception. Equipment that needs independent checks is modeled outside the sealed kit.
- **SEAL-4 [C]** **Verification.** An intact, unexpired seal that is not subject to a pending audit, and whose listed items have no open deficiency (OQ-47, decided), may substitute for inspecting the contents (STK-B). The crew member enters the crew ID read from the physical seal (it must match the seal on file) and attests that the seal is intact. The system records a `SEAL_SUBSTITUTION` event naming the seal event relied upon. It creates **no individual Passed check records** (PRN-1 is unchanged), does not close or alter a deficiency, and does not bypass a supervisor suspension. While a listed item has an open deficiency the seal cannot substitute and the kit needs a full inspection; the deficiency itself is handled only through §8.
- **SEAL-5 [C]** **Break.** Any authorized member may break a seal; a reason is required. The event records actor, server instant, the seal marking and the reason. While the kit is open, or broken, the seal cannot substitute and the contents must be inspected.
- **SEAL-6 [C]** **Full inspection and reseal.** The crew member enters a count and an expiration for every listed item; prior certified values are never prefilled. Quantities that differ from the exact target are recorded as deficiencies (STK-D) and do not block resealing. An item whose expiration date is before today (i.e. already expired; an item is still usable on its expiration date, OQ-46) cannot be accepted into a new seal; it must be replaced first. The system computes and displays the earliest expiration to write on the seal. The crew member enters the crew ID written on the new seal and attests that they inspected every item and wrote the ID and date. A seal applied while any listed item still has an open deficiency (of any kind, OQ-47) does not substitute for contents, and sealing never silently closes a deficiency.
- **SEAL-7 [C]** **Full-content audit.** Only a Supervisor (or Administrator) may request one, for now; the owner intends this to become permission-based later (§3.3). While an audit is pending the seal cannot substitute even if intact. It is satisfied only by a break, a full inspection of every item and a reseal, which records an "audit fulfilled" event. Automatic or random selection and cadence remain undecided (OQ-14, STK-C).
- **SEAL-8 [C]** **Expiration.** Once the earliest contained expiration date has passed (the day after it), the seal is no longer valid and a full inspection and reseal are required. An item may be used on its expiration date but not after it (OQ-46, decided). Early "expiring soon" warnings and reorder flags are deferred to a future release (supply and inventory, STK-E and STK-F): replacement stock may already be on hand, and reorder prompts belong with stock levels.
- **SEAL-9 [P]** Seal events are append-only and attributed (AUD-1, AUD-3), carry operation ids for idempotent replay (REC-4), and are serialized per kit so that two concurrent breaks or reseals cannot both succeed; the loser receives a conflict result. Safe default, reversible.

- **SEAL-10 [C]** (OQ-53) Seal handling is per kit instance. Only a valid `SEAL_SUBSTITUTION` for that instance (SEAL-4) or the full inspection of that instance's listed items (SEAL-5, SEAL-6) satisfies the instance's obligation for the kit's listed contents. Breaking a seal satisfies nothing: it only means the contents must be inspected (SEAL-5). A verification, break, inspection or reseal of one instance never stands in for another instance of the same kit type, and a vehicle with no instance has none required.

- **STK-J [P]** Stock quantities are derived from an append-only **stock movement ledger** (receipt, transfer, restock, adjustment, count) rather than a mutable counter; adjustments require an explanation.

## 11. Maintenance, assets and contracts (primarily R3)

- **MNT-1 [C]** Track individual equipment assets with serial numbers, current assignment, transfers, maintenance and repairs, linked to deficiencies where appropriate (REQ §5).
- **MNT-2 [C]** Preventive maintenance rules are calendar- or usage-based, **whichever occurs first**. Usage is **manually entered**; vehicles support mileage and engine hours.
- **MNT-3 [C]** Record vendors/contacts, parts/labor/total cost, invoices, service reports, and work history.
- **MNT-4 [C]** Warranties/service contracts: provider, covered vehicles/assets (multiple), coverage, term/renewals, costs, attached documents, expiration alerts.
- **MNT-5 [P]** Preventive-maintenance due state follows the same "derived, never stored as authoritative" rule as inspections (§19) and never changes operational status (OPS-2).
- **MNT-6 [OQ-18]** Future engine-hour mandatory triggers and counter validation are open.

## 12. Regulatory and historical compliance (R3–R4)

- **REG-1 [C]** Administrators define recurring regulatory inspections/certifications by vehicle/equipment type, distinct from preventive maintenance (REQ §6).
- **REG-2 [C]** Store requirement version, results, effective dates, due dates, documents, reminders and history. Overdue items appear on the dashboard but **never** automatically remove a vehicle from service.
- **REG-3 [C]** Record only **externally authorized** legal extensions/exemptions, naming the issuing authority, evidence, and revised deadline. An administrator cannot create a legal exemption by fiat; **[P]** the entry cannot be saved without authority name and an attached evidence reference.
- **REG-4 [C]** The system produces **auditable "as of" views** reproducing then-applicable rules, inspections and exemptions — not merely current status.
- **REG-5 [OQ-19]** Actual regulatory requirements, authorities and proof rules are not defined; no regulatory content is asserted by this spec.
- **REG-6 [P]** As-of reconstruction is possible because requirement versions, obligation determinations, checks, corrections, invalidations and exemptions are append-only with effective instants (§19.3). The as-of query takes a timestamp `T` and ignores events with `server_recorded_at > T`; the report states that invalidations recorded after `T` are, by default, **shown as of `T` (valid)** with an optional "include later corrections" mode. **Owner confirmation:** OQ-39.

## 13. Notifications [C / OQ-20]

- **NOT-1 [C]** Channels: email and in-app. Administrators control which events/channels are mandatory; optional preferences are delegated to users; recipients are designated by roles and/or users and **de-duplicated** per event.
- **NOT-2 [C]** Examples with stated recipients: deficiency reopened by added verification → current assignee only (§8.7); status changes → notification (§9); expiring stocking deviation, expiring contracts/certifications, overdue deadlines → alerts.
- **NOT-3 [OQ-20]** The mandatory event list, escalation timings, role-recipient mappings, email safety rules and age-highlight thresholds are undecided. **[P]** R1 ships a minimal configurable set: deficiency assigned, reopened, extension requested/decided, deadline overdue, status changed/requested, session reopened by someone else, deficiency admin-closed. All are configurable and none are hard-coded.
- **NOT-4 [P]** Email bodies contain **no** patient data and minimal operational detail (a deep link rather than full content). Delivery is attempted via a durable outbox with idempotent per-recipient delivery records; failures are visible to administrators.
- **NOT-5 [P]** A notification never mutates operational data and cannot itself be the only record of an event.

## 14. Reporting, search and exports

- **RPT-1 [C]** Search/filter completed and incomplete inspections, checks, deficiencies, and corrections (REQ §6).
- **RPT-2 [C]** Export CSV and Excel. **[P]** exports are role-checked, audit-logged (who, filter, row count, time), and size-bounded with explicit truncation messages; CSV output neutralizes spreadsheet-formula injection by prefixing risky leading characters in text cells.
- **RPT-3 [C]** Saved/shareable filter definitions with role-controlled sharing.
- **RPT-4 [C]** Participation reports: counts, dates, inspected items/categories, vehicle, recency, with filters. **No quotas or required minimums** anywhere in the product.
- **RPT-5 [C]** Fleet dashboard: service status, due/overdue items, inspection progress, deficiencies, compliance, readiness. Suspended items and deferred external dependencies remain visible in supervisor contexts (§8.5, §8.9).
- **RPT-6 [C]** Optional automated report generation and email delivery; not required for initial release. **[OQ-16]** templates, who may schedule/receive, formats and retention are undefined.
- **RPT-7 [P]** Reports distinguish: Passed, Not Applicable (rule), Carried over (rule), Suspended, Exempt (out of service), Unable, Failed, Invalidated, Superseded, and Completed late — never collapsing them into a single "compliant" flag.

## 15. Integrations and identity

### 15.1 Google Workspace sign-in [C / OQ-13]

- **AUTH-1 [C]** Sign-in uses **organization-managed Google Workspace identities only** — not personal Google accounts and not merely any account whose address claims the organization's domain (REQ §7).
- **AUTH-2 [C]** Disabling Workspace access must revoke entry while retaining historical attribution.
- **AUTH-3 [P]** Identity key is the provider's stable subject identifier, **not** the email address. Email, display name and domain are attributes captured at sign-in. A sign-in is accepted only if the server has validated the identity token's signature, issuer, audience, expiry, and that the account belongs to the configured Workspace organization **as asserted by a verified claim** (domain hint alone is insufficient; the specific verification mechanism must be confirmed against Google's current documentation before implementation and is therefore OQ-13). Until verified, no production authentication.
- **AUTH-4 [P]** Revocation: sessions are short-lived and re-validated; a disabled user's active sessions are refused within a configurable bound (the mechanism for detecting Workspace disablement must be confirmed from official documentation — OQ-13). Deprovisioned users remain as inactive `users` rows so attribution stays intact.
- **AUTH-5 [OQ-13]** Emergency ("break-glass") access if Google sign-in is unavailable is undecided. **[P]** Safe default: no alternate password login; documented operator procedure only.
- **AUTH-6 [P]** New valid users are auto-provisioned with Crew only (§3.1), after the identity check passes.

### 15.2 Humanity roster (schedule feed) [C / OQ-17]

- **HUM-1 [C]** Humanity/ShiftPlanning is the **schedule source** only (not payroll/timeclock). The schedule is refreshed/cached about **hourly** (REQ §8).
- **HUM-2 [OQ-17]** API documentation, credentials, authentication, shift/user/ambulance mapping, refresh/backoff behavior and stale/unavailable behavior are undefined. This spec defines **no** Humanity API call; **[P]** it defines an internal `RosterProvider` interface returning `(person, vehicle_assignment, shift_start, shift_end, fetched_at)` so the adapter can be implemented later and stubbed in tests.
- **HUM-3 [P]** Staleness: if `now − fetched_at` exceeds a configured limit (default proposal: 3 h, 3× the refresh), roster data is treated as **unavailable** for conditional rules (CND-3). Refresh uses bounded retries with backoff; it never loops unbounded. The limit governs new determinations; re-evaluating an existing waiver follows CND-7, under which aged but unchanged data does not reinstate it.
- **HUM-4 [C]** Credentials are never committed; they come from deployment secrets (§17.6).
- **HUM-5 [C]** The roster does **not** restrict who may perform equipment checks. Volunteers have no timeclock; paid staff's payroll timeclock is out of scope.

### 15.3 Actual-roster correction [C] (REQ §8)

At inspection time a crew member may confirm/correct the **actual roster for that inspection only**. The system retains both the scheduled and corrected rosters and who changed it. There is **no write-back** to Humanity and no effect on other sessions' own records. **[C: owner-approved 2026-10-15, OQ-44]** Because several sessions may coexist for one vehicle yet an obligation's waiver is shared: the roster governing a conditional determination is the scheduled roster overlaid with the **most recent** (by server time) actual-roster correction among sessions on that vehicle covering the determination instant. Every correction is retained and triggers a re-evaluation event; if two corrections from different sessions disagree on membership, supervisors are notified and the determination records both and fails closed (the obligation is required) until a supervisor resolves it. Each determination records the roster provenance (scheduled vs which correction).

## 16. Audit, security and privacy

### 16.1 Audit log [C] (REQ §2.3, §9)

- **AUD-1 [C]** Maintain immutable, attributable history of inspections, corrections, verifications, cancellations, invalidations, reopenings, role changes, status changes, exports and denied actions.
- **AUD-2 [C]** Check/inspection history is stored **independently from current materialized status** (REQ §2.3).
- **AUD-3 [P]** Every audit entry records: actor, actor's roles at the time, action, target entity+id, server instant, request/correlation id, and prior/new values or a reference to the immutable rows. The application's database account has no UPDATE/DELETE privilege on append-only tables (enforcement mechanism depends on the stack, OQ-24).
- **AUD-4 [P]** Mutable administrative configuration (schedules, thresholds, role grants) is versioned or event-sourced so changes appear in history.
- **AUD-5 [OQ-35]** Audit and record retention periods are undefined; **[P]** safe default: retain indefinitely; no automated pruning.

### 16.2 Application security [P]

Standard web controls apply: TLS everywhere; secure, HTTP-only, same-site session cookies; CSRF protection on state changes; output encoding; parameterized queries; input validation on all fields with length bounds; rate limiting on authentication and exports; secrets from environment/secret store (never in repository or logs); dependency pinning and review. Specifics depend on the stack (OQ-24).

### 16.3 Time and clock [C]

Trustworthy **server** timestamps (REQ §9); client timestamps are informational only.

### 16.4 Online-only [C]

No offline queue or sync (REQ §2.4, §9). A lost connection must be conspicuous and must never present unconfirmed entries as saved.

### 16.5 Attachments and sensitive data [OQ-23]

Allowed types/sizes, retention and role visibility are undefined. **[P]** Safe default: images and PDF only, per-file size cap, virus/type validation server-side, stored outside the web root with access checks, visible only to roles that can see the parent record, and UI text warning users **not to attach patient information or identifiable photos**. Patient data is out of scope (§1.3).

### 16.6 Privacy of crew member data [P]

Participation and attribution data is visible only per the permission matrix; personal data collected is limited to what Workspace identity provides plus role and activity records. Inspection records attribute actions to authenticated user accounts without storing clinical qualifications or employment classifications; proof of credentials and driver status is established through external roster and organization training/personnel systems. Exports containing personal data are role-restricted and logged.

## 17. Architecture and stack

> **Stack decided (owner, 2026-10-20): Python with the Django framework and MariaDB 10.11 on Linux** — see §17.5. The rest of this section states the capabilities the stack must provide and one logical decomposition; §17.7 lists the design rules the spike showed this stack needs. Hosting, secrets and recovery are decided in §17.8 ([issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16)).

### 17.1 Logical components [P]

```
 Browser (responsive web UI, touch-first)
        │  HTTPS (request/response)      ▲  server push (live status)
        ▼                                │
 ┌─────────────── Application server ─────────────────┐
 │ AuthN (Google Workspace) • AuthZ (roles) • API      │
 │ Command handlers (checks, corrections, deficiencies)│
 │ Query/projection layer • Realtime fan-out           │
 └───────┬───────────────────────────────┬─────────────┘
         │ transactions                  │ jobs
         ▼                               ▼
 Relational database (system of    Background worker: obligation generation,
 record: append-only events +      session auto-close, roster refresh,
 projections)                      notification outbox, expiry alerts
```

### 17.2 Required stack capabilities (selection criteria) [P]

| ID | Capability | Why |
|---|---|---|
| ARC-1 | Transactional relational store with unique constraints and row/table-level serialization | Idempotency keys (REC-4), atomic conflict check + insert (CON-5), closure transactions (VER-4) |
| ARC-2 | Ability to restrict application DB role to INSERT-only on append-only tables (or equivalent enforced immutability) | AUD-3 |
| ARC-3 | Server push to browsers (e.g. WebSocket or Server-Sent Events — choice open) | REC-3 live status |
| ARC-4 | Scheduled/background job execution that is idempotent and restart-safe | TIME-1, SES-6 |
| ARC-5 | Time zone–aware date/time library supporting IANA zones and DST rules | TIME-2/3 |
| ARC-6 | Verified OpenID Connect/Google token validation support | AUTH-3 |
| ARC-7 | Backup/restore with tested recovery, including point-in-time capability for as-of audits | REG-4, OQ-24 |
| ARC-8 | Offline-capable automated tests with injectable stubs for Google and Humanity | [AGENTS.md](AGENTS.md) rule 6 |

### 17.3 Live synchronization [P]

State-changing commits publish an event after the transaction commits; subscribers get per-vehicle updates. Clients treat pushes as hints and reconcile from the authoritative query on (re)connect, so missed messages cannot leave a permanently wrong view. Target (proposal): other viewers see a committed change within a few seconds under normal conditions [OQ-15].

### 17.4 Derived state [P]

Current statuses (obligation state, item attention, deficiency status, vehicle readiness) are **projections** computed from authoritative event rows, rules and effective dates. Projections may be materialized for speed but must be rebuildable from events; a verification routine compares projections to a recomputation (§19).

### 17.5 Hosting, database engine, backup [OQ-24]

**Decision (owner, 2026-10-20) [C]:** the application uses **Python with Django** and **MariaDB 10.11 LTS** on a Linux server. Basis: the OQ-24 architecture spike ([`docs/spikes/oq24-architecture-spike.md`](docs/spikes/oq24-architecture-spike.md)), where both Perl/Mojolicious and Python/Django passed every database criterion on MariaDB. The owner weighed their Perl experience against agents writing the code, Django's built-in security defaults and larger maintainer pool, and accepted Django. The earlier owner-stated constraints (Linux server, publicly facing so security weighs heavily) still apply. This closes the language, framework and database part of OQ-24. Hosting, secrets and recovery are decided in §17.8 (owner, 2026-10-21; [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16)). The spike left items unverified, and each has its own verification path: Google token claims and revocation detection under [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25); restore to a timestamp and restore time at realistic data size under the DEP-12 pilot gates; public-exposure hardening under [issue #6](https://github.com/JustApu/CheckYourAmbulance/issues/6) and the DEP-10 defaults; physical/hot backup tools, load under production-like concurrency, and replication or high availability under no issue yet (they stay open and need an owner or issue before they are relied on).

*Superseded by the decision above; kept as the record of the options considered in [Appendix A](#appendix-a-stack-comparison-note-non-binding).*

### 17.6 Configuration and secrets [P]

Configuration files and secrets live outside version control (the repository's `data/` convention in [AGENTS.md](AGENTS.md)); only documented sample configuration with synthetic values is committed.

### 17.7 Design rules adopted from the spike for Django + MariaDB [P]

Adopted as starting rules for the M1 contracts and audit issues ([issue #20](https://github.com/JustApu/CheckYourAmbulance-archive/issues/20), [issue #23](https://github.com/JustApu/CheckYourAmbulance-archive/issues/23)); the contracts issue may refine them with evidence, through review. Evidence: spike report §3 and §5.

1. **Two database accounts:** a schema-owner account used only for migrations, and a least-privilege runtime account with `SELECT, INSERT` on append-only tables and no DDL. Django models over append-only tables are **unmanaged** or guarded so `save()`/`update()`/`delete()` cannot be used on them; the Django admin must not expose them for editing.
2. **Idempotent writes** use the operation id's unique key: attempt the INSERT, catch the duplicate-key error, return the original record. **Never `INSERT IGNORE`** (it downgrades other errors and defeats strict mode).
3. **Per-(vehicle, item) lock rows** are created when a checklist version is published and locked with `SELECT … FOR UPDATE` inside `transaction.atomic()`; the runtime account holds `UPDATE` on that lock table only. Lock rows are not created lazily inside the check transaction (observed deadlocks). Wrap the check write in a bounded retry on deadlock only.
4. **Every connection** sets UTC (`time_zone`) and strict SQL mode explicitly (Django's default session time zone was `SYSTEM` and it does not add strictness), and a startup check refuses to run otherwise. CHECK constraints and triggers back up the application checks.
5. **Sessions** use signed cookies or a store the runtime account can write; cookies are `Secure`, `HttpOnly`, `SameSite=Strict`; CSRF protection stays on (Origin/Referer checking included).
6. **Tokens:** validate with a pinned algorithm and required `exp`, `iat`, `iss`, `aud`, `sub`; Google's organization-claim semantics must be confirmed from Google's documentation before sign-in is built ([issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25) adds a real-Workspace verification).
7. **Live updates** use WebSockets (Django Channels is the spike-tested option). A shared channel layer is needed once more than one worker runs; choosing it is part of the deployment decisions, not decided here. **Every WebSocket connection must be authenticated and authorized per vehicle before it joins a group, and must be rejected otherwise**; the spike's WebSocket consumer is deliberately unauthenticated and must not be reused as written (the Perl probe checked the session, the Django probe did not).
8. **Admin separation:** the database administrator account is not used by the application, its use is rare and logged outside the database; shipping audit events to a second store is to be considered in the audit-log issue.

### 17.8 Deployment, secrets and recovery [OQ-24; [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16)]

Owner decisions recorded on [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16) on 2026-10-21 ([decision record](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16#issuecomment-6322809972)). The owner then approved the items first proposed as **[P]** on that issue, so every item below is confirmed. Operational details (retention counts, check cadence, alert thresholds) are starting values the operator may tune within the stated recovery objectives.

- **DEP-1 [C]** Production runs on one Linux VPS operated for the organization.
- **DEP-2 [C]** One application process is assumed initially. A multi-process deployment needs a shared channel layer (§17.7 item 7), which is then a new decision.
- **DEP-3 [C]** The organization names an accountable operator and a deputy before the pilot. The names are held by the owner and are not committed to the repository (§0.3). Their roles may be filled by volunteers, paid staff, or a mix, according to the agency's staffing model.
- **DEP-4 [C]** Planning budget: about USD 25 per month for the server, off-host backup storage and monitoring together. This is a planning estimate, not a hard ceiling, and donated resources may offset part of the cost. If real prices differ materially, the owner decides. The planning estimate assumes no paid contractor and no warm spare; personnel may be volunteers, paid staff, or a mix, according to the agency's staffing model.
- **DEP-5 [C]** Binary logging stays enabled (point-in-time recovery, ARC-7). The production server configuration sets `log_bin_trust_function_creators=1` so that the schema-owner account, which has no SUPER privilege, can run the trigger migrations. On the pinned MariaDB 10.11 image, without that setting `CREATE TRIGGER` fails with error 1419 while binary logging is on, and of the narrower privileges tried (`BINLOG ADMIN`, `SET USER`, `BINLOG REPLAY`) none lifted it; only SUPER did. The runtime account never holds SUPER or trigger-creation powers (§17.7 item 1). The first restore rehearsal confirms the setting against the MariaDB documentation and that a rebuilt server carries it. A trigger records its creating account as DEFINER: rotate that account's password, never drop or rename it, and verify definers after any restore or account change (a missing definer makes every insert into a table with an INSERT trigger fail, error 1449).
- **DEP-6 [C]** Recovery objectives: RPO 15 minutes and RTO 24 hours. Mechanism: a nightly encrypted full backup plus binary-log shipping off the host.
- **DEP-7 [C]** The encrypted off-host copy lives in an account separate from the server, held by an authorized recovery custodian. The operator holds a write-only key. Restores are performed by authorized agency personnel, and a production restore needs the owner's authorization. Provider snapshots alone do not satisfy this.
- **DEP-8 [C]** Backup retention: 30 daily and 12 monthly full backups, plus binary logs back to the oldest retained full backup. This is distinct from record retention (AUD-5, OQ-35): application data is never pruned because backups age out, and backups carry the same access controls as production.
- **DEP-9 [C]** A restore rehearsal each quarter into a throwaway environment, including the projection rebuild comparison (AC-26). A rehearsal copy contains real data and is handled and destroyed under production rules, never on a developer machine or in CI.
- **DEP-10 [C]** Exposure and operations defaults: a reverse proxy terminates TLS with automatic certificate renewal and forwards to the application on loopback or a Unix socket; the database listens on loopback only; the host firewall admits only HTTPS and restricted SSH; sign-in and exports are rate-limited at the proxy as well as in the application (§16.2); secrets live in a root-owned mode-0600 file outside the repository and web root, rotated yearly, on operator change and on suspected exposure; alerts cover site unreachable, certificate expiry within 14 days, disk space, a backup or log-shipping run older than expected, the database refusing connections, and repeated 5xx errors.
- **DEP-11 [C]** Outage plan (NFR-5): printed checklists for each vehicle and kit type are kept in each rig, outside the app. Until a back-entry feature exists (OQ-50), paper records stay outside the app and are not entered as ordinary live checks.
- **DEP-12 [C]** Pilot gates: the operator and deputy are named; one restore rehearsal has passed (DEP-9, including a restore to a chosen timestamp and the restore time at a realistic data size, two items the spike left unverified); and the trigger setting (DEP-5) is confirmed on the production host.

## 18. Logical data model (proposed)

Entities are grouped by mutability. **A** = append-only; **V** = versioned/effective-dated; **M** = ordinary mutable (with change history in the audit log).

| Group | Entity | Kind | Key fields / notes |
|---|---|---|---|
| Identity | `organization` | M | single row in v1 |
| | `user` | M | `idp_subject` (key), email attr, display name, `active`, attribution never deleted |
| | `role_grant` | A | user, role, granted_by, granted_at, revoked_at/by |
| Fleet | `vehicle` | M | name, number, active |
| | `compartment`, `vehicle_system` | V | vehicle slot definitions |
| | `kit_instance`, `kit_seal_event` | M/A | R1 minimum (§10.1); `kit_instance`'s vehicle, compartment slot and active flag are a rebuildable projection of the latest `kit_assignment_event`, never the source for an as-of question |
| | `kit_assignment_event` | A | R1 minimum (OQ-54): kit instance, vehicle (nullable), compartment slot (nullable), active flag, effective instant, recorded instant, actor; the source of truth for TIME-1a |
| | `kit_movement` | A | R2; extends `kit_assignment_event` with transfer workflows and history across stations |
| | `equipment_asset`, `asset_assignment` | M/A | R3 |
| Checklists | `checklist_template`, `checklist_version` | V | immutable once published; `effective_at` |
| | `checklist_item_def` | V | belongs to version; kind, qty target, bulk_eligible (reserved, unused in R1), NA rule ref |
| | `vehicle_override_version` | V | overlay |
| | `schedule_rule` | V | tz-aware recurrence, `w_early`, `w_grace`, effective range |
| | `conditional_rule` | V | closed rule types (§5.6) |
| | `stocking_deviation` | V | validity range, approver |
| Obligations | `obligation` | A (state derived) | `(vehicle,item,kit instance,D,requirement_version)` unique; kit instance null only outside a kit |
| | `obligation_determination` | A | waive/reinstate with inputs (CND-1) |
| Inspection | `inspection_session` | M + event stream | pinned `checklist_version_id`; state events |
| | `session_event` | A | open, reopen, close(auto/manual), roster confirm |
| | `check_record` | A | §7.2 |
| | `check_event` | A | CORRECTION (links to original) / INVALIDATION (+explanation) |
| | `check_obligation_link` | A | unique `(check_id, obligation_id)`; one link by default, extra links only via audited `shared_rule_id` (SCH-5) |
| | `odometer_entry`, `engine_hours_entry` | A | + correction/invalidation events |
| | `roster_snapshot`, `session_roster_correction` | A | scheduled vs actual, who changed |
| Deficiency | `deficiency` | M header | immutable creation facts; `category` carries an administrator-configured `supply_related` flag used for Supply Officer assignment rights (§3.3); `severity` is a versioned administrator-configured level (DEF-8) |
| | `deficiency_event` | A | status, assignment, deadline, extension, dependency, ack, closure, reopen |
| | `deficiency_evidence` | A | links to supporting checks |
| | `deficiency_link` | A | add/remove relationships |
| | `verification_scope_row` | A | deficiency, correction event, item + definition, nullable physical kit instance with a non-null instance key (all-zero UUID for no-kit), required-after instant, scope revision; unique deficiency/revision/item/instance; VER-4 |
| | `check_suspension` | A | explicit checks, start/end, ending event |
| Operations | `vehicle_status_event`, `status_request` | A | OPS-6 |
| Inventory | `catalog_item`, `storage_position`, `stock_movement`, `purchase_order`, `po_line`, `receipt` | M/A | R2 |
| Maintenance | `maintenance_rule`, `maintenance_job`, `vendor`, `service_contract` | V/M | R3 |
| Regulatory | `reg_requirement`, `reg_result`, `reg_exemption` | V/A | R3–R4 |
| Cross-cutting | `audit_event`, `attachment`, `notification_pref`, `notification_outbox`, `saved_filter`, `report_schedule`, `user_preference` | A/M | |

### 18.1 Required constraints [P]

1. `UNIQUE(organization_id, operation_id)` on `check_record`, `odometer_entry`, `deficiency` creation requests, and every other client-initiated create.
2. `check_obligation_link` unique per `(check_id, obligation_id)`; a second link for one `check_id` requires a non-null `shared_rule_id` (SCH-5).
3. `obligation` unique on `(vehicle_id, item_id, kit_instance_id, due_at, requirement_version_id)`, with a missing kit instance compared as a value (MariaDB `UNIQUE` treats NULLs as distinct, so the physical key must not rely on a nullable column alone).
4. A correction's author must equal the original's author (enforced in the command handler and by constraint where the engine allows).
5. An invalidation row requires a non-empty explanation and an administrator actor.
6. `deficiency_event` status transitions validated against §8.2.
7. Referential integrity everywhere; users are never hard-deleted.

## 19. Derived-state rules (reproducibility)

**19.1 Effective check outcome.** For a correction chain: if an INVALIDATION event exists on any of its rows → the whole chain is *Invalidated* (excluded; no earlier outcome revives, INV-2a). Otherwise the latest correction by the same author is the effective outcome; earlier rows are *Superseded*.

**19.2 Obligation state.** Computed from the obligation, any determination (waived/reinstated), effective checks linked to it, active suspension, vehicle Out-of-Service exemption interval, and server time (§5.3–5.4).

**19.3 As-of.** The same functions, parameterized by `T`, over events with `server_recorded_at ≤ T` and rules with `effective_at ≤ T` (REG-6).

**19.4 Item attention state.** Red if any unresolved deficiency applies to the item (via scope) or the latest effective check is Failed/Unable; otherwise clear (§7.5). For an item inside a kit the state is kept per kit instance.

**19.5 Deficiency status.** Last valid lifecycle event; a recompute check is part of CI once a stack exists.

## 20. Nonfunctional requirements

- **NFR-1 [C]** Responsive, touch-friendly UI optimized for phones and larger screens (REQ §9). **[P]** Accessibility target: WCAG 2.2 AA, including color-independent status cues (important for status chips), target sizes suitable for gloved use [OQ-40 for any specific device/glove requirements].
- **NFR-2 [C]** Online-only; authoritative server-side validation of schedules, corrections, permissions, deadlines and status transitions.
- **NFR-3 [C]** Near-real-time multi-user sync; no silent overwrites; clear pending/error indicators.
- **NFR-4 [P]** Capacity assumption for sizing: a handful of vehicles and tens of users; hundreds of items per vehicle; peak concurrency of a few simultaneous inspectors per vehicle. If the real numbers differ materially, amend.
- **NFR-5 [C]** Recovery objectives: RPO 15 minutes and RTO 24 hours (owner, 2026-10-21; §17.8 DEP-6). Because the app is online-only, outages block inspections; crews use the paper fallback in §17.8 DEP-11.
- **NFR-6 [P]** Every list/export/loop is bounded (pagination, row caps, retry limits), per [engineering standards](docs/engineering-standards.md#core-principles).

## 21. External dependencies and artifacts

| Dependency | State | Spec impact |
|---|---|---|
| Real checklists (OQ-01) | Owner to supply | Blocks seed data and real-world acceptance, not the model |
| Humanity API details/credentials (OQ-17) | Owner to supply | Conditional rule realism; adapter unimplemented until supplied |
| Regulatory definitions (OQ-19) | Pending | R3–R4 content |
| Hosting/stack/Workspace configuration (OQ-13, OQ-24) | Decided (stack 2026-10-20, §17.5; hosting, secrets and recovery 2026-10-21, §17.8); production setup and the pilot gates in DEP-12 pending | Does not block coding; the pilot gates block the pilot |
| Station storage/catalog examples (OQ-22) | Pending sanitization | R2 |

## 22. Glossary

- **Obligation** — a dated instance of a requirement that a check can satisfy.
- **Check** — one attributed result for one item.
- **Session** — an organizing container for checks; never itself evidence of compliance.
- **Supersede** — a correction replaces the effective outcome of an earlier check by the same author; original kept.
- **Invalidate** — administrator exclusion of a check from compliance; original kept.
- **Suspension** — supervisor-defined pause of specific routine checks for a known defect; not a Pass.
- **Exempt** — obligation not generated because the vehicle is Out of Service.
- **Waived/Carried over** — obligation not required under a conditional rule.

## 23. Acceptance criteria

Each criterion is testable offline with synthetic fixtures. Tags show whether the underlying rule is [C] or [P].

| ID | Criterion |
|---|---|
| AC-1 | **Item-level compliance [C].** Given vehicle V with items A and B due at D, when Crew1 passes A and Crew2 passes B in window, then A and B are each Satisfied with their own attribution; closing or never closing a session changes neither. |
| AC-2 | **No session shortcut [C].** Given a session marked Closed with item C unchecked, C's obligation remains unsatisfied. |
| AC-3 | **Window evaluation [P/OQ-02].** For a 06:00 daily requirement with ±1 h and no other unsatisfied obligation open: a pass at 05:15 is on time and satisfies it; a pass at 07:01 is a late completion of it (linked as late; obligation recorded Overdue → Completed late, with the lateness retained in history); a pass at 04:59 does not satisfy it. |
| AC-4 | **DST [P/OQ-25].** On a spring-forward day with a due time inside the gap, the due instant resolves to the first valid instant after the gap; on fall-back, the first occurrence is used. |
| AC-5 | **Conditional carry-over [C/P].** Given a synthetic roster in which all 06:00 crew were on the 00:00–06:00 shift and a valid 00:00 check exists, the 06:00 glucometer obligation is waived with a recorded determination; when one new crew member begins at 06:00, it is required; when roster data is unavailable, it is required. |
| AC-6 | **Inactivity close [C].** A session with no persisted business activity for 6 h becomes Incomplete with all checks intact; viewing/heartbeat does not reset the timer; the crew member reopens without approval, and the reopen is audit-logged with the original checklist version unchanged. |
| AC-7 | **Auto-save and acknowledgement [C].** An outcome is shown Pending until acknowledged; if the server rejects it the UI shows Failed with retry and the item is not displayed as saved. |
| AC-8 | **Idempotent replay [P].** Submitting the same idempotency key twice yields one record and the same response. |
| AC-9 | **Concurrent duplicate warning [C/P].** Crew1 and Crew2 both open item X; Crew1 commits; Crew2's first submit returns a conflict warning naming Crew1's check; Crew2 proceeds; two independent effective records exist, no lock was taken. |
| AC-10 | **Fail never lost [P].** If a Pass and Fail race on one item, both commit (after acknowledgement of conflict) and the item's attention state stays red until the deficiency is resolved. |
| AC-11 | **Bulk pass [C/P] — deferred from R1 (§7.6); not required in R1.** Bulk-passing five eligible and two ineligible (one measurement, one with an open deficiency) items commits five separate attributed records with a shared group id and rejects the two with reasons; an item with a newer concurrent check is excluded and returned. |
| AC-12 | **Author-only correction [C].** A supervisor attempting to correct another crew member's check is refused; the supervisor can record a fresh independent check; the author can correct and both versions remain visible. |
| AC-13 | **Reported in Error [C/OQ-42].** Author's Correct-entry of Failed→Passed marks the linked deficiency Reported in Error with history (confirmed baseline). Under the approved COR-4 conditions: with another effective Failed or Unable to Inspect check, or a recorded repair, the deficiency stays open instead. |
| AC-14 | **Invalidation keeps deficiency open [C].** An administrator invalidating a Failed check (with explanation) excludes it from compliance and recalculates status, and the associated deficiency remains open; it closes only after an explicit closure allowed by §8.8, with an explanation. |
| AC-15 | **Invalidation authorization [C].** A non-administrator cannot invalidate; an invalidation without an explanation is rejected; the original record is unmodified. |
| AC-16 | **Verification scope [C].** For kit or compartment defects, closure requires passes of every item in the correction-time scope, matched to its physical kit instance where applicable; for a vehicle-level defect, only the report-time affected items and supervisor expansions; ordinary routine passes without `purpose=VERIFICATION` do not close it. |
| AC-17 | **Reopen on added verification [C].** A supervisor adding verification to a closed deficiency reopens it, notifies only the current assignee, requires a new deadline, and preserves the old one in history. |
| AC-18 | **Suspension is not a Pass [C].** A supervisor-suspended check shows "Known Deficiency — Check Suspended", does not count as Satisfied, is excluded from Needs Attention Only but visible in full checklists and dashboards, and ends automatically when the deficiency closes by verification. |
| AC-19 | **No automatic status change [C].** Recording a critical-severity Failed check, an overdue regulatory item, or an expired certification does not change the vehicle's operational status; only an audited supervisor action does. |
| AC-20 | **Out-of-service exemption [C].** While Out of Service, no routine obligations are generated and the interval displays "Exempt", not Passed. |
| AC-21 | **Odometer [C/P].** A routine inspection obligation set includes one odometer obligation satisfiable by any crew member; a lower-than-previous reading requires confirmation; engine hours are optional. |
| AC-22 | **Auth [C/P].** A token with a valid signature but a personal-account identity or non-organization account is refused; a disabled user cannot sign in but historical attribution remains intact. |
| AC-23 | **Roster correction [C/P].** An actual-roster correction is stored alongside the scheduled roster with its editor and nothing is written back to the Humanity stub. It is entered per session but, for a shared conditional obligation, the latest correction by server time among sessions on that vehicle governs (OQ-44); with two concurrent sessions entering conflicting rosters, the determination records both, fails closed (obligation required), notifies supervisors, and is re-evaluated when one is resolved. |
| AC-24 | **Deny by default [P].** An action absent from the approved permission matrix is refused server-side even if the client submits it directly. |
| AC-25 | **Immutability [P].** No application code path updates or deletes an append-only row; a test enumerates tables flagged append-only. |
| AC-26 | **Reproducibility [P].** Rebuilding projections from events yields identical statuses to the live ones for a randomized synthetic history. |
| AC-27 | **As-of [C/P].** An as-of query for time T returns the obligations, rules, checks and exemptions that existed at T, regardless of later changes. |
| AC-28 | **Offline testability [AGENTS.md].** All of the above run with no network, using stubs for Google and Humanity. |
| AC-29 | **Unable to Inspect [C].** Recording it without a reason is rejected; the obligation stays open; a supervisor marking it complete (with explanation) shows "Completed by supervisor — unable to inspect", not Passed; a crew member cannot do so. |
| AC-30 | **Link cardinality [P].** With two overlapping-window obligations for one item, one check links to the earliest-due only; a second link without a shared-rule id is rejected; invalidating the check voids all its links. |
| AC-31 | **Idempotency and conflict [P].** After a lost response the retry with the same operation id returns the original result; a conflict warning leaves the id reusable; acknowledging with `acknowledges_check_id` commits only if no newer check landed in between, otherwise a fresh warning is returned. |
| AC-32 | **Chain invalidation [P].** Invalidating a corrected check excludes the whole chain with no earlier outcome revived; further correction is refused; a linked deficiency stays open. **1.3.0 addition (INV-7):** if COR-3/COR-4 marked its deficiency Reported in Error, invalidating that chain reopens the deficiency to Open atomically, retains the invalid evidence, and leaves vehicle operational status unchanged. |
| AC-33 | **Seal verification [C/P].** With an intact, unexpired seal and no pending audit, entering the matching crew ID and the intact attestation records one `SEAL_SUBSTITUTION` event and no individual Passed check; a mismatched ID is rejected; with an open deficiency on a listed item, verification is refused and the deficiency is neither closed nor hidden. |
| AC-34 | **Break and reseal [C/P].** Breaking requires a reason. Resealing is refused unless every item has a freshly entered count and an expiration date that is today or later, the new seal's crew ID is present and the attestation is made; inputs are not prefilled from the previous seal. A short count reseals but is recorded as a deficiency and the seal does not substitute; the written date equals the earliest expiration across all items; a repeated crew ID is accepted. |
| AC-35 | **Audit and expiry gating [C/P].** After a supervisor requests an audit, a crew member's verification is refused even with a matching ID until break, full inspection and reseal complete, which records "audit fulfilled"; a crew member cannot request an audit. Beginning on the day after the earliest contained expiration date, verification is refused and a full inspection is required; on the expiration date itself verification is still accepted (subject to the other gates). |

## 24. Decision register

### 24.1 Classes

- **Blocking before coding (BBC)** — the data model, security or irreversible behavior of the affected area cannot be implemented safely without it.
- **Blocking before production (BBP)** — implementation may proceed against the safe default, but the feature must not go live until the owner decides or confirms.
- **Deferrable (DEF)** — a documented default is acceptable for the release shown; decision can wait.

"Owner approval" = the owner must explicitly approve the decision or default in writing on the PR/issue (§25).

### 24.2 Register for OQ-01–OQ-27

The source file labels OQ-01–OQ-13 "blocking before the first production inspection release". This register further distinguishes which of those must also be settled **before coding** the affected area (BBC) because they shape the data model or security; the distinction is a spec-author proposal.

| ID | Topic | Class | Spec § | Proposed safe default | Owner approval needed? |
|---|---|---|---|---|---|
| OQ-01 | Real checklists | **BBP** (model is codeable now; seeds synthetic) | 4.3 | Synthetic seed data only, labeled non-authoritative | Yes — real content supplied by owner |
| OQ-02 | ±1h semantics (due/early/grace/overdue) | **BBC** | 5.3 | Separate `D`, `E`, `G`; states Upcoming/Current/Due-Grace/Overdue; late completion recorded as late. **Owner called the working model "acceptable" 2026-10-15**; edge cases (day boundaries, DST) stay with OQ-25 | **Decided** (owner, 2026-10-16): safe default approved; edge cases settled with OQ-25 |
| OQ-03 | Window overlap / multiple obligations | **BBC** | 5.5 | One linked obligation per check by default (earliest-due); multi-satisfy only by explicit audited rule | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-04 | Meaning of "no activity" | **BBC** | 6.3 | Persisted business events only; reopen restarts 6 h | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-05 | Version governing current status | **BBC** | 5.7 | Obligation bound to version effective at `D`; session shows original version | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-06 | Role permission matrix | **BBC** (authz design) | 3.3 | Deny-by-default; matrix in §3.3 | **Decided** (owner, 2026-10-15): all matrix cells resolved; deny-by-default remains for any action not listed |
| OQ-07 | Other Reported-in-Error cases | **BBP** | 7.7, 8.2 | Only author Correct-entry path (COR-4) | Yes before widening |
| OQ-08 | Multiple failed records per deficiency | **BBC** | 8.3 | Evidence list; never auto-close/cancel while support remains; admin invalidation never closes | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-09 | Correct entry vs repair | **BBC** | 7.7 | Two separate flows; correction never implies repair | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-10 | Concurrent-edit semantics | **BBC** | 7.4, 7.6 | Basis-id conflict detection; idempotency keys; per-item bulk conflicts excluded | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-11 | Bulk confirmation/eligibility | **DEF** | 7.6 | Bulk Pass deferred from R1; BLK-1–BLK-5 retained for a later release | **Decided** (owner, 2026-10-20): deferred from R1 |
| OQ-12 | Odometer vs item checks | **BBC** | 6.4 | Vehicle-level odometer obligation per scheduled inspection | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-13 | Workspace identity model; break-glass | **BBC** (security) | 15.1 | `sub`-keyed identity; verified org claim; no production auth until verified; no password fallback | **Decided** (owner, 2026-10-15): verified org claim, no fallback; the verification mechanism must still be confirmed against Google's current documentation before implementation |
| OQ-14 | Sealed-kit audits | **DEF** (partly decided) | 10, 10.1 | R1: manual supervisor-requested audit only (SEAL-7), **decided** (owner, 2026-10-20). Scheduled or random cadence not implemented in R1 | Yes before the cadence is built (R2) |
| OQ-15 | Real-time UI details | **DEF** | 7.4, 17.3 | Conflict warning per CON-3; connection-loss banner; no presence indicator | No (UX detail) |
| OQ-16 | Report templates / scheduled email | **DEF** (R4, optional) | 14 | No scheduled reports in R1 | Yes before R4 |
| OQ-17 | Humanity API/mapping/stale behavior | **BBP** for conditional rules; otherwise DEF | 15.2 | `RosterProvider` interface; stale → conditional rule not applied | Yes — owner supplies details |
| OQ-18 | Engine-hour triggers | **DEF** | 6.4, 11 | Optional entry | No |
| OQ-19 | Regulatory content | **DEF** (R3–R4) | 12 | No regulatory content asserted | Yes before R4 |
| OQ-20 | Notification events, timing, recipients | **BBP** | 13 | Minimal configurable set (NOT-3) | Yes |
| OQ-21 | Limited-service categories, request approver | **BBP** | 9 | Free-text restrictions; supervisor decides | Yes |
| OQ-22 | Inventory starting data, POs | **DEF** (R2) | 10 | Ledger-based model only | Yes before R2 |
| OQ-23 | Attachment rules | **BBP** | 16.5 | Images/PDF, size cap, role-scoped, no-patient-data warning | Yes |
| OQ-24 | Hosting/stack/DB/backup | **BBC** | 17 | Decide against ARC-1–ARC-8 criteria | **Decided** (owner, 2026-10-20 and 2026-10-21): Python/Django + MariaDB 10.11 on Linux (§17.5, §17.7); hosting, secrets and recovery objectives (§17.8, [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16)). The items first proposed as [P] in §17.8 were approved by the owner on 2026-10-21. Unverified spike items are not all closed by the DEP-12 pilot gates, which cover only restore-to-timestamp, restore time at realistic size and the trigger setting; the rest follow the verification paths in §17.5 |
| OQ-25 | Timezone/DST/as-of timing | **BBC** (schedule engine) | 5.8 | Single IANA zone; UTC storage; DST rules TIME-3 | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-26 | Governance for promoting to SPEC | **BBC** (process) | 25 | §25 process | **Decided** (owner, 2026-10-16): §25 process approved |
| OQ-27 | MVP / release boundaries | **BBC** (scope) | 2 | R1–R4 per §2.1 | **Decided** (owner, 2026-10-15): R1–R4 as written in §2.1 |

### 24.3 Questions raised while drafting (new)

Raised while drafting; mirrored in `OPEN_QUESTIONS.md` so the two lists agree.

| ID | Question | Class | Spec § | Proposed safe default | Owner approval? |
|---|---|---|---|---|---|
| OQ-28 | Does a **Failed** check satisfy the inspection obligation? | **Decided** (owner, 2026-10-15) | 5.4 | Yes — the check was done; deficiency/attention state handled separately | Done |
| OQ-29 | Does **Unable to Inspect** satisfy the obligation? | **Decided** (owner, 2026-10-15) | 5.4.1 | Reason required; supervisor may mark complete (distinct from Pass) | Done |
| OQ-30 | If a verification check that auto-closed a deficiency is later invalidated, what happens? | **Decided** (owner accepted proposal, 2026-10-15) | 7.8 | Deficiency reopens; assignee and supervisors notified | Done |
| OQ-31 | Is an inspection required when a vehicle returns to service? | **BBP** | 5.9 | No automatic requirement; supervisor may require ad hoc | Yes |
| OQ-32 | Time or state limits on an author correcting their own check? | **BBP** | 7.7 | None | Yes |
| OQ-33 | Can an administrator invalidate their own check? | **BBP** | 7.8 | No — use correction | Yes |
| OQ-34 | Is an explanation mandatory for explicit administrator closure of a deficiency? | **BBP** | 8.8 | Yes | Yes |
| OQ-35 | Retention periods for audit and record data | **BBP** | 16.1 | Retain indefinitely | Yes |
| OQ-36 | First-administrator bootstrap; minimum administrator count | **BBC** | 3.6 | Operator-seeded; cannot remove last admin | **Decided** (owner, 2026-10-16): safe default approved |
| OQ-37 | If a conditional waiver is withdrawn after the grace deadline, how is the obligation displayed? | **DEF** | 5.6 | Required and shown overdue from `G` | No |
| OQ-38 | Per-crew-member vs per-device persistence of view preference and filter (source documents differ) | **DEF** | 6.5 | Per crew member, synced across devices | No |
| OQ-39 | In as-of views, are later invalidations shown or hidden by default? | **BBP** | 12 | Show state as of `T`; optional "include later corrections" | Yes |
| OQ-42 | Provisional extra conditions on "Reported in Error" via author correction (no other failed evidence; no recorded repair): approve or revise? | **Decided** (owner, 2026-10-15) | 7.7 | Conditions as COR-4 | Done |
| OQ-43 | Invalidation of a correction chain: whole chain excluded, no revival of earlier outcomes (INV-2a) — approve? | **Decided** (owner, 2026-10-15) | 7.8 | As INV-2a | Done |
| OQ-44 | Which actual-roster correction governs a shared obligation when several sessions disagree? | **Decided** (owner, 2026-10-15) | 15.3 | Latest governs; disagreement fails closed and notifies supervisors | Done |
| OQ-45 | Effect of later correction/invalidation of an Unable-to-Inspect check on a supervisor completion | **Decided** (owner, 2026-10-15) | 5.4.1 | As UTI-3 | Done |
| OQ-46 | Does a seal or item expire **on** its expiration date, or after it? | **Decided** (owner, 2026-10-20) | 10.1 | Usable **on** its expiration date, expired after it; a reseal accepts a date of today or later. The prototype ([PR #29](https://github.com/JustApu/CheckYourAmbulance-archive/pull/29), merged) implements this | Done |
| OQ-47 | Does *any* open deficiency on a sealed-kit item block seal substitution, or only count and expiration deficiencies? | **Decided** (owner, 2026-10-20) | 10.1 | Any open deficiency on a listed item blocks substitution until resolved through §8. The prototype blocks only count and expiration deficiencies and needs a follow-up. | Done |
| OQ-48 | State of a Reported-in-Error deficiency after invalidating its Correct-entry chain ([issue #31](https://github.com/JustApu/CheckYourAmbulance-archive/issues/31)) | **Decided** (owner, 2026-10-20) | 7.8, 8.2 | Reopen to Open, preserve invalid evidence; INV-7 and AC-32 implement the approved choice | Done — amendment 1.3.0 explicitly approved |
| OQ-49 | Baseline role naming and generic crew member terminology ([issue #45](https://github.com/JustApu/CheckYourAmbulance-archive/issues/45)) | **Decided** (owner, 2026-10-21) | 3.1, 16.6 | Baseline role is Crew; persona is crew member; clinical qualifications and volunteer/paid affiliation are decoupled from check attribution and verified by cross-referencing roster and training records | Done — amendment 1.4.0 explicitly approved |
| OQ-50 | Entering paper-recorded inspections into the app after an outage ([issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16)) | **Decided** (owner, 2026-10-21): paper fallback now, back-entry feature later | 17.8 | Paper records stay outside the app and are not entered as ordinary live checks (DEP-11). A back-entry feature needs its own amendment defining late-entry marking, who may enter, and what time is recorded | DEF — feature deferred; this amendment adds no behavior for it |
| OQ-51 | Does roster data that has aged past the staleness limit, without changing, reinstate a carry-over waiver granted on fresh data? ([PR #64](https://github.com/JustApu/CheckYourAmbulance-archive/pull/64) review, [issue #66](https://github.com/JustApu/CheckYourAmbulance-archive/issues/66)) | **Decided** (owner, 2026-10-23) | 5.6, 15.2 | No. Only a newer roster change, unavailability or conflict in the waiver's dependency window, or an invalidated or no-longer-qualifying relied-on check, reinstates it (CND-7). The HUM-3 limit still governs new determinations | Done — amendment 1.6.0 explicitly approved |
| OQ-52 | May an administrator apply a newly published checklist version to obligations that are already created and in-window ("apply to open obligations", §5.7)? ([PR #68](https://github.com/JustApu/CheckYourAmbulance-archive/pull/68) review, [issue #69](https://github.com/JustApu/CheckYourAmbulance-archive/issues/69)) | **Decided** (owner, 2026-10-23) | 5.7 | No: the option is not offered in this release. Obligations bind the version effective at `D`; an in-window or past obligation keeps its selection. The owner may reconsider in a future revision, by amendment | Done — amendment 1.7.0 explicitly approved |
| OQ-53 | How many obligations does a kit item produce when a vehicle has several identical kits? (design review of [PR #89](https://github.com/JustApu/CheckYourAmbulance-archive/pull/89) / [issue #73](https://github.com/JustApu/CheckYourAmbulance-archive/issues/73), [issue #91](https://github.com/JustApu/CheckYourAmbulance-archive/issues/91)) | **Decided** (owner, 2026-10-24) | 4.2, 5.1, 7.2, 10.1 | One obligation per kit instance: a kit is a template, a vehicle keeps zero, one or several instances, and each is checked on its own. Defaults for an instance added or removed mid-window are TIME-1a (decided by OQ-54) | Done: amendment 1.8.0 explicitly approved; the TIME-1a defaults were decided by OQ-54 (amendment 1.10.0) |
| OQ-54 | How are a kit instance's mid-window additions and removals handled, and where is its assignment history recorded in R1? (design review of [issue #92](https://github.com/JustApu/CheckYourAmbulance-archive/issues/92)) | **Decided** (owner, 2026-10-24) | 5.1, 10.1, 18 | Added mid-window: required from the next due instant; removed: later obligations retired, earlier ones keep their state; move = removal plus addition; temporary absence = gap with no backfill (TIME-1a). A minimal append-only `kit_assignment_event` is recorded in R1 and extended by R2 `kit_movement` | Done: amendment 1.10.0 explicitly approved |
| OQ-55 | What is the default plausibility threshold for an odometer reading, and does engine hours have one? ([issue #79](https://github.com/JustApu/CheckYourAmbulance-archive/issues/79)) | **Decided** (owner, 2026-10-25) | 6.4 | 1,000 mi (1,600 km) per reading, administrator-configurable and versioned; engine hours: lower-than-previous only | Done: amendment 1.11.0 explicitly approved |
| OQ-56 | Who sets a deficiency's severity, which levels exist, and what do they mean? ([PR #121](https://github.com/JustApu/CheckYourAmbulance-archive/pull/121) review, [issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80)) | **Decided** (owner, 2026-10-25) | 8.3 | The reporter chooses from administrator-configurable, versioned levels; starting levels Low, Medium, High, Critical with the criteria in DEF-8, shown beside the choice. A logged re-grade permission exists (§3.3; [P] supervisors and administrators) | Done: amendment 1.12.0 explicitly approved |
| OQ-57 | Does an Unable to Inspect check that still supports a deficiency count as "another effective failed check" under COR-4? ([issue #81](https://github.com/JustApu/CheckYourAmbulance-archive/issues/81), [PR #127](https://github.com/JustApu/CheckYourAmbulance-archive/pull/127)) | **Decided** (owner, 2026-10-25) | 7.7 | Yes: another effective Failed or Unable to Inspect chain keeps the deficiency open, because an unverified item is not a resolved defect (COR-4, AC-13) | Done: amendment 1.13.0 explicitly approved |
| OQ-58 | Should correcting an earlier odometer or engine-hours reading require explicit confirmation when the replacement would exceed the next effective reading in sequence? ([issue #125](https://github.com/JustApu/CheckYourAmbulance-archive/issues/125)) | **Decided** (owner, 2026-10-26) | 6.4 | Yes: require explicit confirmation for the correction when it would make the next effective reading lower than the corrected value; keep both readings and their sequence positions unchanged (ODO-3, ODO-5) | Done: amendment 1.14.0 |
| OQ-41 | May a supervisor mark complete an Unable-to-Inspect obligation they recorded themselves? | **Decided** (owner, 2026-10-25) | 5.4.1 | Yes, allowed and flagged in reports (UTI-3); implemented in [issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) Part A | Pending: explicit owner approval of the concrete 1.13.2 text |
| OQ-40 | Device/glove/lighting constraints for the UI | **DEF** | 20 | WCAG 2.2 AA baseline | No |

## 25. Approval and amendment process

*(Resolves OQ-26 as a proposal; the owner must approve this section itself.)*

### 25.1 Lifecycle

| Status | Meaning |
|---|---|
| **Draft** | Under review; no authority. |
| **Approved** | The owner has stated in writing, on the PR/issue, which version is approved and listing any decisions overridden. The `Status` line and version change to e.g. `1.0.0 — Approved` in the same commit. |
| **Amended** | A later approved change. |

An owner merge of the PR is **not by itself** approval of a Draft; approval must be explicit. Agents never approve or merge ([coordination procedures](docs/coordination.md#handoff-and-completion)).

### 25.2 Approval checklist (what must be true before the status flips)

1. Every **BBC** row in §24 has an owner-approved answer or an owner-approved safe default, recorded in the register with "Decision: …, date, approver".
2. Every **[OQ]** marker remaining in the body is either resolved or listed as BBP/DEF with an owner-approved interim.
3. Independent reviews were obtained from other agent families (auditability, authorization, concurrency, scheduling) and unresolved disagreements are recorded as `OWNER INPUT REQUIRED` items.
4. Link, consistency and traceability checks (§26) pass.

### 25.3 Follow-up changes upon approval

On approval (and not before), a single follow-up change updates [README.md](README.md), [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md), `REQUIREMENTS.md` and `OPEN_QUESTIONS.md` so that: `SPEC.md` is named authoritative in exactly one place per topic; `REQUIREMENTS.md` and `OPEN_QUESTIONS.md` are marked as the historical discovery record (with a banner pointing to the spec register) rather than competing instruction sources; and any registered linked-document blob IDs in `AGENTS.md` are refreshed in the same commit as per its rules.

### 25.4 Amendments

After approval, a change to product behavior or architecture is made by PR that: (a) states the reason and the affected requirement/AC IDs; (b) edits `SPEC.md` and bumps the version (**major**: removes or reverses an approved rule or invariant; **minor**: adds or materially changes behavior; **patch**: clarification with no behavior change); (c) appends to §27 Change log; (d) records any new unresolved decision in the §24 register with a class and safe default; (e) requires explicit owner approval for any minor/major change. Implementation issues must reference spec section/AC IDs. If code and spec diverge, the spec is not silently rewritten to match; either the implementation is fixed or an amendment is approved ([engineering standards](docs/engineering-standards.md#documentation-audit)).

### 25.5 Invariants requiring a major version

Changing any of the following is a major change: PRN-1–PRN-7, INV-3 (invalidation never closes a deficiency), COR-1 (author-only correction), OPS-2 (no automatic status change), the append-only history rule (AUD-1/AUD-3).

### 25.6 Approval record

Original approvals, amendments, reviews and their historical commit evidence remain in the [private archive](https://github.com/JustApu/CheckYourAmbulance-archive). Archive access is required to inspect those records; public readers cannot verify them directly. The clean-repository migration preserves these approvals and does not constitute a new product approval. Historical evidence applies to its original commit, not the new initial commit.

- **Version approved:** 1.0.0, consisting of the draft 0.1.0 merged in [PR #4](https://github.com/JustApu/CheckYourAmbulance-archive/pull/4) (merge commit [`349c135`](https://github.com/JustApu/CheckYourAmbulance-archive/commit/349c13534dee235e91a99ad1568574727d887dda)) plus the status normalization in [issue #5](https://github.com/JustApu/CheckYourAmbulance-archive/issues/5).
- **Approved by:** the owner, by explicit statement on 2026-10-16 ("Approve the spec").
- **Decisions overridden:** none.
- **Amendment 1.1.0 (2026-10-20):** the owner stated "I will accept Django & MariaDB for this project", resolving the language, framework and database part of OQ-24. Decisions overridden: none (it supersedes the working direction recorded on 2026-10-16). Hosting and deployment decisions are not part of this amendment.
- **Amendment 1.2.0 (2026-10-20):** the owner stated "I approve the SPEC 1.2.0 amendment described in [PR #34](https://github.com/JustApu/CheckYourAmbulance-archive/pull/34), subject to the remaining documentation corrections and successful final review. Claude may finalize the amendment, but merging still requires my authorization." The corrections were made and the final cross-family review of head [`b867a33`](https://github.com/JustApu/CheckYourAmbulance-archive/commit/b867a33ec69b82135a1636fafa140ebd6545cf10) (ChatGPT, OpenAI) found no blocking findings. Preceding owner decisions, recorded on [issue #18](https://github.com/JustApu/CheckYourAmbulance-archive/issues/18) and in the working session: minimum sealed-kit workflow in R1, supervisors request audits for now, Bulk Pass deferred (BLK-1–BLK-5 retained for a later release), an item is usable on its expiration date (OQ-46), and any open deficiency on a listed kit item blocks seal substitution (OQ-47). Decisions overridden: the R2 placement of detailed seal tracking and the R1 placement of Bulk Pass (§2.1, OQ-27 boundaries); no §25.5 invariant changed. Independent EMT or supervisor feedback on the prototype is deferred by owner decision ([issue #18](https://github.com/JustApu/CheckYourAmbulance-archive/issues/18)).
- **Amendment 1.3.0 (2026-10-20, [issue #20](https://github.com/JustApu/CheckYourAmbulance-archive/issues/20) / [issue #31](https://github.com/JustApu/CheckYourAmbulance-archive/issues/31), [PR #37](https://github.com/JustApu/CheckYourAmbulance-archive/pull/37)):** reopen a Reported-in-Error deficiency when the COR-3/COR-4 chain that cancelled it is invalidated (INV-7, §8.2, AC-32, OQ-48). The owner selected "Reopen" in the [issue #20](https://github.com/JustApu/CheckYourAmbulance-archive/issues/20) working session and then stated "I approve" in direct response to the request to approve the concrete SPEC 1.3.0 amendment in [PR #37](https://github.com/JustApu/CheckYourAmbulance-archive/pull/37). This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; the previously unspecified edge is resolved. No §25.5 invariant is reversed: the chain remains excluded and invalidation does not close a deficiency.
- **Amendment 1.4.0 (2026-10-21, [issue #45](https://github.com/JustApu/CheckYourAmbulance-archive/issues/45), [PR #46](https://github.com/JustApu/CheckYourAmbulance-archive/pull/46)):** replace "EMT" with "Crew" as the baseline built-in system role (§3.1, §3.3) and "crew member" as the generic persona throughout the specification. Inspection attribution records the authenticated user account without storing clinical titles; qualification compliance is cross-referenced with shift rosters (Humanity) and organization personnel/training records. Volunteer agency members and paid employees share the baseline Crew inspection role for R1. Confirmed on [PR #46](https://github.com/JustApu/CheckYourAmbulance-archive/pull/46) ([comment 6324400294](https://github.com/JustApu/CheckYourAmbulance-archive/pull/46#issuecomment-6324400294), 2026-10-21) at owner instruction: "You may post my confirmation of owner approval of the change," approving SPEC amendment 1.4.0 text and OQ-49 under §25.4(e). Decisions overridden: none; generalization of terminology and role naming. OQ-49 records the approved decision.
- **Amendment 1.5.0 (2026-10-21, [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16), [PR #47](https://github.com/JustApu/CheckYourAmbulance-archive/pull/47)):** hosting, secrets and recovery decisions (new §17.8 DEP-1 to DEP-12), NFR-5 recovery objectives (RPO 15 minutes, RTO 24 hours), OQ-24 and the §21 dependency row updated, OQ-50 added, and a §5.2 clarification that the fleet schedule and the conditional carry-over rule are agency configuration. The owner reviewed the concrete SPEC 1.5.0 text on [PR #47](https://github.com/JustApu/CheckYourAmbulance-archive/pull/47) and confirmed that the decision items are accurate, that the DEP-4 budget is a planning figure rather than a hard ceiling with donated resources possibly offsetting cost, that every item first proposed as [P] in §17.8 is approved, and that check cadence and the carry-over omission must be configurable because they are agency-specific. This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; no §25.5 invariant changed.
- **Amendment 1.6.0 (2026-10-23, [issue #66](https://github.com/JustApu/CheckYourAmbulance-archive/issues/66), [PR #67](https://github.com/JustApu/CheckYourAmbulance-archive/pull/67)):** new CND-7 limits when a re-evaluation reinstates a carry-over waiver; HUM-3 cross-reference; OQ-51. While reviewing [PR #64](https://github.com/JustApu/CheckYourAmbulance-archive/pull/64), the owner decided: "I agree with you. Stale roster data should be ignored instead of deemed unreliable. Stale inspections are the critical item." Asked about scope, the owner chose re-evaluation only, leaving HUM-3/CND-3 unchanged for new determinations. The owner then stated "I approve CND-7" in direct response to the request to approve the concrete CND-7 text in [PR #67](https://github.com/JustApu/CheckYourAmbulance-archive/pull/67). The HUM-3 sentence, OQ-51 and the contracts row only record and cross-reference that rule. This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; it refines how CND-5 applies HUM-3 and does not change the staleness limit for new determinations. No §25.5 invariant changed.
- **Amendment 1.7.0 (2026-10-23, [issue #69](https://github.com/JustApu/CheckYourAmbulance-archive/issues/69), [PR #70](https://github.com/JustApu/CheckYourAmbulance-archive/pull/70)):** §5.7 states that the administrator option "apply to open obligations" is not offered in this release, replacing "Owner confirmation required"; OQ-52. While [PR #68](https://github.com/JustApu/CheckYourAmbulance-archive/pull/68) ([issue #59](https://github.com/JustApu/CheckYourAmbulance-archive/issues/59)) was under review, the owner was asked to confirm that option and answered: "Confirmed, never, for now. We may change this in a future revision." The owner then stated "Approved" in direct response to the request to approve the concrete §5.7 and OQ-52 text in [PR #70](https://github.com/JustApu/CheckYourAmbulance-archive/pull/70). This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; it resolves the open confirmation in §5.7 without changing the approved obligation-binding rule. No §25.5 invariant changed.
- **Amendment 1.8.0 (2026-10-24, [issue #91](https://github.com/JustApu/CheckYourAmbulance-archive/issues/91), [PR #93](https://github.com/JustApu/CheckYourAmbulance-archive/pull/93)):** for a kit item an obligation is one per kit instance (§4.2, §5.1, TIME-1, TIME-1a, SCH-6, §7.2, CON-3, SEAL-10, §18, §18.1, §19.4); OQ-53. During the design review of [PR #89](https://github.com/JustApu/CheckYourAmbulance-archive/pull/89) ([issue #73](https://github.com/JustApu/CheckYourAmbulance-archive/issues/73)) the owner decided: "With several identical kits on one ambulance, checking just one kit SHOULD NOT satisfy the requirement for all of them, just the one checked. The 'kit' should be a template object but there may be zero, one or more than one that needs its individual checks (including quantities, expiration dates, etc.)", and confirmed "Yes, confirm one obligation per kit instance". The owner then stated "I approve the SPEC 1.8.0 text" in direct response to the request to approve the concrete text in [PR #93](https://github.com/JustApu/CheckYourAmbulance-archive/pull/93). This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; it replaces the **[P]** safe default in §4.2 for kit items only, and equipment assets are unchanged. No §25.5 invariant changed. The TIME-1a defaults for a kit instance added or removed mid-window remain **[P]** and reversible: the owner asked for them to be elaborated and has not yet confirmed them, so they need a separate owner decision before production use of that feature. After this approval the cross-family review (Codex, [PR #93](https://github.com/JustApu/CheckYourAmbulance-archive/pull/93)) found that the SEAL-10 sentence grouped breaking a seal with verification as satisfying an obligation, which contradicts SEAL-5; SEAL-10 was narrowed so only a valid seal substitution or the full inspection of the instance's listed items satisfies the obligation and a break satisfies nothing. This corrects wording to match SEAL-4 to SEAL-6 and changes no intended behavior; the owner was told of the change.
- **Amendment 1.9.0 (2026-10-24, [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78), [PR #90](https://github.com/JustApu/CheckYourAmbulance-archive/pull/90)):** §3.3, §6.2 and SES-3 clarify that all four roles may open/join sessions, participants alone may rejoin an ended session or manually end it, and a nonparticipant may start a separate session even while another is open. The owner stated: "All roles may open/join. Participants can rejoin or end. Other crew members can start a new inspection regardless of the previous inspection being closed, incomplete/stale or open." In response to the request to approve the concrete session amendment, the owner stated "SPEC amendment approved" on [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78), approving the text then numbered 1.8.0 under §25.4(e). After [PR #93](https://github.com/JustApu/CheckYourAmbulance-archive/pull/93) established kit-instance amendment 1.8.0, this same approved session amendment is renumbered 1.9.0; the approval predates and applies to the unchanged session text. This does not authorize merge. No §25.5 invariant changed.
- **Amendment 1.10.0 (2026-10-24, [issue #97](https://github.com/JustApu/CheckYourAmbulance-archive/issues/97), [PR #98](https://github.com/JustApu/CheckYourAmbulance-archive/pull/98)):** the mid-window rules for a kit instance become owner-decided (TIME-1a); a minimal append-only `kit_assignment_event` is recorded in R1 and extended by R2 `kit_movement` (§18, SEAL-1; OQ-54); a §3.3 row lets Supervisors, Supply Officers and Administrators add, move or deactivate a kit instance. Asked to decide the written options, the owner stated: "Decision 3 - I accept your recommendations. Decision 4 - I accept." The owner then stated: "I approve the additional text for [PR #98](https://github.com/JustApu/CheckYourAmbulance-archive/pull/98) regarding when an assignment takes effect. Kits may be added, moved or deactivated by Supervisors, Supply Officers or the Administrators." Asked to confirm approval of the rest of the concrete text, the owner stated: "I approve." This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; it settles the provisional TIME-1a defaults left by amendment 1.8.0 and narrows the SEAL-1 sentence that kept movement history in R2. The session-role amendment for [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) ([PR #90](https://github.com/JustApu/CheckYourAmbulance-archive/pull/90)) is recorded as version 1.9.0; its owner approval predates the renumbering. No §25.5 invariant changed.
- **Amendment 1.11.0 (2026-10-25, [issue #79](https://github.com/JustApu/CheckYourAmbulance-archive/issues/79), [PR #110](https://github.com/JustApu/CheckYourAmbulance-archive/pull/110)):** ODO-3 records the default plausibility threshold for odometer readings (1,000 miles, 1,600 km), kept as numbered administrator-set revisions, one unit per vehicle, and says engine hours need confirmation only when lower than the previous value (OQ-55). The owner stated on [PR #110](https://github.com/JustApu/CheckYourAmbulance-archive/pull/110): "[PR #110](https://github.com/JustApu/CheckYourAmbulance-archive/pull/110) SPEC amendment approved". This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; it settles the safe default that ODO-3 left to configuration. No §25.5 invariant changed.
- **Amendment 1.12.0 (2026-10-25, [issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80), [PR #121](https://github.com/JustApu/CheckYourAmbulance-archive/pull/121)):** new DEF-8 makes severity an administrator-configurable, versioned set of levels chosen by the reporting crew member, with a concise criteria description shown beside the choice and starting levels Low, Medium, High and Critical; a §3.3 row for configuring categories and severity levels and one for re-grading a severity; the §18 deficiency row; OQ-56. Asked which severity values to use and who sets them, the owner replied: "I'm okay with your proposed defaults though we should build based on the ability to add or change them. We also need to define the criteria for each severity level and display a concise description of each as the user chooses the severity for their report so they know they are selecting accurately." The owner then reviewed the concrete SPEC 1.12.0 text on [PR #121](https://github.com/JustApu/CheckYourAmbulance-archive/pull/121) and stated: "The ability to regrade a deficiency should be an available permission, with logging. Other than that amendment, I approve." The re-grade sentence in DEF-8 and its §3.3 row were added from that instruction; the roles holding the permission are the drafter's proposed default ([P]) and may be changed by the owner. This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; it settles a value list the specification left open. No §25.5 invariant changed.
- **Amendment 1.13.0 (2026-10-25, [issue #81](https://github.com/JustApu/CheckYourAmbulance-archive/issues/81), [PR #127](https://github.com/JustApu/CheckYourAmbulance-archive/pull/127)):** COR-4 and AC-13 say that another effective Failed **or Unable to Inspect** check keeps the deficiency open instead of Reported in Error, because an unverified item is not a resolved defect (OQ-57). The owner directed this rule while [PR #127](https://github.com/JustApu/CheckYourAmbulance-archive/pull/127) was in review and asked that COR-4 be adjusted to match the implementation, then reviewed the concrete wording and stated: "SPEC 1.13.0 approved". This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none; it clarifies the owner-approved COR-4 conditions (OQ-42) toward keeping the deficiency open. No §25.5 invariant changed.
- **Amendment 1.13.1 (2026-10-25, [issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82), [PR #128](https://github.com/JustApu/CheckYourAmbulance-archive/pull/128)):** add to §3.3 the administrator-only permission to invalidate a supervisor-completion determination, with explanation and retained original, as already specified by UTI-3. The owner explicitly approved this amendment on 2026-10-25, confirming that it documents the already approved behavior and retains the current policy implementation; the owner's words, recorded on [PR #128](https://github.com/JustApu/CheckYourAmbulance-archive/pull/128), were: "I confirm - supervisors can complete their own 'unable to inspect' checks. I also approve of the SPEC change at 1.13.1". This is a patch clarification under §25.4 because UTI-3 already defines the behavior; no permission behavior changes. Decisions overridden: none. No §25.5 invariant changed.
- **Amendment 1.13.2 (2026-10-25, [issue #132](https://github.com/JustApu/CheckYourAmbulance-archive/issues/132), [PR #134](https://github.com/JustApu/CheckYourAmbulance-archive/pull/134)):** OQ-41 becomes a decision and UTI-3 says so: a supervisor may complete an obligation whose Unable-to-Inspect check they recorded themselves, and reports flag that case; the §25.6 record for 1.13.1 now quotes the owner's approval. The owner stated on [PR #128](https://github.com/JustApu/CheckYourAmbulance-archive/pull/128): "I confirm - supervisors can complete their own 'unable to inspect' checks. I also approve of the SPEC change at 1.13.1". This amendment only records that confirmation; it changes no behavior ([issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) Part A already implements it) and is classed a patch under §25.4(b). The concrete 1.13.2 wording needs the owner's explicit approval under §25.4(e) before merge. Decisions overridden: none. No §25.5 invariant changed.
- **Amendment 1.14.0 (2026-10-26, [issue #125](https://github.com/JustApu/CheckYourAmbulance-archive/issues/125)):** require explicit confirmation for an odometer or engine-hours correction when the replacement value would exceed the next effective reading in sequence (making that next reading lower than the corrected value); keep both readings and their sequence positions unchanged (ODO-3, ODO-5, OQ-58). The owner stated on [issue #125](https://github.com/JustApu/CheckYourAmbulance-archive/issues/125): "Approved. Require explicit confirmation for the correction when it would make the next effective reading lower than the corrected value; keep both readings and their sequence positions unchanged." This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none. No §25.5 invariant changed.
- **Amendment 1.15.0 (2026-10-28, [issue #164](https://github.com/JustApu/CheckYourAmbulance-archive/issues/164)):** §8.2 gains the allowed transition Pending Verification to In Progress when a verification attempt fails, and VER-5 now says a Failed attempt records its evidence and returns the deficiency to In Progress (the stay-in-Pending alternative is removed), keeping the correction and its scope in history; a later correction creates a new correction event and a new scope revision. This resolves the conflict between VER-5 and the §8.2 transition list. On 2026-10-27 the owner chose "Return to In Progress" for a failed verification and, in the Claude Code session, selected "Approve as written" for the concrete 1.15.0 wording (relayed on [issue #164](https://github.com/JustApu/CheckYourAmbulance-archive/issues/164)). This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none. No §25.5 invariant changed (INV-3 is unaffected).
- **Amendment 1.16.0 (2026-10-28, [issue #146](https://github.com/JustApu/CheckYourAmbulance-archive/issues/146)):** DEF-4 adds that the assignee must be an active user who holds the Crew, Supervisor or Supply Officer role, so an account that holds only the Administrator role, a disabled user or a user of another organization cannot be assigned a deficiency. On 2026-10-27 the owner decided that the assignee rule is Crew, Supervisor or Supply Officer holders only (not Administrator-only accounts). On 2026-10-28, reviewing [PR #168](https://github.com/JustApu/CheckYourAmbulance-archive/pull/168), the owner approved the concrete wording: "I approve of the SPEC change incorporated." This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none. No §25.5 invariant changed.
- **Amendment 1.17.0 (2026-10-28, [issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149)):** §3.3 gains the row "Add a progress note to a deficiency" (Crew, Supervisors and Administrators on any deficiency; Supply Officers on supply-related deficiencies only), and §8.5 says that each revision of the dependency record is an appended event, that ending the dependency returns the deficiency to Open or In Progress as the person chooses (by default the status it was in before it began waiting), and that a progress note is an append-only, attributed entry on an active deficiency. On 2026-10-27 the owner decided that the exit status is the caller's choice with the prior status as the default, and that a progress note takes the same permission as updating the dependency record. On 2026-10-29 the owner reviewed the concrete wording, asked that the §3.3 row state that roles combine so a Supply Officer who also holds another role may add notes wherever that role allows, and then approved it ("After that, it is approved"). This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none. No §25.5 invariant changed.
- **Amendment 1.18.0 (2026-10-29, [issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153), [PR #179](https://github.com/JustApu/CheckYourAmbulance-archive/pull/179)):** §3.3 records the owner decision that any of the four roles may record a repair; VER-3 separates correction-time kit/compartment membership from the report-time vehicle affected-item snapshot and records the owner decision for vehicle-level affected systems; VER-4 makes scope revisioned and identifies the checklist definition and physical kit instance or no-kit case per row. The owner approved the role and vehicle-level affected-system decisions on 2026-10-27 and stated on [PR #179](https://github.com/JustApu/CheckYourAmbulance-archive/pull/179), "SPEC amendment 1.18 approved." on 2026-10-29. This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none. No §25.5 invariant changed.
- **Amendment 1.19.0 (2026-10-30, [issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153)):** VER-2 permits an explicit closure allowed by §8.8; the §3.3 matrix permits Supervisors and Administrators to explicitly close a deficiency before correction only when its current correction-time verification scope is empty, while the existing Administrator-only general closure remains unchanged. §8.2 adds the distinct status Closed — No Current Verification Items and an Administrator-only reopen transition. §8.8 states that checklist publication or kit-assignment changes may remove all current verification items after reporting; correction remains refused with `EMPTY_SCOPE` and writes no correction event, with no report-time fallback; a distinct explained close command re-resolves scope under vehicle/checklist locks and refuses when scope remains nonempty. A deficiency in Pending Verification keeps its already materialized scope and cannot use this path. INV-3 and AC-14 continue to prohibit automatic closure on invalidation and refer to this limited explicit-closure exception. The Supervisor/Administrator closure has its own attributed event. The owner decided the role and circumstance on 2026-10-30 (“Supervisors and administrators may close the deficiency in that circumstance”) and approved this concrete wording on 2026-10-30 (“Approved.”) under §25.4(e). This explicitly approves the minor amendment but does not authorize merge. Decisions overridden: none. No §25.5 invariant changed.
- **Amendment 1.20.0 (2026-10-30, [issue #154](https://github.com/JustApu/CheckYourAmbulance-archive/issues/154)):** VER-3 states the item-target scope rule that [issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153) implemented but VER-3 did not name: an item-level deficiency's verification scope is one row for each active physical kit instance of a kit-defined item, or one row for a non-kit item, and each row needs its own qualifying pass (source: the implementer's clarification on [PR #179](https://github.com/JustApu/CheckYourAmbulance-archive/pull/179)). VER-4 states which verification checks are accepted (a deficiency on the check's vehicle that is Pending Verification, an item and kit instance in the newest scope revision, a Pass or a Fail), that a Pass in an older revision never satisfies a newer one, and that a Failed verification check is evidence only for the deficiency it verifies. This is classed a minor amendment under §25.4. The owner approved this concrete wording on 2026-10-30 (“I approve of the amendment.”), after it was presented in the Claude Code session, under §25.4(e). This explicitly approves the minor amendment but does not authorize merge. Decisions overridden: none. No §25.5 invariant changed (INV-3 is unaffected: a deficiency whose supporting check chain was invalidated is not closed by verification and awaits an explicit closure under §8.8).
- **Amendment 1.21.0 (2026-10-30, [issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158)):** §3.3 grants the ad hoc inspection command to Supervisors and Administrators. ADH-1 defines explicit item/physical-kit scope, future due time, immediate opening with no extra grace, published requirements effective at the due time known when requested, identity separate from routine selections, no routine reconciliation/kit retirement, and no Out-of-Service exemption or carry-over. Existing link cardinality is unchanged and return to service alone creates nothing. The owner reviewed this concrete text in the [issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158) implementation session and replied "Approve as written" on 2026-10-30. This explicitly approves the minor amendment under §25.4(e); no §25.5 invariant changes and no merge is authorized.
- **Amendment 1.22.1 (2026-10-31, [issue #22](https://github.com/JustApu/CheckYourAmbulance/issues/22)):** Owner-requested, nonbehavioral generalization for public readiness. Generalizes agency terminology, synthetic names, and demonstration ambulance labels; clarifies that staffing may be volunteer, paid, or mixed by agency choice; and removes organization-specific wording from §17.8 DEP-1, DEP-3, DEP-4, and DEP-7. Existing schedule seed values remain configurable and unchanged. The one-organization first-release scope, schedule behavior, recovery controls, and conditional-rule semantics are unchanged. No owner decision or §25.5 invariant is changed.
- **Amendment 1.22.0 (2026-10-30, [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)):** VER-4 lets an effective Pass keep satisfying a row that a supervisor expansion repeats in its new revision; added rows take the expansion instant, and Passes still never carry over to the revision of a new correction. VER-3 keeps supervisor expansions with the deficiency so every later correction's scope includes each expanded item, and each expanded physical kit instance while it still holds that item on the vehicle's current checklist (resolved at correction time). §8.7 states that the supervisor names the items and kit instances to add after closure. In the Claude Code session the owner chose "Keep them" (prior Passes after an expansion) and "Named extra items" (added verification after closure), then selected "Approve as written" for this concrete wording (recorded on [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)). After the cross-family review of [PR #186](https://github.com/JustApu/CheckYourAmbulance-archive/pull/186) found that a moved kit instance could block later corrections, the owner selected "Approve revision" for the VER-3 kit-instance sentence now shown (2026-10-30, recorded on [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156)). This explicitly approves the minor amendment under §25.4(e), but does not authorize merge. Decisions overridden: none. No §25.5 invariant changed.
- **Open items carried forward:** the §24 rows not marked **Decided** keep their stated safe defaults as interim behavior. Owner clarification (2026-10-16, recorded on [PR #6](https://github.com/JustApu/CheckYourAmbulance-archive/pull/6)): these undecided defaults may guide prototypes and development, but each requires a separate owner decision before production use of that feature. **BBP** rows must be decided before production use of the affected feature; **DEF** defaults remain provisional until their named release. The application framework and database are **decided** (Python/Django and MariaDB 10.11, by the amendment above). Hosting, secrets and backups are **decided** (amendment 1.5.0, [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16)). What remains under OQ-24 is production setup, the DEP-12 pilot gates, and the spike's other unverified items, which follow the separate verification paths listed in §17.5 (Google token claims under [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25), hardening under [issue #6](https://github.com/JustApu/CheckYourAmbulance/issues/6); hot backup tools, load and high availability have no owning issue yet).

## 26. Traceability to the discovery baseline

### 26.1 REQUIREMENTS.md → SPEC.md

| REQUIREMENTS.md | Topic | SPEC.md |
|---|---|---|
| §1 Purpose and boundaries | scope, principles | §1, §0 |
| §2.1 Templates and versioning | templates, overrides, deviations | §4.3, §4.5, §5.7 |
| §2.2 Initial fleet schedule | schedule, windows, conditional, out-of-service | §5.2–5.3, §5.6, §5.9 |
| §2.3 Sessions and individually recorded checks | sessions, 6 h, outcomes, auto-save, concurrency, bulk, corrections, invalidation, odometer | §6, §7, §19 |
| §2.4 Inspection views | priority/normal, attention filter, QR | §6.5 |
| §3 Kits, inventory, expirations | kits, seals, inventory, POs | §10 |
| §4 Deficiency lifecycle and verification | all deficiency rules, suspension | §8 |
| §5 Ambulance operations, maintenance, contracts | status, assets, PM, contracts | §9, §11 |
| §6 Regulatory and historical compliance | regulatory, as-of, search, exports | §12, §14 |
| §7 Users, permissions, notifications | Workspace, roles, participation, notifications, dashboard | §3, §13, §14, §15.1 |
| §8 Humanity integration | schedule, roster correction | §15.2–15.3 |
| §9 Delivery and nonfunctional | responsive, online, validation, audit | §16, §17, §20 |
| §10 Planned release boundaries | R1–R4 | §2 |
| §11 Review/change control | governance | §25 |

### 26.2 PROJECT_HANDOFF.md invariants → SPEC.md

| HO invariant | SPEC.md |
|---|---|
| 1 Item-level compliance | PRN-1, §5.1, AC-1/2 |
| 2 Concurrent crew work | §7.3–7.6, AC-7–11 |
| 3 Preserve evidence | PRN-4, §7.7–7.8, §16.1, AC-12/15/25 |
| 4 No false Pass | §5.4, §8.9, §14 RPT-7, AC-18 |
| 5 Deficiency is independent | PRN-6, §7.5, INV-3, AC-10/14 |
| 6 Controlled vehicle status | OPS-2, AC-19 |
| 7 Scoped verification | VER-3/4, AC-16 |
| 8 Online-only | PRN-7, §16.4, REC-2 |
| 9 Rosters, not payroll | HUM-1/5, §15.3, AC-23 |
| 10 Access control | §3, §15.1, AC-22/24 |

### 26.3 OPEN_QUESTIONS.md "Explicitly decided" → SPEC.md

| Decided item | SPEC.md |
|---|---|
| Ambulance 4 example: 18:00 daily | §5.2 |
| ±1 h default, admin-configurable | §5.2–5.3 |
| 6 h inactivity, crew reopen, refresh, no summary | SES-1–4 |
| Preserve original version/checks on reopen | CHK-4, SES-4 |
| Auto-save, live sync, bulk pass, warn not lock | §7.3–7.6 |
| Own-only correction, supervisors make new check | COR-1 |
| Failed→Passed accidental correction → Reported in Error | COR-3/4 |
| Admin-only invalidation; deficiency stays open | INV-1–3 |
| View/filter persistence; suspended excluded from attention filter | §6.5 (see OQ-38) |
| Suspended checks not passes; end on closure | §8.9 |
| Engine hours optional, odometer mandatory | §6.4 |
| Humanity hourly | §15.2 |
| Automated reports optional | RPT-6 |
| Weekly sealed audits not confirmed | STK-C |

## 27. Change log

| Version | Date | Change |
|---|---|---|
| 1.22.1 | 2026-10-31 | **Owner-requested, nonbehavioral identity generalization ([issue #22](https://github.com/JustApu/CheckYourAmbulance/issues/22); recorded in §25.6).** Uses agency terminology and synthetic ambulance labels, describes agency staffing as volunteer, paid, or mixed, and generalizes DEP-1, DEP-3, DEP-4 and DEP-7. Existing configurable schedule seed values, one-organization first-release scope, schedule behavior, recovery controls and conditional-rule semantics are unchanged. No decision or §25.5 invariant changed. |
| 1.22.0 | 2026-10-30 | **Owner-approved minor amendment (§25.4(e), [issue #156](https://github.com/JustApu/CheckYourAmbulance-archive/issues/156); approval record in §25.6).** VER-4: an effective Pass keeps satisfying a row a supervisor expansion repeats; added rows take the expansion instant; no carry-over to a new correction's revision. VER-3: supervisor expansions persist into every later correction's scope (kit instances while they still hold the item on the current checklist). §8.7: added verification names items and kit instances. No §25.5 invariant changed. |
| 1.21.0 | 2026-10-30 | **Owner-approved minor amendment (§25.4(e), [issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158)).** §3.3 grants ad hoc inspection requests to Supervisors and Administrators; ADH-1 defines explicit per-item/kit scope, request-time opening, due-time deadline, immutable pins and isolation from routine selection/reconciliation. OQ-31 retains no automatic inspection on return to service. No §25.5 invariant changes. |
| 1.20.0 | 2026-10-30 | **Owner-approved minor amendment (§25.4(e), [issue #154](https://github.com/JustApu/CheckYourAmbulance-archive/issues/154); approval record in §25.6).** VER-3 states the item-level scope rule (one row per active physical kit instance of a kit item, one row for a non-kit item); VER-4 states which verification checks are accepted, that older-revision Passes never satisfy a newer revision, and that a Failed verification is evidence only for the deficiency it verifies. No §25.5 invariant changed. |
| 1.19.0 | 2026-10-30 | **Owner-approved minor amendment (§25.4(e), [issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153)).** VER-2 permits explicit closure allowed by §8.8. §3.3 adds Supervisor and Administrator permission to explicitly close before correction only when correction-time scope is empty. §8.2 adds Closed — No Current Verification Items and the Administrator-only reopen transition; §8.8 keeps correction refusal fail-closed, disallows report-time fallback, preserves materialized scope after correction and requires a distinct explained closure after re-resolving current scope under locks. INV-3/AC-14 continue to prohibit automatic closure on invalidation while referring to the limited explicit-closure exception. The existing Administrator-only general closure is unchanged. Owner decision: “Supervisors and administrators may close the deficiency in that circumstance.” No §25.5 invariant changed. |
| 1.18.0 | 2026-10-29 | **Owner-approved minor amendment (§25.4(e), [issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153), [PR #179](https://github.com/JustApu/CheckYourAmbulance-archive/pull/179); approval record in §25.6).** §3.3 records the four-role permission to record a repair; VER-3 distinguishes correction-time kit/compartment scope from the report-time vehicle-level affected-item snapshot and records the owner decision; VER-4 makes scope revisioned and identifies the checklist definition and physical kit instance or no-kit case. No §25.5 invariant changed. |
| 1.17.0 | 2026-10-28 | **Owner-approved minor amendment (§25.4(e), [issue #149](https://github.com/JustApu/CheckYourAmbulance-archive/issues/149); approval record in §25.6).** §3.3 adds the row "Add a progress note to a deficiency"; §8.5 adds that dependency revisions are appended events, that ending a dependency returns the deficiency to Open or In Progress as chosen (default: the status before it began waiting), and that a progress note is an append-only, attributed entry on an active deficiency (owner decision 2026-10-27); the §3.3 row notes that roles combine (§3.2). No §25.5 invariant changed. |
| 1.16.0 | 2026-10-28 | **Owner-approved minor amendment (§25.4, [issue #146](https://github.com/JustApu/CheckYourAmbulance-archive/issues/146), [PR #168](https://github.com/JustApu/CheckYourAmbulance-archive/pull/168); approval record in §25.6).** DEF-4: the assignee must be an active user who holds the Crew, Supervisor or Supply Officer role (owner decision 2026-10-27). No §25.5 invariant changed. |
| 1.15.0 | 2026-10-28 | **Owner-approved minor amendment (§25.4, [issue #164](https://github.com/JustApu/CheckYourAmbulance-archive/issues/164); approval record in §25.6).** §8.2 adds the transition Pending Verification to In Progress when a verification attempt fails; VER-5 says a Failed attempt records its evidence and returns the deficiency to In Progress, keeping the correction and its scope in history, and that a later correction creates a new correction event and scope revision. No §25.5 invariant changed. |
| 1.14.0 | 2026-10-26 | **Owner-approved minor amendment (§25.4, [issue #125](https://github.com/JustApu/CheckYourAmbulance-archive/issues/125); approval record in §25.6).** ODO-3: an author's correction to an odometer or engine-hours reading requires explicit confirmation when the replacement value would exceed the next effective reading in sequence (making that later reading lower than the corrected value); both readings and their sequence positions remain unchanged (OQ-58). No §25.5 invariant changed. |
| 1.13.2 | 2026-10-25 | **Patch clarification awaiting explicit owner approval of its concrete text (§25.4, [issue #132](https://github.com/JustApu/CheckYourAmbulance-archive/issues/132); record in §25.6).** OQ-41 decided (a supervisor may complete their own Unable-to-Inspect check; flagged in reports); UTI-3 no longer calls it open; the 1.13.1 record quotes the owner's approval. No behavior change; no §25.5 invariant changed. |
| 1.13.1 | 2026-10-25 | **Owner-approved patch clarification (§25.4, [issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82), [PR #128](https://github.com/JustApu/CheckYourAmbulance-archive/pull/128); approval record in §25.6).** Adds the administrator-only §3.3 permission row for invalidating a supervisor-completion determination, with explanation and retained original, as already specified in UTI-3. No behavior change; no §25.5 invariant changed. |
| 1.13.0 | 2026-10-25 | **Owner-approved minor amendment (§25.4, [issue #81](https://github.com/JustApu/CheckYourAmbulance-archive/issues/81), [PR #127](https://github.com/JustApu/CheckYourAmbulance-archive/pull/127); approval record in §25.6).** COR-4 and AC-13: another effective Failed or Unable to Inspect check keeps the deficiency open (OQ-57). No §25.5 invariant changed. |
| 1.12.0 | 2026-10-25 | **Owner-approved minor amendment (§25.4, [issue #80](https://github.com/JustApu/CheckYourAmbulance-archive/issues/80), [PR #121](https://github.com/JustApu/CheckYourAmbulance-archive/pull/121); approval record in §25.6).** DEF-8: severity is an administrator-configurable, versioned set of levels chosen by the reporter, each with concise criteria shown at the choice; starting levels Low, Medium, High, Critical; §3.3 rows for configuring categories and severity levels and for re-grading a severity (logged); §18 deficiency row; OQ-56. No §25.5 invariant changed. |
| 1.11.0 | 2026-10-25 | **Owner-approved minor amendment (§25.4, [issue #79](https://github.com/JustApu/CheckYourAmbulance-archive/issues/79), [PR #110](https://github.com/JustApu/CheckYourAmbulance-archive/pull/110); approval record in §25.6).** ODO-3 gains the safe default for the configurable plausibility threshold (1,000 mi / 1,600 km, versioned, one unit per vehicle) and says engine hours are checked only for a lower value; OQ-55 records it. No §25.5 invariant changed. |
| 1.10.0 | 2026-10-24 | **Owner-approved minor amendment (§25.4, [issue #97](https://github.com/JustApu/CheckYourAmbulance-archive/issues/97), [PR #98](https://github.com/JustApu/CheckYourAmbulance-archive/pull/98); approval record in §25.6).** TIME-1a becomes owner-decided (added kit required from the next due instant; removed kit retires only later obligations; move = removal plus addition); a minimal append-only `kit_assignment_event` is recorded in R1 (§18, SEAL-1, OQ-54); a §3.3 row lets supervisors, supply officers and administrators add, move or deactivate a kit instance. No §25.5 invariant changed. |
| 1.9.0 | 2026-10-24 | **Owner-approved minor amendment (§25.4, [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78), [PR #90](https://github.com/JustApu/CheckYourAmbulance-archive/pull/90); approval record in §25.6).** All roles may open/join; participants alone may reopen or manually end; nonparticipants may start a new session regardless of other session state. No §25.5 invariant changed. |
| 1.8.0 | 2026-10-24 | **Owner-approved minor amendment (§25.4, [issue #91](https://github.com/JustApu/CheckYourAmbulance-archive/issues/91), [PR #93](https://github.com/JustApu/CheckYourAmbulance-archive/pull/93); approval record in §25.6).** For a kit item an obligation is one per kit instance: a kit is a template and a vehicle keeps zero, one or several instances, each checked on its own (§4.2, §5.1, TIME-1, new TIME-1a, SCH-6, §7.2, CON-3, new SEAL-10, §18, §18.1, §19.4); OQ-53 added and decided. Replaces the §4.2 slot default for kit items. No §25.5 invariant changed. |
| 1.7.0 | 2026-10-23 | **Owner-approved minor amendment (§25.4, [issue #69](https://github.com/JustApu/CheckYourAmbulance-archive/issues/69), [PR #70](https://github.com/JustApu/CheckYourAmbulance-archive/pull/70); approval record in §25.6).** §5.7 records the owner's decision that the administrator option "apply to open obligations" is not offered in this release and may be reconsidered later; OQ-52 added and decided. Replaces "Owner confirmation required". No §25.5 invariant changed; the obligation generation implemented in [PR #68](https://github.com/JustApu/CheckYourAmbulance-archive/pull/68) already behaves this way. |
| 1.6.0 | 2026-10-23 | **Owner-approved minor amendment (§25.4, [issue #66](https://github.com/JustApu/CheckYourAmbulance-archive/issues/66), [PR #67](https://github.com/JustApu/CheckYourAmbulance-archive/pull/67); approval record in §25.6).** New CND-7 says a re-evaluation reinstates a carry-over waiver only on a newer roster change, unavailability or conflict in its dependency window, or an invalidated relied-on check, not because unchanged roster data aged; HUM-3 states that its limit governs new determinations; OQ-51 added and decided. No §25.5 invariant changed. |
| 1.5.0 | 2026-10-21 | **Owner-approved minor amendment (§25.4, [issue #16](https://github.com/JustApu/CheckYourAmbulance-archive/issues/16), [PR #47](https://github.com/JustApu/CheckYourAmbulance-archive/pull/47); approval record in §25.6).** Hosting, secrets and recovery decisions (new §17.8 DEP-1 to DEP-12), NFR-5 set to RPO 15 minutes and RTO 24 hours, OQ-24 and the §21 dependency row updated, OQ-50 added, and §5.2 states that the fleet schedule and the carry-over omission are agency configuration. Builds on amendment 1.4.0 ([issue #45](https://github.com/JustApu/CheckYourAmbulance-archive/issues/45), [PR #46](https://github.com/JustApu/CheckYourAmbulance-archive/pull/46)). No §25.5 invariant changed. |
| 1.4.0 | 2026-10-21 | **Owner-approved minor amendment (§25.4, [issue #45](https://github.com/JustApu/CheckYourAmbulance-archive/issues/45); approval record in §25.6).** Generalize terminology and baseline role: replace "EMT" with "Crew" as the baseline built-in system role (§3.1, §3.3 matrix) and "crew member" as the generic persona. Clarify decoupled attribution in §3.1 and §16.6 (no clinical titles in check records; cross-referenced with roster and training records; volunteers and paid employees share the baseline Crew role). OQ-49 records the approved decision. Downstream contracts and application shell aligned. |
| 1.3.0 | 2026-10-20 | **Owner-approved minor amendment**, [issue #20](https://github.com/JustApu/CheckYourAmbulance-archive/issues/20) / [issue #31](https://github.com/JustApu/CheckYourAmbulance-archive/issues/31), [PR #37](https://github.com/JustApu/CheckYourAmbulance-archive/pull/37) (§25.6): INV-7, §8.2 and AC-32 define reopening after invalidation of a COR-3/COR-4 Reported-in-Error chain; OQ-48 records the approved decision. No §25.5 invariant changed. |
| 1.2.0 | 2026-10-20 | **Owner-approved amendment (§25.4, [issue #33](https://github.com/JustApu/CheckYourAmbulance-archive/issues/33), [PR #34](https://github.com/JustApu/CheckYourAmbulance-archive/pull/34); approval record in §25.6).** Minimum sealed-kit workflow moved into R1 (§2.1, §10.1, SEAL-1–SEAL-9, AC-33–AC-35, §3.3 rows); Bulk Pass deferred from R1 (§7.6, OQ-11, AC-11); OQ-14 partly decided; OQ-46 added and decided (usable on the expiration date, expired after it); OQ-47 added and decided (any open deficiency on a listed item blocks seal substitution); "expiring soon" alerts and reorder flags deferred. Follows 1.1.0 ([PR #30](https://github.com/JustApu/CheckYourAmbulance-archive/pull/30)). |
| 1.1.0 | 2026-10-20 | OQ-24 decided by owner-approved amendment: Python/Django + MariaDB 10.11 (§17.5, new §17.7, §24, Appendix A banner, §25.6). Minor change per §25.4. |
| 1.0.0 | 2026-10-16 | Approved by the owner (§25.6). Status and authority text updated; wording polish from the [PR #4](https://github.com/JustApu/CheckYourAmbulance-archive/pull/4) review; no change to approved behavior. |
| 0.1.0-draft | 2026-10-15 | Initial draft from discovery baseline 0.1 ([issue #3](https://github.com/JustApu/CheckYourAmbulance-archive/issues/3)). Not approved. |
| 0.1.0-draft | 2026-10-15 | Owner decisions recorded (OQ-13, OQ-27, OQ-28–30, OQ-42–45, matrix cells); Appendix A added and updated for the owner's MySQL/MariaDB leaning. |

## Appendix A. Stack comparison note (non-binding)

> **Resolved 2026-10-20.** The owner chose Python/Django + MariaDB (§17.5). This appendix is kept as the evidence trail for the options considered; its recommendation is superseded by that decision.

**Purpose.** Support the OQ-24 decision. This appendix decides nothing and asserts no approved stack. **Verification status:** the author could not reach official documentation from this environment (outbound requests to the relevant documentation sites were refused), so every capability statement below is marked **U (unverified: general knowledge, confirm against official documentation before relying on it)**. Per [AGENTS.md](AGENTS.md), none of it may be used as an interface or capability guarantee.

**Owner constraints [C]:** database leaning toward MySQL or MariaDB (2026-10-15, not final); Linux server; any stack acceptable; Perl preferred but negotiable; publicly facing, so security is weighted heavily. **[P]** Long-term maintainability matters; the agency may use volunteer, paid, or mixed staffing (author's inference).

### A.1 Candidates

| | A1 | A2 | A3 | B |
|---|---|---|---|---|
| Language/framework | Perl + Mojolicious | Perl + Mojolicious | Perl + Mojolicious | A mainstream mature framework in another language (e.g. Python + Django) |
| Database | SQLite | PostgreSQL | MySQL or MariaDB (owner's leaning) | PostgreSQL or MySQL/MariaDB |
| Notes | Matches the inherited repo guidance only; adoption is not implied | Keeps the language preference; server database with enforced privileges | Keeps the language preference and the owner's database leaning; MySQL vs MariaDB still to choose | Largest contributor pool and more built-in security features (U); abandons the language preference |

### A.2 Fit to the required capabilities (§17.2)

| Criterion | A1 Mojolicious + SQLite | A2 Mojolicious + PostgreSQL | A3 Mojolicious + MySQL/MariaDB | B Mainstream framework + PostgreSQL or MySQL/MariaDB |
|---|---|---|---|---|
| ARC-1 transactions, unique constraints, serialization per `(vehicle,item)` | Transactions and unique constraints expected (U); one writer at a time (U) — probably adequate at this scale, must be load-tested | Row-level locking, strong isolation options (U) | Transactional storage engine with row locking (U); confirm the isolation behavior the conflict check (CON-3/CON-5) relies on | Same as the chosen database (U) |
| ARC-2 INSERT-only enforcement on append-only tables | Embedded library without per-role privileges (U); triggers and application discipline only, weaker against a compromised app | Role-based GRANT/REVOKE plus triggers (U) | Per-user privileges plus triggers (U); must be proven by test | Same as the chosen database (U) |
| ARC-3 server push | WebSocket / server-sent-event support to be confirmed (U) | Same as A1 | Same as A1 | Often needs an add-on or async server (U) |
| ARC-4 restart-safe scheduled jobs | A Perl job queue exists (U); with SQLite it shares the single-writer limit | Job queue support to be confirmed for this database (U) | Job queue support to be confirmed for this database (U) | Mature options (U) |
| ARC-5 time-zone/DST library | Perl date-time libraries exist (U) | Same | Same; also confirm database time-zone handling is avoided or tested (store UTC, TIME-2) | Standard library plus mature packages (U) |
| ARC-6 verified OIDC/Google ID-token validation | Library availability and maintenance to be confirmed (U); highest-risk gap for a public-facing system | Same as A1 | Same as A1 | Mature, widely used libraries (U) |
| ARC-7 backup/restore and point-in-time recovery for as-of audits | File-copy backups; no built-in point-in-time recovery (U) | Continuous archiving / point-in-time recovery (U) | Full backups plus binary-log replay for point-in-time recovery (U); must be rehearsed | Same as the chosen database |
| ARC-8 offline tests with injected stubs | Feasible | Feasible | Feasible | Feasible |
| Data strictness for audit data | Loose typing by default (U) | Strict by default (U) | Depends on strict SQL mode and constraint enforcement being configured and kept on (U); enforce at startup | Same as the chosen database |
| Public-facing hardening (CSRF, cookies, validation, dependency audit) | Assembled and audited by the team (U) | Same as A1 | Same as A1 | More built in; larger security-review community (U) |
| Maintainability / contributor pool | Smaller Perl pool; owner's preference | Same as A1 | Same as A1; database skills widely available | Larger pool |

### A.3 Proposed recommendation **[P]**

1. **Do not choose A1** for production: ARC-2 (immutability against a compromised application) and ARC-7 (point-in-time recovery for as-of audits) are weak for a public-facing, audit-critical system.
2. **A3 is the working direction**: it honors the owner's language preference and database leaning. MySQL vs MariaDB is to be chosen in the spike by which one passes the verification tasks and has the better-maintained Perl driver and operations story on the chosen Linux distribution (U). A2 remains an alternative if the database tests fail; **B** is the fallback if ARC-6 or session/CSRF tooling proves inadequate in Perl.
3. The choice is hard to reverse, so make it through a short architecture issue that performs the tasks below with real installs rather than relying on this table. The decision is the owner's (OQ-24).

### A.4 Verification tasks for the architecture issue

1. Confirm from official documentation: framework support for WebSocket and/or server-sent events; maintained OIDC/Google ID-token validation (signature, issuer, audience, expiry, organization claim) and its security record; Perl driver maintenance for each database.
2. Prove ARC-2: a database account that cannot UPDATE or DELETE append-only tables, with a test that attempts both, and confirm the application connects only with that account.
3. Prove strict data handling: startup check that strict mode and constraint enforcement (unique, foreign key, CHECK) are active; tests that invalid or oversized values are rejected rather than coerced or truncated.
4. Prove CON-5 and REC-4: simultaneous check submissions for one `(vehicle,item)` and replayed operation ids, asserting one commit and correct conflict warnings under the database's actual isolation level.
5. Prove ARC-4/TIME-3: an idempotent obligation generator across spring-forward and fall-back boundaries, storing UTC.
6. Test backup and point-in-time restore to a chosen instant and rebuild projections (§19, AC-26).
7. Choose MySQL or MariaDB on those results and operations fit; review dependency footprint, patch cadence and public-exposure hardening (TLS termination, rate limiting, session settings).
8. Record the result as an OQ-24 decision for the owner to approve.
