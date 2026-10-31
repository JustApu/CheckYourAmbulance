"""Derived operational status and out-of-service intervals (SPEC OPS-5, OPS-6, section 5.9).

Status is never stored on the vehicle: it is the vehicle's latest vehicle_status_event.
Events are append-only and carry the server's recorded_at (TIME-4), so an as-of answer for
a past instant can never change later. That is also why returning to service does not
backfill routine obligations (OQ-31): a due instant that fell inside an out-of-service
interval stays exempt, and only later due instants see the vehicle as in service.

A status holds from its event's recorded_at up to, but excluding, the next event's. Nothing
here writes. Instants must be timezone-aware.
"""

from dataclasses import dataclass
from datetime import datetime

from cya.operations.models import VehicleStatusEvent

OUT_OF_SERVICE = "OUT_OF_SERVICE"


@dataclass(frozen=True)
class Interval:
    """A closed span [start, end] of out-of-service time, clipped to the queried horizon."""

    start: datetime
    end: datetime
    event_id: str  # the OUT_OF_SERVICE event that began the period


def _aware(value, field):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


def _events(organization_id, vehicle_id):
    return VehicleStatusEvent.objects.filter(organization_id=organization_id, vehicle_id=vehicle_id)


def current_status(organization_id, vehicle_id):
    """The latest event, or None when no status has ever been recorded for the vehicle."""
    return _events(organization_id, vehicle_id).order_by("-sequence").first()


def status_at(organization_id, vehicle_id, instant):
    """The event in force at `instant` (inclusive of an event recorded exactly then), or None."""
    _aware(instant, "instant")
    return (
        _events(organization_id, vehicle_id)
        .filter(recorded_at__lte=instant)
        .order_by("-sequence")
        .first()
    )


def out_of_service_at(organization_id, vehicle_id, instant):
    """The OUT_OF_SERVICE event in force at `instant`, else None (no recorded status is not OOS)."""
    event = status_at(organization_id, vehicle_id, instant)
    return event if event is not None and event.status == OUT_OF_SERVICE else None


def intervals_from(events, start, end):
    """Out-of-service spans within closed [start, end] from (id, status, recorded_at) tuples.

    `events` are in sequence order: the one in force at `start` (if any) first, then every
    later event up to `end`. Pure so the boundary arithmetic is testable without a database.
    """
    spans = []
    for index, (event_id, status, began) in enumerate(events):
        if status != OUT_OF_SERVICE:
            continue
        ended = events[index + 1][2] if index + 1 < len(events) else None
        # Events may share a recorded_at (sequence breaks the tie), and then [began, ended)
        # is empty: the vehicle was never out of service under this event.
        if ended is not None and ended <= began:
            continue
        # Half-open [began, ended) meets the closed horizon unless it ends at/before start.
        if began <= end and (ended is None or ended > start):
            spans.append(
                Interval(max(began, start), end if ended is None else min(ended, end), event_id)
            )
    return spans


def out_of_service_intervals(organization_id, vehicle_id, start, end):
    """Spans of closed [start, end] during which the vehicle was out of service."""
    _aware(start, "start")
    _aware(end, "end")
    if start > end:
        raise ValueError("start must not exceed end")
    in_force = status_at(organization_id, vehicle_id, start)
    later = _events(organization_id, vehicle_id).filter(recorded_at__gt=start, recorded_at__lte=end)
    rows = ([in_force] if in_force is not None else []) + list(later.order_by("sequence"))
    return intervals_from([(e.pk, e.status, e.recorded_at) for e in rows], start, end)


def out_of_service_during(organization_id, vehicle_id, start, end):
    """True when any part of closed [start, end] was out of service (for example [E, G])."""
    return bool(out_of_service_intervals(organization_id, vehicle_id, start, end))


def routine_exemption(organization_id, vehicle_id, due_at):
    """The OUT_OF_SERVICE event that exempts a routine obligation due at `due_at`, else None.

    SPEC 5.9 exempts the obligation when the vehicle is out of service at its due instant D.
    A vehicle back in service by D keeps its obligation even if the window began out of
    service, and one that goes out of service after D keeps the obligation generated at D.
    """
    return out_of_service_at(organization_id, vehicle_id, due_at)
