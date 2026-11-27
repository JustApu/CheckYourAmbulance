"""Pure obligation projections (SPEC 5.3, 5.4 and 19). No ORM or current clock.

Stored checks, links, lifecycle events and supervisor check suspensions are replayed at the
requested instant. Optional check and suspension adapters remain available for explicit evidence
inputs. No writes occur.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from cya.authorization.policy import unable_self_completion_allowed

if TYPE_CHECKING:
    from cya.attention.state import Attention, DeficiencyFact
    from cya.checks.state import CheckDecision
    from cya.obligations.history import (
        RecordedCheck,
        RecordedDeficiency,
        RecordedDeficiencyEvent,
        RecordedLink,
        RecordedSupport,
    )

# An obligation's anchor is (vehicle, item, kit instance, due instant). An item outside a kit has
# no instance; it is compared as this fixed value rather than as NULL, because a missing kit
# instance must be a value (SPEC 18.1: two obligations cannot differ only by a missing instance).
NO_KIT = "00000000-0000-0000-0000-000000000000"


def aware(value, field="instant"):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


@dataclass(frozen=True)
class ObligationEvidence:
    id: str
    organization_id: str
    vehicle_id: str
    item_id: str
    requirement_version_id: str
    publication_id: str
    schedule_rule_id: str | None
    conditional_rule_id: str | None
    recorded_at: datetime
    early_at: datetime
    due_at: datetime
    grace_at: datetime
    routine: bool = True
    kit_instance_id: str | None = None  # SPEC 1.8.0: one obligation per kit instance

    explanation: str | None = None
    actor_user_id: str | None = None
    command_id: str | None = None
    # L = G + the w_late pinned at creation (SCH-2, issue #80). None only for evidence built
    # without it; anything that needs L refuses such evidence rather than assuming a window.
    late_limit_at: datetime | None = None
    # The obligation's stored stream key: its kit, NO_KIT, or a kit presence line's unit key
    # (SPEC 2.0.0 KDEF-12). None for evidence built without it, which falls back to the kit.
    kit_key: str | None = None
    # SPEC 6.0.0 18.3 item 4: for an obligation created before the item-requirement cutover
    # (kit_key NO_KIT), the item requirement its pinned row maps to (immutable map row), so a
    # replayed transition link is checked against the exact requirement the check names.
    mapped_requirement_id: str | None = None

    @property
    def unit_key(self):
        """The key that groups this obligation with its checks, deficiencies and attention."""
        return self.kit_key or self.kit_instance_id or NO_KIT

    def __post_init__(self):
        for field in ("recorded_at", "early_at", "due_at", "grace_at"):
            aware(getattr(self, field), field)
        if not self.early_at <= self.due_at <= self.grace_at:
            raise ValueError("obligation window must satisfy E <= D <= G")
        if self.late_limit_at is not None:
            aware(self.late_limit_at, "late_limit_at")
            if self.late_limit_at < self.grace_at:
                raise ValueError("obligation late limit must not precede its grace deadline")

    def late_limit(self):
        """The pinned late limit L; refuses evidence that does not carry it."""
        if self.late_limit_at is None:
            raise ValueError("obligation evidence has no pinned late limit; rebuild required")
        return self.late_limit_at


@dataclass(frozen=True)
class RuleEvidence:
    id: str
    recorded_at: datetime
    effective_at: datetime

    def applies(self, instant, due_at):
        aware(self.recorded_at)
        aware(self.effective_at)
        return self.recorded_at <= instant and self.effective_at <= min(instant, due_at)


@dataclass(frozen=True)
class DeterminationEvidence:
    id: str
    obligation_id: str
    recorded_at: datetime
    determination_type: str
    prior_determination_id: str | None = None
    rule: RuleEvidence | None = None
    actor_user_id: str | None = None


@dataclass(frozen=True)
class CheckEvidence:
    """A linked check stub, never an unlinked participation check.

    voided_at is the earliest correction/invalidation instant excluding this row.
    late_eligible attests that the link satisfied SCH-2's competing-window rule.
    supervisor_determination_id attests a determination_check link for an Unable check.
    """

    id: str
    organization_id: str
    vehicle_id: str
    item_id: str
    obligation_id: str
    recorded_at: datetime
    outcome: str
    voided_at: datetime | None = None
    na_rule: RuleEvidence | None = None
    late_eligible: bool = False
    supervisor_determination_id: str | None = None
    author_user_id: str | None = None
    # SPEC 8.0.0 5.3, LATE-5: a paper check's original performance instant. It is the check's
    # qualifying time for windows, completion time and lateness; recorded_at (the server entry
    # instant) still decides whether the check is known at an instant (19.3, 19.6).
    performed_at: datetime | None = None

    @property
    def qualifying_at(self):
        return self.performed_at or self.recorded_at


@dataclass(frozen=True)
class SealSubstitutionEvidence:
    """An attributed substitution explicitly linked to one obligation.

    Links must be created only after the seal command validates its entire
    covered scope. They identify individual obligations without creating checks.
    """

    id: str
    organization_id: str
    obligation_id: str
    recorded_at: datetime
    late_eligible: bool = False


@dataclass(frozen=True)
class SealInspectionEvidence:
    """A fresh inspected-item result linked to one obligation."""

    id: str
    organization_id: str
    obligation_id: str
    recorded_at: datetime
    outcome: str
    late_eligible: bool = False


@dataclass(frozen=True)
class StatusEvidence:
    id: str
    vehicle_id: str
    sequence: int
    recorded_at: datetime
    status: str


@dataclass(frozen=True)
class ActivityEvidence:
    """When a vehicle became active or inactive (vehicle_activity_event, SPEC 4.8 CFG-1)."""

    id: str
    vehicle_id: str
    sequence: int
    recorded_at: datetime
    active: bool


@dataclass(frozen=True)
class SuspensionEvidence:
    """A supervisor check suspension (SPEC 8.9) covering one routine obligation.

    recorded_at is when it started and ended_at when the closing event ended it (None while in
    force). suspension_id and deficiency_id name the stored START and its deficiency when known.
    """

    obligation_id: str
    recorded_at: datetime
    ended_at: datetime | None = None
    suspension_id: str | None = None
    deficiency_id: str | None = None


@dataclass(frozen=True)
class SelectionEvidence:
    vehicle_id: str
    item_id: str
    due_at: datetime
    sequence: int
    recorded_at: datetime
    selected_obligation_id: str | None
    kit_key: str = NO_KIT


class State(StrEnum):
    UPCOMING = "UPCOMING"
    CURRENT = "CURRENT"
    DUE = "DUE"
    OVERDUE = "OVERDUE"
    SATISFIED = "SATISFIED"
    EXEMPT = "EXEMPT"
    WAIVED = "WAIVED"
    SUSPENDED = "SUSPENDED"


@dataclass(frozen=True)
class ObligationState:
    obligation: ObligationEvidence
    state: State
    evidence_id: str | None = None
    outcome: str | None = None
    completed_at: datetime | None = None
    late_seconds: float = 0
    self_completed: bool = False
    attention: Attention | None = None
    deficiencies: tuple[DeficiencyFact, ...] = ()
    # SUSPENDED only: when the suspension ended, if it ended by T (the obligation's window closed
    # while it was in force); None while it is still in force at T.
    suspension_ended_at: datetime | None = None

    @property
    def suspension_in_force(self):
        return self.state == State.SUSPENDED and self.suspension_ended_at is None

    @property
    def display_text(self):
        if self.outcome == "SUPERVISOR_COMPLETED":
            return "Completed by supervisor — unable to inspect"
        if self.state == State.SUSPENDED:
            return "Known Problem — Check Suspended"  # SPEC SUS-1, AC-18
        return self.outcome or self.state.value


def _checks_for(obligation, checks, instant):
    result = []
    for check in checks:
        aware(check.recorded_at)
        if check.performed_at is not None:
            aware(check.performed_at)
        if check.voided_at is not None:
            aware(check.voided_at)
        qualifying = check.qualifying_at
        if (
            (check.organization_id, check.vehicle_id, check.item_id, check.obligation_id)
            != (
                obligation.organization_id,
                obligation.vehicle_id,
                obligation.item_id,
                obligation.id,
            )
            or check.recorded_at > instant
            or not obligation.early_at <= qualifying
            or (check.voided_at is not None and check.voided_at <= instant)
            or (qualifying > obligation.grace_at and check.late_eligible is not True)
        ):
            continue
        result.append(check)
    # The earliest qualifying check satisfies (SCH-7), by qualifying time (19.4, 19.6).
    return sorted(result, key=lambda c: (c.qualifying_at, c.recorded_at, c.id))


def _determination_for(obligation, determinations, instant):
    known = []
    for determination in determinations:
        aware(determination.recorded_at)
        if determination.obligation_id == obligation.id and determination.recorded_at <= instant:
            known.append(determination)
    # Only visible children supersede a prior determination (including INVALIDATED).
    superseded = {d.prior_determination_id for d in known}
    leaves = [d for d in known if d.id not in superseded]
    if len(leaves) > 1:
        raise ValueError("ambiguous determination history")
    return leaves[0] if leaves else None


def _status_for(obligation, status_events, instant):
    status_time = min(obligation.due_at, instant)
    statuses = []
    for event in status_events:
        aware(event.recorded_at)
        if event.vehicle_id == obligation.vehicle_id and event.recorded_at <= status_time:
            statuses.append(event)
    return max(statuses, key=lambda e: e.sequence, default=None)


def _activity_for(obligation, activity_events, instant):
    """The vehicle's activity event in force at min(D, T), or None (no history: as before).

    Like an out-of-service status (SPEC 5.9), it is judged at the due instant, so reactivation
    never backfills an obligation due while the vehicle was inactive.
    """
    activity_time = min(obligation.due_at, instant)
    events = []
    for event in activity_events:
        aware(event.recorded_at)
        if event.vehicle_id == obligation.vehicle_id and event.recorded_at <= activity_time:
            events.append(event)
    return max(events, key=lambda e: e.sequence, default=None)


def _suspension_for(obligation, suspensions, instant):
    """The suspension in force for a routine obligation at min(T, G), or None (SPEC 8.9).

    Only routine checks are suspended (SUS-1): an explicit ad hoc order is not. The instant is
    capped at the grace deadline, like exemption at D: an obligation whose window closed while
    suspended stays Suspended after the suspension ends (never Overdue, never Satisfied), and a
    suspension that starts after G cannot erase an inspection already missed. One that ends while
    the window is still open makes the check required again. Ties break on the earliest start.
    The caller lets a qualifying check recorded before that start win.
    """
    basis = min(instant, obligation.grace_at)
    found = None
    for suspension in suspensions:
        aware(suspension.recorded_at)
        if suspension.ended_at is not None:
            aware(suspension.ended_at)
        if (
            obligation.routine
            and suspension.obligation_id == obligation.id
            and suspension.recorded_at <= basis
            and (suspension.ended_at is None or basis < suspension.ended_at)
            and (
                found is None
                or (suspension.recorded_at, suspension.suspension_id or "")
                < (found.recorded_at, found.suspension_id or "")
            )
        ):
            found = suspension
    return found


def _satisfaction(obligation, linked, determination):
    for check in linked:
        qualifies = check.outcome in ("PASSED", "FAILED")
        if check.outcome == "NOT_APPLICABLE":
            qualifies = check.na_rule is not None and check.na_rule.applies(
                check.qualifying_at, check.qualifying_at
            )
        if qualifies:
            return ObligationState(
                obligation,
                State.SATISFIED,
                check.id,
                check.outcome,
                check.qualifying_at,
                max(0, (check.qualifying_at - obligation.grace_at).total_seconds()),
            )
    if determination is not None and determination.determination_type == "SUPERVISOR_COMPLETED":
        if any(
            c.outcome == "UNABLE_TO_INSPECT"
            and c.supervisor_determination_id == determination.id
            and c.recorded_at <= determination.recorded_at
            for c in linked
        ):
            return ObligationState(
                obligation,
                State.SATISFIED,
                determination.id,
                "SUPERVISOR_COMPLETED",
                determination.recorded_at,
                max(0, (determination.recorded_at - obligation.grace_at).total_seconds()),
                any(
                    c.supervisor_determination_id == determination.id
                    and unable_self_completion_allowed(
                        determination.actor_user_id, c.author_user_id
                    )
                    for c in linked
                ),
            )
    return None


def evaluate_obligation(
    obligation,
    instant,
    *,
    determinations=(),
    checks=(),
    status_events=(),
    suspensions=(),
    seal_substitutions=(),
    seal_inspections=(),
    activity_events=(),
):
    """Derive state from evidence known by T, without querying or reading the clock.

    Exemption uses min(D,T); later return to service never backfills an outage at D.
    Distinct exemptions, waivers and suspensions take display precedence over checks.
    """
    aware(instant)
    if obligation.recorded_at > instant:
        raise ValueError("obligation did not exist at instant")
    if (
        not determinations
        and not checks
        and not status_events
        and not suspensions
        and not seal_substitutions
        and not seal_inspections
        and not activity_events
    ):
        if instant < obligation.early_at:
            state = State.UPCOMING
        elif instant < obligation.due_at:
            state = State.CURRENT
        elif instant <= obligation.grace_at:
            state = State.DUE
        else:
            state = State.OVERDUE
        return ObligationState(obligation, state)
    status = _status_for(obligation, status_events, instant)
    if obligation.routine and status is not None and status.status == "OUT_OF_SERVICE":
        return ObligationState(obligation, State.EXEMPT, status.id)
    # SPEC 4.8 CFG-1: an inactive vehicle owes no routine obligation; same precedence as 5.9.
    activity = _activity_for(obligation, activity_events, instant)
    if obligation.routine and activity is not None and not activity.active:
        return ObligationState(obligation, State.EXEMPT, activity.id)
    determination = _determination_for(obligation, determinations, instant)
    if (
        obligation.routine
        and determination is not None
        and determination.determination_type == "WAIVED"
    ):
        if determination.rule is not None and determination.rule.applies(
            instant, obligation.due_at
        ):
            return ObligationState(obligation, State.WAIVED, determination.id)
    satisfaction = _satisfaction(
        obligation, _checks_for(obligation, checks, instant), determination
    )
    substitutions = []
    for evidence in seal_substitutions:
        aware(evidence.recorded_at)
        if (
            evidence.organization_id == obligation.organization_id
            and evidence.obligation_id == obligation.id
            and obligation.early_at <= evidence.recorded_at <= instant
            and (
                evidence.recorded_at <= obligation.grace_at
                or (
                    evidence.late_eligible is True
                    and obligation.late_limit_at is not None
                    and evidence.recorded_at <= obligation.late_limit_at
                )
            )
        ):
            substitutions.append(evidence)
    if substitutions:
        first = min(substitutions, key=lambda e: (e.recorded_at, e.id))
        if satisfaction is None or first.recorded_at < satisfaction.completed_at:
            satisfaction = ObligationState(
                obligation,
                State.SATISFIED,
                first.id,
                "SEAL_SUBSTITUTION",
                first.recorded_at,
                max(0, (first.recorded_at - obligation.grace_at).total_seconds()),
            )
    inspections = []
    for evidence in seal_inspections:
        aware(evidence.recorded_at)
        if (
            evidence.organization_id == obligation.organization_id
            and evidence.obligation_id == obligation.id
            and evidence.outcome in ("PASSED", "FAILED")
            and obligation.early_at <= evidence.recorded_at <= instant
            and (
                evidence.recorded_at <= obligation.grace_at
                or (
                    evidence.late_eligible is True
                    and obligation.late_limit_at is not None
                    and evidence.recorded_at <= obligation.late_limit_at
                )
            )
        ):
            inspections.append(evidence)
    if inspections:
        first = min(inspections, key=lambda e: (e.recorded_at, e.id))
        if satisfaction is None or first.recorded_at < satisfaction.completed_at:
            satisfaction = ObligationState(
                obligation,
                State.SATISFIED,
                first.id,
                first.outcome,
                first.recorded_at,
                max(0, (first.recorded_at - obligation.grace_at).total_seconds()),
            )
    suspension = _suspension_for(obligation, suspensions, instant)
    # A suspension never revokes an obligation already satisfied when it started (owner decision):
    # it covers only an obligation still unsatisfied at its start.
    if suspension is not None and (
        satisfaction is None or satisfaction.completed_at >= suspension.recorded_at
    ):
        ended = suspension.ended_at
        return ObligationState(
            obligation,
            State.SUSPENDED,
            suspension.suspension_id,
            suspension_ended_at=ended if ended is not None and ended <= instant else None,
        )
    if satisfaction is not None:
        return satisfaction
    if instant < obligation.early_at:
        state = State.UPCOMING
    elif instant < obligation.due_at:
        state = State.CURRENT
    elif instant <= obligation.grace_at:
        state = State.DUE
    else:
        state = State.OVERDUE
    return ObligationState(obligation, state)


@dataclass(frozen=True)
class ProjectionHistory:
    """An organization-scoped immutable export; replay does not query current configuration."""

    organization_id: str
    obligations: tuple[ObligationEvidence, ...] = ()
    selections: tuple[SelectionEvidence, ...] = ()
    determinations: tuple[DeterminationEvidence, ...] = ()
    statuses: tuple[StatusEvidence, ...] = ()
    checks: tuple[CheckEvidence, ...] = ()
    suspensions: tuple[SuspensionEvidence, ...] = ()
    check_records: tuple[RecordedCheck, ...] = ()
    check_decisions: tuple[CheckDecision, ...] = ()
    check_links: tuple[RecordedLink, ...] = ()
    check_supports: tuple[RecordedSupport, ...] = ()
    deficiency_headers: tuple[RecordedDeficiency, ...] = ()
    deficiency_events: tuple[RecordedDeficiencyEvent, ...] = ()
    use_stored_checks: bool = True
    seal_substitutions: tuple[SealSubstitutionEvidence, ...] = ()
    seal_inspections: tuple[SealInspectionEvidence, ...] = ()
    activities: tuple[ActivityEvidence, ...] = ()


def rebuild_vehicle_projection(
    history, vehicle_id, instant, start, end, *, include_all_ad_hoc=False
):
    """Rebuild from immutable events in any input order, bounded to due instants [start,end]."""
    aware(instant)
    aware(start, "start")
    aware(end, "end")
    if start > end:
        raise ValueError("start must not exceed end")
    from cya.attention.state import applies, attention_states
    from cya.obligations.history import deficiency_states, replay_checks

    linked, check_facts = replay_checks(history, instant)
    deficiencies = deficiency_states(history.deficiency_headers, history.deficiency_events, instant)
    keys = {(o.item_id, o.unit_key) for o in history.obligations}
    attention = attention_states(keys, check_facts, deficiencies)
    tails = {}
    for event in history.selections:
        aware(event.recorded_at)
        aware(event.due_at)
        if event.vehicle_id != vehicle_id or event.recorded_at > instant:
            continue
        if not start <= event.due_at <= end:
            continue
        anchor = (event.item_id, event.kit_key, event.due_at)
        if anchor not in tails or event.sequence > tails[anchor].sequence:
            tails[anchor] = event
    obligations = {o.id: o for o in history.obligations}
    checks_by_obligation = {}
    for check in (*history.checks, *(linked if history.use_stored_checks else ())):
        checks_by_obligation.setdefault(check.obligation_id, []).append(check)
    determinations_by_obligation = {}
    for determination in history.determinations:
        determinations_by_obligation.setdefault(determination.obligation_id, []).append(
            determination
        )
    suspensions_by_obligation = {}
    for suspension in history.suspensions:
        suspensions_by_obligation.setdefault(suspension.obligation_id, []).append(suspension)
    seals_by_obligation = {}
    for substitution in history.seal_substitutions:
        seals_by_obligation.setdefault(substitution.obligation_id, []).append(substitution)
    inspections_by_obligation = {}
    for inspection in history.seal_inspections:
        inspections_by_obligation.setdefault(inspection.obligation_id, []).append(inspection)
    result = []
    for event in tails.values():
        if event.selected_obligation_id is None:
            continue
        obligation = obligations[event.selected_obligation_id]
        if not obligation.routine:
            raise ValueError("non-routine obligation cannot have a selection event")
        if obligation.organization_id != history.organization_id or (
            obligation.vehicle_id,
            obligation.item_id,
            obligation.unit_key,
            obligation.due_at,
        ) != (vehicle_id, event.item_id, event.kit_key, event.due_at):
            raise ValueError("selection anchor or organization mismatch")
        result.append(
            evaluate_obligation(
                obligation,
                instant,
                determinations=determinations_by_obligation.get(obligation.id, ()),
                checks=checks_by_obligation.get(obligation.id, ()),
                status_events=history.statuses,
                activity_events=history.activities,
                suspensions=suspensions_by_obligation.get(obligation.id, ()),
                seal_substitutions=seals_by_obligation.get(obligation.id, ()),
                seal_inspections=inspections_by_obligation.get(obligation.id, ()),
            )
        )
    # Explicit human orders are immutable and never participate in routine selection.
    for obligation in history.obligations:
        if (
            not obligation.routine
            and obligation.organization_id == history.organization_id
            and obligation.vehicle_id == vehicle_id
            and obligation.recorded_at <= instant
            and (include_all_ad_hoc or start <= obligation.due_at <= end)
        ):
            result.append(
                evaluate_obligation(
                    obligation,
                    instant,
                    determinations=determinations_by_obligation.get(obligation.id, ()),
                    checks=checks_by_obligation.get(obligation.id, ()),
                    status_events=history.statuses,
                    activity_events=history.activities,
                    suspensions=suspensions_by_obligation.get(obligation.id, ()),
                    seal_substitutions=seals_by_obligation.get(obligation.id, ()),
                    seal_inspections=inspections_by_obligation.get(obligation.id, ()),
                )
            )
    # Each item's deficiencies, in their original order: checking every deficiency against every
    # obligation grew with the vehicle's whole history (issue #63).
    by_item = {}
    for d in deficiencies:
        for item_id in d.all_item_ids:
            by_item.setdefault(item_id, []).append(d)
    result = [
        replace(
            r,
            attention=attention[(r.obligation.item_id, r.obligation.unit_key)],
            deficiencies=tuple(
                d
                for d in by_item.get(r.obligation.item_id, ())
                if applies(d, r.obligation.item_id, r.obligation.unit_key)
            ),
        )
        for r in result
    ]
    return tuple(
        sorted(
            result,
            key=lambda r: (
                r.obligation.due_at,
                r.obligation.item_id,
                r.obligation.unit_key,
                r.obligation.id,
            ),
        )
    )
