"""Odometer obligation state as of an instant (SPEC 6.4 ODO-2, SCH-4, AC-21).

State is derived from the odometer obligations in force, the links and the vehicle's status
stream, never stored. Only evidence recorded by T counts, so a past instant gives the answer
that was true then. A later correction changes the effective entry; an invalidated chain's link
stays in history but no longer satisfies the obligation.
"""

from dataclasses import dataclass
from datetime import datetime

from cya.obligations.projections import MAX_ROWS, _bounded, _bounds
from cya.obligations.queries import odometer_obligations
from cya.obligations.state import State, aware
from cya.odometer.models import OdometerObligationLink, ReadingEvent
from cya.operations.models import VehicleStatusEvent


@dataclass(frozen=True)
class OdometerState:
    obligation_id: str
    due_at: datetime
    state: State
    entry_id: str | None = None
    completed_at: datetime | None = None
    late_seconds: float = 0


def _exempt(statuses, obligation, instant):
    """Out of service at min(D, T) exempts a routine obligation; a later return never backfills."""
    at = min(obligation.due_at, instant)
    known = [s for s in statuses if s.recorded_at <= at]
    return bool(known) and max(known, key=lambda s: s.sequence).status == "OUT_OF_SERVICE"


def _timed(obligation, instant):
    if instant < obligation.early_at:
        return State.UPCOMING
    if instant < obligation.due_at:
        return State.CURRENT
    return State.DUE if instant <= obligation.grace_at else State.OVERDUE


def get_vehicle_odometer_states_as_of(
    vehicle_id, instant_T, *, organization_id, start=None, end=None
):
    """Odometer obligation states at T for due instants in [start, end] (default T-7d..T+48h).

    The caller supplies trusted organization scope and performs request authorization.
    """
    aware(instant_T)
    start, end = _bounds(instant_T, start, end)
    organization_id, vehicle_id = str(organization_id), str(vehicle_id)
    obligations = _bounded(
        odometer_obligations(organization_id, start, end, vehicle_id=vehicle_id, as_of=instant_T)
    )
    link_rows = _bounded(
        OdometerObligationLink.objects.filter(
            organization_id=organization_id,
            obligation_id__in=[o.pk for o in obligations],
            recorded_at__lte=instant_T,
        )
        .select_related("entry")
        .order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    linked_roots = {link.entry.root_entry_id for link in link_rows}
    invalidated = set(
        ReadingEvent.objects.filter(
            organization_id=organization_id,
            event_type="INVALIDATION",
            root_odometer_id__in=linked_roots,
            recorded_at__lte=instant_T,
        )
        .order_by("recorded_at", "id")
        .values_list("root_odometer_id", flat=True)[: MAX_ROWS + 1]
    )
    if len(invalidated) > MAX_ROWS:
        raise ValueError("projection evidence exceeds 10000 rows; narrow the due interval")
    links = {
        str(link.obligation_id): link
        for link in link_rows
        if link.entry.root_entry_id not in invalidated
    }
    roots = {link.entry.root_entry_id for link in links.values()}
    tips = {link.entry.root_entry_id: link.entry for link in links.values()}
    corrections = _bounded(
        ReadingEvent.objects.filter(
            organization_id=organization_id,
            event_type="CORRECTION",
            root_odometer_id__in=roots,
            recorded_at__lte=instant_T,
        )
        .select_related("replacement_odometer")
        .order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    pending = {(event.root_odometer_id, event.selected_odometer_id): event for event in corrections}
    if len(pending) != len(corrections):
        raise ValueError("odometer correction chain forks")
    for root_id, tip in tips.items():
        for _ in range(len(corrections)):
            event = pending.pop((root_id, tip.pk), None)
            if event is None:
                break
            tip = event.replacement_odometer
        tips[root_id] = tip
    if pending:
        raise ValueError("odometer correction chain is inconsistent")
    statuses = _bounded(
        VehicleStatusEvent.objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, recorded_at__lte=instant_T
        ).order_by("sequence")[: MAX_ROWS + 1]
    )
    result = []
    for obligation in obligations:
        link = links.get(str(obligation.pk))
        if _exempt(statuses, obligation, instant_T):
            state = OdometerState(str(obligation.pk), obligation.due_at, State.EXEMPT)
        elif link is not None:
            done = link.entry.recorded_at
            late = max(0, (done - obligation.grace_at).total_seconds())
            state = OdometerState(
                str(obligation.pk),
                obligation.due_at,
                State.SATISFIED,
                str(tips[link.entry.root_entry_id].pk),
                done,
                late,
            )
        else:
            state = OdometerState(
                str(obligation.pk), obligation.due_at, _timed(obligation, instant_T)
            )
        result.append(state)
    return tuple(result)
