# Acceptance-scenario traceability

Authoritative mappings: `contracts/step-catalog.json` (`cases[*].requirements`); scenarios and this table must match it. Catalog baseline: `SPEC.md` §23 (approved version 1.2.0); the current approved SPEC remains authoritative. **Unbound entries are structured behavioral contracts, not executed tests.** The runner status column and supplemental coverage below identify executable bindings; pending contracts are reported as skipped.

| Criterion | Scenario | SPEC references | Runner status |
|---|---|---|---|
| AC-1 | [AC-01.json](scenarios/AC-01.json) | PRN-1, SCH-1 | Symbolic; unimplemented |
| AC-2 | [AC-02.json](scenarios/AC-02.json) | PRN-1, SCH-1; §6.1 | Symbolic; unimplemented |
| AC-3 | [AC-03.json](scenarios/AC-03.json) | SCH-1, SCH-2, SCH-6 | Symbolic; unimplemented |
| AC-4 | [AC-04.json](scenarios/AC-04.json) | TIME-1, TIME-2, TIME-3 | Symbolic; unimplemented |
| AC-5 | [AC-05.json](scenarios/AC-05.json) | CND-1, CND-2, CND-3 | Symbolic; unimplemented |
| AC-6 | [AC-06.json](scenarios/AC-06.json) | SES-1, SES-2, SES-3, CHK-4 | Symbolic; unimplemented |
| AC-7 | [AC-07.json](scenarios/AC-07.json) | REC-1, REC-2 | Symbolic; unimplemented |
| AC-8 | [AC-08.json](scenarios/AC-08.json) | REC-4 | Symbolic; unimplemented |
| AC-9 | [AC-09.json](scenarios/AC-09.json) | CON-1, CON-2, CON-3 | Covered by direct tests in `cya/checks/tests/test_conflicts.py` (warning, proceed keeps both, different items unblocked); scenario stays symbolic |
| AC-10 | [AC-10.json](scenarios/AC-10.json) | CON-1, CON-2, CON-4, PRN-6 | Covered by direct tests in `cya/checks/tests/test_conflicts.py` (outcome-independent warning) and `cya/deficiencies/tests/test_from_check.py` (a Pass and a Fail race: both retained, one deficiency); scenario stays symbolic |
| AC-11 | [AC-11.json](scenarios/AC-11.json) | §7.6, §2.1a (deferred from R1; no requirement ID) | Symbolic; unimplemented |
| AC-12 | [AC-12.json](scenarios/AC-12.json) | COR-1, COR-2 | Author-only correction, independent check, stale-tip conflict and as-of effective outcome covered by `cya/checks/tests`; scenario remains symbolic |
| AC-13 | [AC-13.json](scenarios/AC-13.json) | COR-3, COR-4 | Covered by direct tests in `cya/deficiencies/tests/test_effects.py` (Reported in Error with history; another effective Failed or Unable to Inspect chain, recorded repair or status keeps it open with a supervisor-notice event); scenario stays symbolic |
| AC-14 | [AC-14.json](scenarios/AC-14.json) | INV-1, INV-2, INV-3 | Partly covered by direct tests in `cya/checks/tests/test_invalidation.py` (administrator-only invalidation, obligation returns to unsatisfied); the deficiency half (INV-3) is covered by `cya/deficiencies/tests/test_effects.py` and `test_from_check.py` (the deficiency stays Open and keeps its evidence); scenario stays symbolic |
| AC-15 | [AC-15.json](scenarios/AC-15.json) | INV-1, INV-2 | Covered by direct tests in `cya/checks/tests/test_invalidation.py` (nothing modified, explanation mandatory, non-administrator changes nothing); scenario stays symbolic |
| AC-16 | [AC-16.json](scenarios/AC-16.json) | VER-2, VER-3, VER-4 | Symbolic; unimplemented |
| AC-17 | [AC-17.json](scenarios/AC-17.json) | §8.7 (no requirement ID) | Covered by direct tests in `cya/deficiencies/tests/test_notices.py` (added verification reopens and notifies only the current assignee, not an earlier assignee, the reporter or supervisors) and `cya/deficiencies/tests/test_scope_expansion.py` (reopening with the prior deadline retained); scenario stays symbolic |
| AC-18 | [AC-18.json](scenarios/AC-18.json) | SUS-1, SUS-2, SUS-3 | Covered by direct tests in `cya/deficiencies/tests/test_suspension.py` (Known Deficiency — Check Suspended, never Satisfied, out of Needs Attention Only but in the full checklist, ended by verified closure and by every SUS-4 path, as-of replay) and `cya/attention/tests/test_checklist.py`; scenario stays symbolic |
| AC-19 | [AC-19.json](scenarios/AC-19.json) | OPS-1, OPS-2 | Symbolic; unimplemented |
| AC-20 | [AC-20.json](scenarios/AC-20.json) | OPS-5; §5.9 | Symbolic; unimplemented |
| AC-21 | [AC-21.json](scenarios/AC-21.json) | ODO-1, ODO-2, ODO-3 | Part A covered by direct tests in `cya/odometer/tests` (one obligation per inspection satisfied once by any crew member, lower and over-threshold readings need confirmation, engine hours optional); correction and invalidation (Part B) pending; scenario stays symbolic |
| AC-22 | [AC-22.json](scenarios/AC-22.json) | AUTH-1, AUTH-2, AUTH-3 | Bound and run (`cya/accounts/scenario_steps.py`) |
| AC-23 | [AC-23.json](scenarios/AC-23.json) | CND-2, CND-3 | Symbolic; unimplemented |
| AC-24 | [AC-24.json](scenarios/AC-24.json) | §3.2, §3.4 (no requirement ID) | Symbolic; unimplemented |
| AC-25 | [AC-25.json](scenarios/AC-25.json) | AUD-1, AUD-3, ARC-2 | Symbolic; unimplemented |
| AC-26 | [AC-26.json](scenarios/AC-26.json) | AUD-2 | Symbolic; unimplemented |
| AC-27 | [AC-27.json](scenarios/AC-27.json) | REG-4, REG-6, CHK-3 | Symbolic; unimplemented |
| AC-28 | [AC-28.json](scenarios/AC-28.json) | ARC-8 | Symbolic; unimplemented |
| AC-29 | [AC-29.json](scenarios/AC-29.json) | UTI-1, UTI-2, UTI-3 | Direct tests in `cya/checks/tests/test_obligation_links.py` and `cya/obligations/tests/test_state.py`: reason, attribution, self-completion flag, direct and whole-chain invalidation, Unable-to-Passed/Failed supersession, as-of history, replay, rollback, effect bounds, shared obligations and concurrent completion/invalidation; scenario remains symbolic |
| AC-30 | [AC-30.json](scenarios/AC-30.json) | SCH-5, SCH-6, INV-2 | Symbolic; unimplemented |
| AC-31 | [AC-31.json](scenarios/AC-31.json) | REC-4, CON-3 | Covered by direct tests in `cya/checks/tests/test_conflicts.py` (lost response, replay after warning, a check between warning and acknowledgement); scenario stays symbolic |
| AC-32 | [AC-32.json](scenarios/AC-32.json) | INV-2a, COR-2 | Covered by direct tests in `cya/checks/tests/test_invalidation.py` (whole chain excluded, no earlier outcome revives, later correction refused; INV-7 reopening in `cya/deficiencies/tests/test_effects.py`); scenario stays symbolic |
| AC-33 | [AC-33.json](scenarios/AC-33.json) | SEAL-4, STK-B, PRN-1 | Symbolic; unimplemented |
| AC-34 | [AC-34.json](scenarios/AC-34.json) | SEAL-2, SEAL-5, SEAL-6, STK-D | Symbolic; unimplemented |
| AC-35 | [AC-35.json](scenarios/AC-35.json) | SEAL-7, SEAL-8 | Symbolic; unimplemented |

## Supplemental coverage

- 24 adversarial or end-to-end `scenarios/EX-*.json` files: multi-user races, clock boundaries and DST, invalidation/correction, authorization, malformed operation IDs, restore/replay. Three are bound and run: [EX-role-change-end-to-end](scenarios/EX-role-change-end-to-end.json) ([issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25): sign-in stub, role change through the web interface, denial, replay) and [EX-m2-obligation-engine](scenarios/EX-m2-obligation-engine.json) ([issue #61](https://github.com/JustApu/CheckYourAmbulance-archive/issues/61): seven-day fleet generation, publication, outage, carry-over and historical replay). Also bound is [EX-m3-inspection-deficiency](scenarios/EX-m3-inspection-deficiency.json) ([issue #88](https://github.com/JustApu/CheckYourAmbulance-archive/issues/88): concurrent checks, deficiency effects, session lifecycle, odometer, kit instances, local live delivery and historical replay; see [M3 verification scope](../docs/inspection-integration.md)). The other EX scenarios remain pending contracts.
- [Permission matrix](contracts/permission-matrix.json) and [expanded role-action cases](contracts/permission-cases.json): 39 rows × four roles = 156 cells, with explicit allow/deny/conditional variants (182 individual condition variants); the [issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) administrator-only invalidation policy is supplemental to the approved matrix; PERM-36 and PERM-37 are progress notes and correction (SPEC amendments 1.17.0 and 1.18.0), PERM-38 is empty-scope closure (owner-approved SPEC amendment 1.19.0), and PERM-39 is ad hoc inspection (owner-approved SPEC amendment 1.21.0, [issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158)); R2 actions are contract-only.
- AC-2 and AC-6 are exercised by direct tests while their symbolic scenarios stay unbound: `cya/inspections/tests/test_activity.py` (persisted activity, six hours of viewing only, a delayed job stamped at last activity + 6 h, idempotence, a check racing the close, database guards, reopen with the original version) and `SessionComplianceTests` in `cya/inspections/tests/test_commands.py` (a closed or auto-closed session leaves item obligations unsatisfied; [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78)).
- [Six synthetic event histories](fixtures/event-histories.json): chain invalidation, concurrent Pass/Fail, roster conflict, as-of state, out-of-service and restore/replay.

## Cannot yet be fully executed

- Python/Django and MariaDB are approved, and the offline runner executes bound scenarios against the implemented schema. Unbound semantic step IDs still require real interface bindings; skipped symbolic contracts are not runtime verification.
- No operational checklist data (OQ-01). All vehicles/items/rosters are synthetic.
- The OQ-02 and OQ-25 safe defaults are owner-approved (§24); time-window edge cases and DST rules remain provisional where indicated in §§5.3 and 5.8. Tests follow the current SPEC; changes require its amendment process.
- §3.3 export/saved-filter scope is `role-controlled` but lacks object-level permission detail (OQ-16). Tests require deny by default when no explicit policy is defined; no guessed scope.
- Audit DB permissions, atomic denied-action handling, randomized projection replay and offline stubs have dedicated executable suites. Unbound symbolic cases alone do not prove the remaining feature workflows; see the bound scenarios above and [M2 verification scope](../docs/obligation-integration.md).
