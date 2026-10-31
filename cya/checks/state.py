"""Pure effective-outcome replay for immutable check correction chains."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CheckSnapshot:
    id: str
    root_id: str
    predecessor_id: str | None
    recorded_at: datetime
    outcome: str


@dataclass(frozen=True)
class CheckDecision:
    root_id: str
    selected_id: str
    event_type: str
    recorded_at: datetime
    replacement_id: str | None = None


@dataclass(frozen=True)
class EffectiveCheck:
    root_id: str
    check_id: str
    outcome: str
    invalidated: bool
    superseded_ids: tuple[str, ...]


def effective_checks(checks, events, *, as_of: datetime):
    """Return one effective check per root using only checks/events recorded by ``as_of``.

    Input order has no effect. Malformed forks, missing parents, and decisions that do not
    match their chain fail closed instead of producing a guessed outcome.
    """
    _require_aware(as_of)
    check_rows = tuple(checks)
    event_rows = tuple(events)
    for row in check_rows:
        _require_aware(row.recorded_at)
    for event in event_rows:
        _require_aware(event.recorded_at)
    visible_rows = [row for row in check_rows if row.recorded_at <= as_of]
    visible = {row.id: row for row in visible_rows}
    if len(visible) != len(visible_rows):
        raise ValueError("duplicate check identity")
    decisions = tuple(event for event in event_rows if event.recorded_at <= as_of)
    result = []
    roots = sorted({row.root_id for row in visible.values()})
    if any(event.root_id not in roots for event in decisions):
        raise ValueError("check decision has no visible root")
    for root_id in roots:
        chain = sorted(
            (row for row in visible.values() if row.root_id == root_id),
            key=lambda row: (row.recorded_at, row.id),
        )
        by_id = {row.id: row for row in chain}
        root = by_id.get(root_id)
        if root is None or root.predecessor_id is not None:
            raise ValueError("check chain root is missing or malformed")
        children = {}
        for row in chain:
            if row.id == root_id:
                continue
            parent = by_id.get(row.predecessor_id)
            if parent is None or parent.root_id != root_id:
                raise ValueError("check chain predecessor is missing")
            if row.predecessor_id in children:
                raise ValueError("check chain forks")
            children[row.predecessor_id] = row.id
        tip_id = root_id
        consumed = set()
        invalidated = False
        pending = [event for event in decisions if event.root_id == root_id]
        last_recorded_at = root.recorded_at
        while pending:
            next_events = [
                event
                for event in pending
                if event.selected_id == tip_id and event.recorded_at >= last_recorded_at
            ]
            if not next_events:
                break
            earliest = min(event.recorded_at for event in next_events)
            next_events = [event for event in next_events if event.recorded_at == earliest]
            if len(next_events) != 1:
                raise ValueError("multiple decisions extend the same check")
            event = next_events[0]
            pending.remove(event)
            if event.event_type == "INVALIDATION":
                if event.selected_id != tip_id or event.replacement_id is not None:
                    raise ValueError("invalidation does not match the effective check")
                invalidated = True
            elif event.event_type == "CORRECTION":
                if invalidated:
                    raise ValueError("an invalidated check chain cannot be corrected")
                if event.selected_id not in by_id or event.replacement_id not in by_id:
                    raise ValueError("correction replacement is missing")
                replacement = by_id[event.replacement_id]
                if event.selected_id != tip_id or replacement.predecessor_id != tip_id:
                    raise ValueError("correction does not extend the effective check")
                tip_id = replacement.id
                consumed.add(replacement.id)
                invalidated = False
            else:
                raise ValueError("unknown check decision")
            last_recorded_at = event.recorded_at
        if pending:
            raise ValueError("check decision does not extend the effective chain")
        if set(by_id) - {root_id} != consumed:
            raise ValueError("check replacement has no recorded correction event")
        result.append(
            EffectiveCheck(
                root_id,
                tip_id,
                by_id[tip_id].outcome,
                invalidated,
                tuple(row.id for row in chain if row.id != tip_id),
            )
        )
    return tuple(result)


def _require_aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("check history instants must be timezone-aware")
