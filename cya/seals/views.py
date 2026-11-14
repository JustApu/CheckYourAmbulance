"""Crew page for viewing and handling a vehicle's sealed subjects."""

from django.db import DatabaseError
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.inspections.views import _authorized, _vehicle
from cya.inspections.workflows import report_options
from cya.shell.navigation import roles_for


@never_cache
@require_GET
def vehicle_seals(request, vehicle_id):
    user, refusal = _authorized(request)
    if refusal:
        return refusal
    vehicle = _vehicle(user, vehicle_id)
    try:
        options = report_options(user.organization_id, timezone.now())
    except (DatabaseError, ValueError):
        options = {"categories": [], "severities": []}
    config = {
        "vehicle_id": str(vehicle.pk),
        "subjects_url": reverse("api-seal-subjects", args=[vehicle.pk]),
        "scope_url": reverse("api-seal-scope", args=[vehicle.pk, "KIND", "SUBJECT"]),
        "command_url": reverse("api-seal-command", args=["ACTION"]),
        "report_options": options,
        "can_request_audit": bool(roles_for(user) & {"SUPERVISOR", "ADMINISTRATOR"}),
    }
    return render(request, "seals/vehicle.html", {"vehicle": vehicle, "config": config})
