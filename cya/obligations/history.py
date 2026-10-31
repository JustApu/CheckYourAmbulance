"""Immutable check/link and deficiency evidence for database-free historical replay."""

from dataclasses import dataclass
from datetime import datetime

from cya.checks.state import CheckSnapshot, effective_checks
from cya.obligations.state import NO_KIT, CheckEvidence, aware


@dataclass(frozen=True)
class RecordedCheck:
    snapshot: CheckSnapshot
    item_id: str
    kit_key: str
    author_user_id: str | None


@dataclass(frozen=True)
class RecordedLink:
    check_id: str
    obligation_id: str
    recorded_at: datetime


@dataclass(frozen=True)
class RecordedSupport:
    check_id: str
    determination_id: str
    recorded_at: datetime


@dataclass(frozen=True)
class RecordedDeficiency:
    id: str
    recorded_at: datetime
    item_ids: frozenset[str]
    kit_key: str = NO_KIT


@dataclass(frozen=True)
class RecordedDeficiencyEvent:
    id: str
    deficiency_id: str
    sequence: int
    predecessor_id: str | None
    recorded_at: datetime
    from_status: str | None
    to_status: str


def deficiency_states(headers, events, instant):
    """Replay complete lifecycle chains; refuse gaps/forks rather than invent a status."""
    from cya.attention.state import DeficiencyFact
    from cya.deficiencies.transitions import STATUSES, validate_transition

    aware(instant)
    visible = {h.id: h for h in headers if aware(h.recorded_at) <= instant}
    grouped = {}
    for event in events:
        if aware(event.recorded_at) <= instant:
            if event.deficiency_id not in visible:
                raise ValueError("deficiency event has no visible header")
            grouped.setdefault(event.deficiency_id, []).append(event)
    result = []
    for pk, header in sorted(visible.items()):
        chain = sorted(grouped.get(pk, ()), key=lambda e: (e.sequence, e.id))
        if not chain:
            raise ValueError("deficiency has no lifecycle event")
        previous = None
        for sequence, event in enumerate(chain, 1):
            if (
                event.sequence != sequence
                or event.predecessor_id != (previous.id if previous else None)
                or event.from_status != (previous.to_status if previous else None)
                or event.to_status not in STATUSES
                or (previous and event.recorded_at < previous.recorded_at)
                or event.recorded_at < header.recorded_at
            ):
                raise ValueError("malformed deficiency lifecycle chain")
            if event.from_status != event.to_status:
                validate_transition(event.from_status, event.to_status)
            previous = event
        result.append(DeficiencyFact(pk, previous.to_status, header.item_ids, header.kit_key))
    return tuple(result)


def replay_checks(history, instant):
    """Return linked satisfaction evidence and all effective checks for attention at T.

    Satisfaction retains the original check instant; attention uses the effective row's
    instant. Link and supporting-determination visibility are independently time scoped.
    """
    from cya.attention.state import CheckFact

    rows = {r.snapshot.id: r for r in history.check_records}
    effective = effective_checks(
        (r.snapshot for r in history.check_records), history.check_decisions, as_of=instant
    )
    tips = {e.check_id: e for e in effective if not e.invalidated}
    facts = tuple(
        CheckFact(pk, rows[pk].item_id, rows[pk].kit_key, rows[pk].snapshot.recorded_at, e.outcome)
        for pk, e in tips.items()
    )
    determinations = {d.id: d for d in history.determinations if aware(d.recorded_at) <= instant}
    supports = {}
    for support in sorted(
        history.check_supports, key=lambda s: (s.recorded_at, s.determination_id)
    ):
        if aware(support.recorded_at) <= instant and support.determination_id in determinations:
            d = determinations[support.determination_id]
            supports[(support.check_id, d.obligation_id)] = d.id
    obligations = {o.id: o for o in history.obligations}
    checks = []
    for link in history.check_links:
        if aware(link.recorded_at) > instant or link.check_id not in tips:
            continue
        tip = tips[link.check_id]
        row, root = rows[link.check_id], rows[tip.root_id]
        obligation = obligations.get(link.obligation_id)
        if obligation is None:
            raise ValueError("check link has no exported obligation")
        if (row.item_id, row.kit_key) != (obligation.item_id, obligation.kit_instance_id or NO_KIT):
            raise ValueError("check link item or kit mismatch")
        checks.append(
            CheckEvidence(
                link.check_id,
                history.organization_id,
                obligation.vehicle_id,
                row.item_id,
                obligation.id,
                root.snapshot.recorded_at,
                tip.outcome,
                late_eligible=root.snapshot.recorded_at > obligation.grace_at,
                supervisor_determination_id=supports.get((link.check_id, obligation.id)),
                author_user_id=root.author_user_id,
            )
        )
    return tuple(checks), facts
