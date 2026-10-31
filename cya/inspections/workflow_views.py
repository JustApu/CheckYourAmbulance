"""Read-only HTML shells for issue 86 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/86); all writes use existing audited JSON endpoints."""

import logging
from functools import wraps

from django.db import DatabaseError
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.attention.checklist import vehicle_checklist
from cya.authorization.backend import canonical_id
from cya.checklists.models import KitInstance, Vehicle
from cya.deficiencies.models import Deficiency, DeficiencyProjection
from cya.deficiencies.service import suggest_related
from cya.inspections.views import _authorized, _vehicle
from cya.inspections.workflows import check_history, deficiency_history, report_options
from cya.shell.navigation import roles_for

logger = logging.getLogger(__name__)
EVENT_LABELS = {
    "REPORTED": "Reported",
    "REPORTED_IN_ERROR": "Reported in Error",
    "CORRECTION_NOTICE": "Entry corrected — supervisor review needed",
    "REOPENED": "Reopened",
    "SEVERITY_CHANGED": "Severity changed",
    "ASSIGNED": "Assigned",
    "DEADLINE_SET": "Deadline set",
    "WORK_STARTED": "Work started",
    "DEPENDENCY_ENTERED": "Awaiting external dependency",
    "DEPENDENCY_REVISED": "Dependency updated",
    "DEPENDENCY_ENDED": "Dependency ended",
    "PROGRESS_NOTE": "Progress note",
    "EXTENSION_REQUESTED": "Extension requested",
    "EXTENSION_DECIDED": "Extension decided",
    "ADMINISTRATOR_CLOSED": "Closed by administrator",
    "ADMINISTRATOR_REOPENED": "Reopened by administrator",
    "EMPTY_SCOPE_CLOSED": "Closed — no current verification items",
    "CORRECTION_RECORDED": "Repair recorded",
    "VERIFICATION_CLOSED": "Closed — verified",
    "VERIFICATION_FAILED": "Verification failed — back in progress",
    "VERIFICATION_REOPENED": "Reopened — a verification check was withdrawn",
    "SCOPE_EXPANDED": "Verification scope expanded",
    "VERIFICATION_ADDED": "Reopened — verification added after closure",
}
STATUS_LABELS = {
    "OPEN": "Open",
    "IN_PROGRESS": "In Progress",
    "AWAITING_EXTERNAL_DEPENDENCY": "Awaiting External Dependency",
    "PENDING_VERIFICATION": "Pending Verification",
    "CLOSED_VERIFIED": "Closed — Verified",
    "CLOSED_ADMINISTRATOR": "Closed — Administrator",
    "CLOSED_UNVERIFIABLE": "Closed — No current verification items",
    "REPORTED_IN_ERROR": "Reported in Error",
}


def page(view):
    @never_cache
    @require_GET
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        section = (
            "deficiencies" if view.__name__ in ("deficiencies", "deficiency") else "inspection"
        )
        user, refusal = _authorized(request, section)
        if refusal is not None:
            return refusal
        try:
            return view(request, user, *args, **kwargs)
        except (DatabaseError, ValueError) as error:
            logger.error("workflow page unavailable: %s (%s)", view.__name__, type(error).__name__)
            reporting = view.__name__ == "report"
            return render(
                request,
                "inspections/workflow_unavailable.html",
                {
                    "title": "Reporting temporarily unavailable"
                    if reporting
                    else "History temporarily unavailable",
                    "unavailable_content": "This report form" if reporting else "This history",
                },
                status=503,
            )

    return wrapped


def _identity(value):
    try:
        return canonical_id(value)
    except ValueError:
        raise Http404 from None


@page
def deficiencies(request, user):
    rows = list(
        DeficiencyProjection.objects.filter(organization_id=user.organization_id)
        .select_related(
            "deficiency__vehicle",
            "severity_version",
        )
        .order_by("-deficiency__reported_at", "-deficiency_id")[:100]
    )
    for row in rows:
        row.status_label = STATUS_LABELS[row.status]
    vehicles = Vehicle.objects.filter(organization_id=user.organization_id, active=True).order_by(
        "display_name", "id"
    )[:200]
    return render(
        request,
        "inspections/deficiencies.html",
        {
            "title": "Deficiencies",
            "deficiencies": rows,
            "vehicles": vehicles,
        },
    )


@page
def report(request, user, vehicle_id):
    vehicle = _vehicle(user, vehicle_id)
    now = timezone.now()
    rows = vehicle_checklist(str(user.organization_id), str(vehicle.pk), as_of=now).rows
    items, compartments, systems = {}, {}, {}
    for row in rows:
        items[row.item_id] = row.item_label
        if row.compartment_id:
            compartments[row.compartment_id] = row.compartment_label
        if row.system_id:
            systems[row.system_id] = row.system_label
    kits = {
        str(row.pk): row.label
        for row in KitInstance.objects.filter(
            organization_id=user.organization_id, vehicle=vehicle, active=True
        )
    }
    return render(
        request,
        "inspections/deficiency_report.html",
        {
            "title": "Report a deficiency",
            "vehicle": vehicle,
            "items": sorted(items.items(), key=lambda p: (p[1], p[0])),
            "kits": sorted(kits.items(), key=lambda p: (p[1], p[0])),
            "compartments": sorted(compartments.items(), key=lambda p: (p[1], p[0])),
            "systems": sorted(systems.items(), key=lambda p: (p[1], p[0])),
            "options": report_options(user.organization_id, now),
            "config": {
                "vehicle_id": str(vehicle.pk),
                "suggestions_url": reverse("api-deficiency-suggestions"),
            },
        },
    )


@page
def deficiency(request, user, deficiency_id):
    found = (
        Deficiency.objects.filter(organization_id=user.organization_id, pk=_identity(deficiency_id))
        .select_related("vehicle", "reporter", "severity_version", "category_version")
        .first()
    )
    if found is None:
        raise Http404
    events, related = deficiency_history(found)
    for event in events:
        event.status_label = STATUS_LABELS[event.to_status]
        event.type_label = EVENT_LABELS.get(event.event_type, event.event_type)
    status = DeficiencyProjection.objects.filter(
        organization_id=user.organization_id, deficiency=found
    ).first()
    if status is None:
        raise ValueError("deficiency projection unavailable")
    return render(
        request,
        "inspections/deficiency.html",
        {
            "title": found.summary,
            "deficiency": found,
            "status": STATUS_LABELS[status.status],
            "severity": status.severity_version,
            "events": events,
            "related": related,
            "suggestions": [
                r
                for r in suggest_related(str(user.organization_id), str(found.vehicle_id))
                if r["deficiency_id"] != str(found.pk)
            ],
            "can_record_repair": status.status == "IN_PROGRESS",
            "config": {
                "correction_url": reverse("api-deficiency-corrections", args=[found.pk]),
                "expected_event_id": str(status.last_applied_event_id),
            },
        },
    )


@page
def checks(request, user, vehicle_id):
    vehicle = Vehicle.objects.filter(
        organization_id=user.organization_id, pk=_identity(vehicle_id)
    ).first()
    if vehicle is None:
        raise Http404
    return render(
        request,
        "inspections/check_history.html",
        {
            "title": "Check history",
            "vehicle": vehicle,
            "checks": check_history(user, vehicle, roles_for(user), timezone.now()),
            "config": {},
        },
    )
