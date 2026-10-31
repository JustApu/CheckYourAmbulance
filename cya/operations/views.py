"""Vehicle status pages (issue 148 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/148)): the fleet list and one vehicle's status, history and requests.

Server-rendered shells around data the server already authorized. Every write goes through the
JSON endpoints in cya.operations.api from `operations/status.js`; hiding a form is a convenience
only, because the command's permission check decides again (SPEC 3.2). The panel URL renders just
the part of the page that a live hint refreshes.
"""

import logging
from functools import wraps

from django.db import DatabaseError
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.authorization.policy import ResourceContext, permitted
from cya.checklists.models import Vehicle
from cya.inspections.views import MAX_VEHICLES, STATUS_LABELS, _authorized, _vehicle, reverse_socket
from cya.operations import service
from cya.operations.commands import ACTION
from cya.operations.status import status_at
from cya.shell.navigation import roles_for

logger = logging.getLogger(__name__)


def _guarded(view):
    """Sign-in and section check, then the view; a database failure is an honest 503 page."""

    @never_cache
    @require_GET
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user, refusal = _authorized(request, "status")
        if refusal is not None:
            return refusal
        try:
            return view(request, user, *args, **kwargs)
        except DatabaseError as error:
            logger.error("status page unavailable: %s (%s)", view.__name__, type(error).__name__)
            return render(
                request,
                "inspections/workflow_unavailable.html",
                {
                    "title": "Status temporarily unavailable",
                    "unavailable_content": "Vehicle status",
                },
                status=503,
            )

    return wrapped


def _label(row):
    return {**row, "status_label": STATUS_LABELS[row["status"]]}


@_guarded
def vehicles(request, user):
    now = timezone.now()
    found = list(
        Vehicle.objects.filter(organization_id=user.organization_id, active=True).order_by(
            "display_name", "pk"
        )[:MAX_VEHICLES]
    )
    pending = service.pending_requests(str(user.organization_id))
    counts = {}
    for row in pending["requests"]:
        counts[row["vehicle_id"]] = counts.get(row["vehicle_id"], 0) + 1
    rows = []
    for vehicle in found:
        event = status_at(user.organization_id, vehicle.pk, now)
        code = event.status if event else "IN_SERVICE"
        rows.append(
            {
                "name": vehicle.display_name,
                "number": vehicle.number,
                "url": reverse("status-vehicle", args=[vehicle.pk]),
                "code": code,
                "label": STATUS_LABELS[code],
                "recorded": event is not None,
                "pending": counts.get(str(vehicle.pk), 0),
            }
        )
    return render(
        request,
        "operations/vehicles.html",
        {
            "title": "Vehicle status",
            "vehicles": rows,
            "pending_total": len(pending["requests"]),
            "pending_truncated": pending["truncated"],
        },
    )


def _panel_context(user, vehicle):
    organization_id = str(user.organization_id)
    history = service.status_history(organization_id, str(vehicle.pk))
    pending = service.pending_requests(organization_id, str(vehicle.pk))
    roles = roles_for(user)
    # Setting and deciding share one permission (supervisor, administrator); crew may only request.
    can_set = permitted(ACTION, str(user.pk), roles, context=ResourceContext(intent="mutate"))
    from cya.attention.checklist import vehicle_checklist
    from cya.obligations.adhoc import ACTION as ADHOC_ACTION

    can_order = permitted(ADHOC_ACTION, str(user.pk), roles)
    choices, inspection_unavailable = (), False
    if can_order:
        try:
            choices = vehicle_checklist(organization_id, str(vehicle.pk)).rows
        except ValueError:
            # An optional inspection form must not hide operational status and its controls
            # when checklist resolution or the bounded compliance reader is unavailable.
            inspection_unavailable = True
    return {
        "can_order_inspection": can_order,
        "inspection_items": [r for r in choices if r.kind != "VEHICLE_READING"],
        "inspection_unavailable": inspection_unavailable,
        "vehicle": vehicle,
        "current": _label(history["current"]) if history["current"] else None,
        "history": [_label(row) for row in history["history"]],
        "history_truncated": history["history_truncated"],
        "pending": [
            {**row, "label": STATUS_LABELS[row["requested_status"]]} for row in pending["requests"]
        ],
        "pending_truncated": pending["truncated"],
        "can_set": can_set,
        "can_request": permitted(
            ACTION, str(user.pk), roles, context=ResourceContext(intent="request")
        ),
        "statuses": [(code, label) for code, label in STATUS_LABELS.items()],
    }


@_guarded
def vehicle(request, user, vehicle_id):
    found = _vehicle(user, vehicle_id)
    context = _panel_context(user, found)
    context.update(
        title=found.display_name,
        config={
            "vehicle_id": str(found.pk),
            "panel_url": reverse("status-vehicle-panel", args=[found.pk]),
            "ad_hoc_url": reverse("api-ad-hoc-inspections", args=[found.pk]),
            "set_url": reverse("api-vehicle-status-changes", args=[found.pk]),
            "request_url": reverse("api-vehicle-status-requests", args=[found.pk]),
            "decision_url_template": reverse("api-status-request-decisions", args=["REQUEST"]),
            "socket": reverse_socket(found.pk),
        },
    )
    return render(request, "operations/vehicle.html", context)


@_guarded
def panel(request, user, vehicle_id):
    """The live part of the vehicle page alone, for an already-open page to re-read."""
    return render(
        request, "operations/panel.html", _panel_context(user, _vehicle(user, vehicle_id))
    )
