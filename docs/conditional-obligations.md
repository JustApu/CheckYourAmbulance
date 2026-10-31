# Conditional obligations and fail-closed roster integration

[issue #58](https://github.com/JustApu/CheckYourAmbulance-archive/issues/58) implements [SPEC §5.6, §15.2, and §15.3](../SPEC.md#56-conditional-obligations-c--p-req-22)
and the `conditional_rule`, `obligation_determination`, `roster_snapshot`,
`roster_member`, and `session_roster_correction` portion of
[contracts §2](data-model-contracts.md#2-table-catalogue).

## Effective duty roster and `CND-7` staleness scope

`cya.conditions.evaluator.derive_effective_roster` resolves the effective duty
roster for `(organization, vehicle_id, target_time)`:

- Scheduled rosters are stored in append-only `RosterSnapshot` and
  `RosterMember` rows (`ingest_roster_snapshot`). Snapshots may be scoped to a
  specific `vehicle_id` or fleet-wide (`vehicle_id` null/empty).
- Session actual-roster corrections (`SessionRosterCorrection`,
  `record_session_roster_correction`) apply on strict half-open intervals
  `[covers_from, covers_until)` at full microsecond precision. Disagreeing
  concurrent sessions fail closed as `CONFLICT`, record a
  `ROSTER_CONFLICT_DETECTED` audit event, and dispatch a durable supervisor
  notification intent (`pending_conflict_notifications`,
  `retry_pending_conflict_notifications`).
- For **new determinations** (`enforce_staleness=True`, `HUM-3` / `CND-3`),
  scheduled roster data older than `DEFAULT_STALENESS_LIMIT_SECONDS` (3 hours)
  relative to the server evaluation clock fails closed as `STALE`.
- For **re-evaluation of an existing `WAIVED` determination**
  (`enforce_staleness=False`, `CND-7` / `OQ-51`), elapsed clock time alone or a
  refresh that confirms the same roster does not withdraw an existing waiver.
  A waiver is reinstated only when newer evidence in the dependency window
  `[prior_shift_start, due_at]` invalidates it: a scheduled or actual-roster
  change breaking condition (a) or leaving no preceding shift ending at `D`, an
  active `UNAVAILABLE` refresh overlapping `[prior_shift_start, due_at]`
  (applied independently of whether session corrections exist), unresolved
  disagreeing session corrections, or a relied-on check that was invalidated or
  no longer qualifies for condition (b).
- **Outage recovery rule (`CND-7`):** An `UNAVAILABLE` refresh in the dependency
  window remains in force until newer scheduled `AVAILABLE` data completely
  covers the full intersection of the dependency window with the outage interval
  `[max(dep_start, u_snap.shift_start), min(due_at, u_snap.shift_end))`, as well
  as the due instant `due_at` itself whenever the outage covered `due_at`. Partial
  recoveries leave the evaluation failed-closed (`UNAVAILABLE`) until full union
  coverage is established.

## `CYA_PRIOR_CHECK_LOOKUP` collaborator contract

Condition (b) of `CARRY_OVER_FROM_PRIOR_SHIFT` and current-obligation
satisfaction checks during waiver reinstatement resolve their check repository
through an explicit `prior_check_lookup` argument or the Django setting
`settings.CYA_PRIOR_CHECK_LOOKUP` (a callable or dotted import path to a
callable; default `None` in [`cya/settings.py`](../cya/settings.py)). No
process-local registry is used so worker restarts and multi-worker deployments
evaluate identical facts identically.

### Call signature and window semantics

```python
lookup(
    organization,
    vehicle_id: str,
    item_id: str,
    window_start: datetime,
    window_end: datetime,
    *,
    obligation_id: str | None = None,
) -> dict[str, Any] | None
```

- `window_start` and `window_end` are timezone-aware UTC `datetime` bounds
  defining a half-open interval `[window_start, window_end)`.
- **Prior-shift lookup (`obligation_id=None`, condition b):** returns a
  matching check for `(organization, vehicle_id, item_id)` recorded in
  `[shift_start, due_at)`. When multiple checks exist in the window, a valid
  non-invalidated passing check takes precedence, ordered by latest
  `checked_at`.
- **Current-obligation satisfaction lookup (`obligation_id="<id>"`, `CND-7`
  reinstatement):** returns the earliest qualifying check explicitly linked to
  `obligation_id` (`SCH-5` / `SCH-6` / `SCH-7`), including early on-time checks
  in `[E, D)` (`SCH-1`) and late completions (`SCH-2`). When `early_at` (`E`) is
  configured and provided, `window_start` is bounded by `early_at`; when omitted,
  no artificial 12-hour truncation is applied (defaulting to epoch UTC), which is
  safe because an explicit `obligation_id` link is strictly required. Unlinked
  checks (`obligation_id=None`) and checks linked to another obligation without an
  explicit `shared_rule_id` (`SCH-5`) never satisfy the obligation. Collaborators
  must support the `obligation_id` keyword argument; omitting it raises
  `PriorCheckLookupUnavailable` and rolls back.

### Returned dictionary keys

| Key | Type | Notes |
|---|---|---|
| `id` | `str` | Required check identifier |
| `passed` | `bool` | Required `True` for condition (b) carry-over |
| `outcome` | `str` (optional) | `"PASSED"`, `"FAILED"`, `"NOT_APPLICABLE"`, or `"UNABLE_TO_INSPECT"` (`SPEC §5.4`) |
| `invalidated` | `bool` (optional) | `True` if excluded by administrator invalidation (`INV-2`) |
| `suspended` | `bool` (optional) | `True` if under supervisor check suspension (`§8.9`) |
| `unable_to_inspect` | `bool` (optional) | `True` if Unable to Inspect (`§5.4.1`; requires `SUPERVISOR_COMPLETED` determination) |
| `na_rule_id` | `str \| None` (optional) | Administrator NA rule reference required when `outcome == "NOT_APPLICABLE"` |
| `checked_at` | `datetime \| str` | Server-recorded check timestamp (UTC) |
| `obligation_id` | `str \| None` (optional) | Explicit `check_obligation_link` target (`SCH-5`) |
| `shared_rule_id` | `str \| None` (optional) | Explicit shared-satisfaction rule ID when shared across obligations (`SCH-5`) |
| `shared_obligation_ids` | `Sequence[str]` (optional) | Additional obligation IDs authorized by `shared_rule_id` |

### Failure semantics

- For a **new carry-over evaluation** (`re_evaluating_existing_waiver=False`),
  an unconfigured or failing collaborator fails closed by returning
  `CarryOverEvaluation(waived=False, ...)` so the inspection remains required.
- During **re-evaluation or reinstatement of an existing waiver**
  (`_reevaluate_affected_waivers` and `check_and_apply_reinstatement`), an
  unconfigured (`CYA_PRIOR_CHECK_LOOKUP = None`) or failing collaborator raises
  `PriorCheckLookupUnavailable(IntegrationNotConfigured)` so the enclosing
  command rolls back rather than fabricating a Pass or committing a false
  permanent `REINSTATED` / overdue determination.
- Offline tests inject `cya.testing.stubs.StubPriorCheckLookup` via
  `activate_stub_prior_check_lookup()` and
  `CYA_PRIOR_CHECK_LOOKUP = "cya.testing.stubs.active_stub_prior_check_lookup"`.

## Verification

Run `python manage.py test cya.conditions --noinput` in the documented
[development environment](../CONTRIBUTING.md#python-and-django).

## Database receipt-attribution insert guards ([issue #94](https://github.com/JustApu/CheckYourAmbulance-archive/issues/94))

Per SPEC §17.7 item 4 and §16.1 (AUD-1, AUD-3), database triggers protect the conditions tables on `INSERT` to guarantee that every row matches its command receipt across organization, attribution, and command type:

- `conditional_rule`: `BEFORE INSERT` trigger (`cya_conditional_rule_bi`) enforces that `NEW.organization_id`, `NEW.actor_user_id` (`NEW.actor_user_id IS NOT NULL`), and `NEW.recorded_at` strictly match the referenced `command_receipt` row, with `command_type = 'CONDITIONAL_RULE_PUBLISH'`.
- `roster_snapshot`: `BEFORE INSERT` trigger (`cya_roster_snapshot_bi`) enforces that `NEW.organization_id`, `NEW.actor_user_id` (`<=>`), `NEW.service_identity` (`<=>`), `NEW.job_key` (`<=>`), and `NEW.recorded_at` strictly match the referenced `command_receipt` row, with `command_type IN ('INGEST_ROSTER_SNAPSHOT', 'INGEST_ROSTER_UNAVAILABLE', 'RECORD_SESSION_ROSTER_CORRECTION')`.
- `session_roster_correction`: `BEFORE INSERT` trigger (`cya_session_roster_correction_bi`) enforces that `NEW.organization_id`, `NEW.actor_user_id` (`NEW.actor_user_id IS NOT NULL`), and `NEW.recorded_at` strictly match the referenced `command_receipt` row, with `command_type = 'RECORD_SESSION_ROSTER_CORRECTION'`.
- `obligation_determination`: `BEFORE INSERT` trigger (`cya_obligation_determination_bi`) enforces that `NEW.organization_id`, `NEW.actor_user_id` (`<=>`), `NEW.service_identity` (`<=>`), `NEW.job_key` (`<=>`), and `NEW.recorded_at` strictly match the referenced `command_receipt` row, with `command_type IN ('RECORD_OBLIGATION_DETERMINATION', 'INGEST_ROSTER_SNAPSHOT', 'INGEST_ROSTER_UNAVAILABLE', 'RECORD_SESSION_ROSTER_CORRECTION', 'OBLIGATIONS_RECONCILE')`.

Any attempted insert with mismatched organization, actor, worker/human attribution, timestamp, or unauthorized command type is refused with `SIGNAL SQLSTATE '45000'`.

## `SUPERSEDED` determinations

Replacing or retiring a pre-window obligation when a checklist or override version is published (SPEC §5.7)
appends a `SUPERSEDED` determination for the old obligation, chained to its latest determination. The new
obligation starts with no determinations of its own. See [obligation generation](obligation-generation.md).
