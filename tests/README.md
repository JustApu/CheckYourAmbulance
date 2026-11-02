# Stack-neutral acceptance scenarios

[issue #17](https://github.com/JustApu/CheckYourAmbulance-archive/issues/17); updated for SPEC 1.2.0 by [issue #36](https://github.com/JustApu/CheckYourAmbulance-archive/issues/36). `scenarios/AC-01.json`–`AC-35.json` map one-to-one to SPEC §23; `EX-*.json` add adversarial cases. `fixtures/demo-fleet.json` ([issue #55](https://github.com/JustApu/CheckYourAmbulance-archive/issues/55)) seeds the synthetic, non-authoritative four-ambulance fleet and checklist for `manage.py seed_synthetic_fleet`; `fixtures/base.json`, `fixtures/sealed-kit.json` and `fixtures/event-histories.json` contain only synthetic identities, vehicles, items, kits, rosters and ordered event streams. AC-11 now states the R1 contract for the deferred Bulk Pass (§7.6): no bulk action and no non-null `bulk_group_id`.

## Format

Some criteria rely on prose with no requirement ID: use `spec_refs` (e.g., `§8.7`) and allow an empty `requirements` array when `spec_refs` is nonempty. Never cite an unrelated requirement merely to fill the list.

Every scenario is a JSON object with `schema_version: 1`, `id`, `criterion`, `requirements`, `fixture`, `synthetic: true`, ordered `given`, `when`, `then` and `status` (`contract`, or `bound` once the runner executes it). Narrative fields explain intended behavior; `contracts/step-catalog.json` provides stable, **symbolic** setup/action/assertion IDs for all 35 criteria. These IDs are not implemented methods and must never be treated as a passing test. A future runner must reject unknown steps and assert every outcome; it must not silently skip steps.

`contracts/permission-matrix.json` transcribes the SPEC §3.3 rows, four roles each; the two sealed-kit rows added by SPEC 1.2.0 are appended as PERM-27 and PERM-28 rather than renumbering existing rows. Session rows PERM-29 to PERM-31 were added by SPEC amendment 1.9.0 for [issue #78](https://github.com/JustApu/CheckYourAmbulance-archive/issues/78), kit-assignment row PERM-32 by SPEC amendment 1.10.0, deficiency rows PERM-33 and PERM-34 by SPEC amendment 1.12.0, administrator-only invalidation row PERM-35 by [issue #82](https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) Part A, progress-note row PERM-36 and correction row PERM-37 by SPEC amendments 1.17.0 and 1.18.0, empty-scope closure row PERM-38 by owner-approved amendment 1.19.0 ([issue #153](https://github.com/JustApu/CheckYourAmbulance-archive/issues/153)), and ad hoc inspection row PERM-39 by owner-approved amendment 1.21.0 ([issue #158](https://github.com/JustApu/CheckYourAmbulance-archive/issues/158)). `contracts/permission-cases.json` expands all **156 role × action cells**, including conditional variants (182 individual condition variants). For `automatic`, `role_controlled` and `request_only` cells, do not reinterpret the SPEC as unconditional authorization. `role_controlled` exports have no specified per-object scope; the contract requires an explicit policy or deny-by-default, not an invented scope.

`fixtures/event-histories.json` contains six deterministic event streams for correction-chain invalidation, pass/fail race, roster conflict, as-of versioning, out-of-service exemption and restore/replay. Event streams are fixture examples, not a physical DB schema. Each scenario starts from an isolated fixture snapshot. Time uses explicit UTC instants, with `America/New_York` local conversion where required. All external identity/roster integrations are stubbed; no network or runtime `data/` is permitted.

## Validation and handoff

The requirement mappings in `contracts/step-catalog.json` are authoritative. Each `AC-*` scenario and `TRACEABILITY.md` must reproduce those mappings exactly; all `EX-*` scenarios must cite the SPEC requirements or precise section references they exercise. JSON parse and structural/coverage checks run without a chosen stack: `python3 tools/validate_scenarios.py` (in CI; see [CONTRIBUTING.md](../CONTRIBUTING.md#repository-checks)). They do not test the behavior of a future application. The Django test suite includes the runner skeleton (`cya/testing/scenarios.py`, run by `python manage.py test cya`; see [CONTRIBUTING.md](../CONTRIBUTING.md#python-and-django)). Each scenario file becomes one test. Status `contract` scenarios are reported as **skipped (pending)**, never as passed. To bind an AC scenario, a feature PR:
1. registers a function for every step of its catalog case with `cya.testing.scenarios.STEPS`, in a module imported by `cya/testing/bindings.py`;
2. changes the scenario's status to `bound` and its TRACEABILITY row.

Steps share a context dict and may add callables to `context["cleanups"]`. AC-22 is bound (`cya/accounts/scenario_steps.py`, [issue #21](https://github.com/JustApu/CheckYourAmbulance-archive/issues/21)).

A bound scenario runs its `setup`, `actions` and `assert` steps in order and fails before running anything if any step is unbound. `EX-*` scenarios have no catalog step list. One that is ready to bind lists its own ordered `steps` in its file (same registry and rules as above) and is then marked `bound`; without `steps` it stays pending. A scenario whose steps run audited commands also sets `"database": "commit"`, which runs it in a `TransactionTestCase` because those commands refuse to run inside a wrapping transaction. `EX-role-change-end-to-end` is bound this way (`cya/authorization/scenario_steps.py`, [issue #25](https://github.com/JustApu/CheckYourAmbulance-archive/issues/25)). Bindings must test positive and negative matrix branches.

Python tests load fixtures only through `cya.testing.fixtures.load_fixture()`, which requires the `"synthetic": true` marker, and reach Google Workspace and Humanity only through the offline stubs in `cya.testing.stubs`. See [traceability](TRACEABILITY.md) for remaining decisions and limitations.

`EX-m2-obligation-engine` is also bound with `"database": "commit"` ([issue #61](https://github.com/JustApu/CheckYourAmbulance-archive/issues/61)).
Its steps in `cya/testing/scenarios.py` execute a seven-day sample agency fleet
journey across the fall-back boundary using real audited obligation commands,
local roster/check stubs and exact T1/T2/T3 reconstruction. See the
[M2 verification guide](../docs/obligation-integration.md) for assertions, scope
and the extra test-only 01:30 schedule. Unrelated symbolic AC scenarios remain
pending; this scenario does not implement future inspection/check storage.

`EX-m3-inspection-deficiency` is bound with `"database": "commit"` ([issue #88](https://github.com/JustApu/CheckYourAmbulance-archive/issues/88)).
Its command-backed journey covers concurrent crew checks, retry/conflict handling,
deficiencies, corrections/invalidation, sessions, odometer and per-kit obligations,
with local live delivery and exact historical reconstruction. See the
[M3 verification guide](../docs/inspection-integration.md) for coverage and limits.

`EX-m4-deficiency-resolution` is bound with `"database": "commit"` ([issue #18](https://github.com/JustApu/CheckYourAmbulance/issues/18)).
It reuses the M3 agency and drives the M4 commands: assignment, dependency, extensions,
correction and verification (including a concurrent closing race), reopenings, check suspension,
operational status, notifications through the stub sender and the readiness dashboard, with exact
historical reconstruction. See the [M4 verification guide](../docs/resolution-integration.md).
The individual symbolic AC files retain their existing status.
