"""Read models for the status screens and endpoints (SPEC section 9, OPS-4, OPS-6). No writes.

The current status is the newest event (cya.operations.status derives the same one); this
module adds the bounded history and the pending-request queue that the screens show next to
it. A vehicle with no recorded event has no status: callers show it as In Service only as a
display default, and the checklist reader (cya.attention.checklist) remains the one place that
decides what is Exempt.
"""

from django.db.models import Exists, OuterRef

from cya.checklists.models import Vehicle
from cya.operations.commands import StatusError
from cya.operations.models import StatusRequest, VehicleStatusEvent

MAX_HISTORY = 100
MAX_PENDING = 200


def vehicle_in(organization_id, vehicle_id):
    """The organization's vehicle, or a 404 StatusError (a foreign vehicle looks the same)."""
    vehicle = Vehicle.objects.filter(organization_id=organization_id, pk=vehicle_id).first()
    if vehicle is None:
        raise StatusError("vehicle does not exist", code="NOT_FOUND", status=404)
    return vehicle


def _event_row(event):
    return {
        "event_id": str(event.pk),
        "sequence": event.sequence,
        "status": event.status,
        "explanation": event.explanation,
        "restrictions": event.restrictions,
        "recorded_at": event.recorded_at,
        "actor": event.actor_user.display_name,
    }


def status_history(organization_id, vehicle_id):
    """The vehicle's current event (or None) and its newest MAX_HISTORY events, newest first."""
    vehicle_in(organization_id, vehicle_id)
    rows = list(
        VehicleStatusEvent.objects.filter(organization_id=organization_id, vehicle_id=vehicle_id)
        .select_related("actor_user")
        .order_by("-sequence")[: MAX_HISTORY + 1]
    )
    return {
        "current": _event_row(rows[0]) if rows else None,
        "history": [_event_row(event) for event in rows[:MAX_HISTORY]],
        "history_truncated": len(rows) > MAX_HISTORY,
    }


def pending_requests(organization_id, vehicle_id=None):
    """Requests nobody has decided yet, oldest first, for one vehicle or the organization."""
    decided = StatusRequest.objects.filter(request=OuterRef("pk"))
    rows = (
        StatusRequest.objects.filter(organization_id=organization_id, action="REQUEST")
        .filter(~Exists(decided))
        .select_related("actor_user", "vehicle")
        .order_by("recorded_at", "pk")
    )
    if vehicle_id is not None:
        rows = rows.filter(vehicle_id=vehicle_id)
    found = list(rows[: MAX_PENDING + 1])
    return {
        "requests": [
            {
                "request_id": str(row.pk),
                "vehicle_id": str(row.vehicle_id),
                "vehicle": row.vehicle.display_name,
                "requested_status": row.requested_status,
                "reason": row.reason,
                "requested_by": row.actor_user.display_name,
                "recorded_at": row.recorded_at,
            }
            for row in found[:MAX_PENDING]
        ],
        "truncated": len(found) > MAX_PENDING,
    }
