"""Crew inspection pages (issue 85 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/85)): vehicle list, vehicle start/join/reopen, session checklist.

The pages are server-rendered shells around data the server already authorized; the checklist
screen then calls the JSON endpoints (checks, odometer, sessions, view preferences) from
`inspections/checklist.js`. Hiding a control is a convenience only: every endpoint enforces
roles, participation and organization scope again (SPEC 3.2). A session the signed-in user did
not join answers exactly like one that does not exist.
"""

from django.db import DatabaseError
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.attention import preferences
from cya.authorization.backend import canonical_id
from cya.checklists.models import ChecklistTemplate, Vehicle
from cya.inspections.models import InspectionSession, SessionParticipant
from cya.inspections.projections import current_state, is_expired
from cya.inspections.workflows import report_options
from cya.obligations.requirements import bound_template_ids
from cya.odometer.models import OdometerEntry
from cya.operations.status import status_at
from cya.shell import navigation
from cya.shell.views import deny_section

MAX_VEHICLES = 200
MAX_SESSIONS = 20
STATUS_LABELS = {
    "IN_SERVICE": "In Service",
    "LIMITED_SERVICE": "Limited Service",
    "OUT_OF_SERVICE": "Out of Service",
}
STATE_LABELS = {"OPEN": "Open", "INCOMPLETE": "Incomplete", "CLOSED": "Closed"}


def _authorized(request, section_key="inspection"):
    """(user, None) for a permitted user, else (None, the redirect or refusal to return)."""
    user = request.cya_user
    if user is None:
        return None, HttpResponseRedirect("/auth/login/")
    section = navigation.section_by_key(section_key)
    roles = navigation.roles_for(user)
    if not section.roles & roles:
        return None, deny_section(request, user, roles, "VIEW_" + section.key.upper())
    return user, None


def _vehicle(user, vehicle_id):
    try:
        vehicle_id = canonical_id(vehicle_id)
    except ValueError:
        raise Http404 from None
    vehicle = Vehicle.objects.filter(
        organization_id=user.organization_id, pk=vehicle_id, active=True
    ).first()
    if vehicle is None:
        raise Http404
    return vehicle


def _status(user, vehicle, instant):
    event = status_at(user.organization_id, vehicle.pk, instant)
    code = event.status if event else "IN_SERVICE"
    return {
        "code": code,
        "label": STATUS_LABELS.get(code, code),
        "restrictions": event.restrictions if event else None,
    }


@never_cache
@require_GET
def vehicles(request):
    user, refusal = _authorized(request)
    if refusal:
        return refusal
    now = timezone.now()
    found = Vehicle.objects.filter(organization_id=user.organization_id, active=True).order_by(
        "display_name", "pk"
    )[:MAX_VEHICLES]
    rows = [
        {
            "name": vehicle.display_name,
            "number": vehicle.number,
            "url": reverse("inspection-vehicle", args=[vehicle.pk]),
            "status": _status(user, vehicle, now),
        }
        for vehicle in found
    ]
    return render(request, "inspections/vehicles.html", {"title": "Inspection", "vehicles": rows})


def _templates(user, vehicle, instant):
    ids = bound_template_ids(str(user.organization_id), str(vehicle.pk), instant)
    found = ChecklistTemplate.objects.filter(
        organization_id=user.organization_id, pk__in=ids, active=True
    ).order_by("label", "pk")
    return [{"id": str(t.pk), "label": t.label} for t in found]


def _session_rows(user, vehicle, instant):
    """Open sessions anyone may join, and the user's own ended ones they may reopen."""
    found = InspectionSession.objects.filter(
        organization_id=user.organization_id, vehicle=vehicle
    ).order_by("-opened_at", "-pk")[:MAX_SESSIONS]
    mine = set(
        SessionParticipant.objects.filter(session__in=[s.pk for s in found], user=user).values_list(
            "session_id", flat=True
        )
    )
    rows = []
    for session in found:
        state = current_state(session)
        shown = (
            "INCOMPLETE" if state.state == "OPEN" and is_expired(state, instant) else state.state
        )
        participant = session.pk in mine
        if shown != "OPEN" and not participant:
            continue  # only a participant may reopen it (SES-3)
        rows.append(
            {
                "id": str(session.pk),
                "state": shown,
                "state_label": STATE_LABELS[shown],
                "opened_at": session.opened_at,
                "participant": participant,
                "url": reverse("inspection-session", args=[session.pk]) if participant else None,
            }
        )
    return rows


@never_cache
@require_GET
def vehicle(request, vehicle_id):
    user, refusal = _authorized(request)
    if refusal:
        return refusal
    found = _vehicle(user, vehicle_id)
    now = timezone.now()
    context = {
        "title": found.display_name,
        "vehicle": found,
        "status": _status(user, found, now),
        "templates": _templates(user, found, now),
        "sessions": _session_rows(user, found, now),
        "config": {
            "vehicle_id": str(found.pk),
            "sessions_url": reverse("api-inspection-sessions"),
            "session_url_template": reverse("inspection-session", args=["SESSION"]),
        },
    }
    return render(request, "inspections/vehicle.html", context)


def _odometer_unit(user, vehicle):
    """The unit of the vehicle's latest reading, if any: a vehicle keeps one unit (ODO-3)."""
    return (
        OdometerEntry.objects.filter(organization_id=user.organization_id, vehicle=vehicle)
        .order_by("-recorded_at", "-pk")
        .values_list("unit", flat=True)
        .first()
    )


@never_cache
@require_GET
def session(request, session_id):
    user, refusal = _authorized(request)
    if refusal:
        return refusal
    try:
        session_id = canonical_id(session_id)
    except ValueError:
        raise Http404 from None
    found = (
        InspectionSession.objects.select_related("vehicle", "checklist_version")
        .filter(organization_id=user.organization_id, pk=session_id)
        .first()
    )
    if found is None or not SessionParticipant.objects.filter(session=found, user=user).exists():
        raise Http404
    now = timezone.now()
    state = current_state(found)
    shown = "INCOMPLETE" if state.state == "OPEN" and is_expired(state, now) else state.state
    saved = preferences.get_preferences(str(user.organization_id), user.pk)
    template = ChecklistTemplate.objects.filter(
        organization_id=user.organization_id, pk=found.checklist_version.template_id
    ).first()
    config = {
        "session_id": str(found.pk),
        "vehicle_id": str(found.vehicle_id),
        "checklist_version_id": str(found.checklist_version_id),
        "state": shown,
        "view": saved["preferred_view"],
        "needs_attention_only": saved["needs_attention_only"],
        "odometer_unit": _odometer_unit(user, found.vehicle),
        "urls": {
            "checklist": reverse("api-vehicle-checklist", args=[found.vehicle_id]),
            "checks": reverse("api-checks"),
            "odometer": reverse("api-odometer-entries"),
            "preferences": reverse("api-view-preferences"),
            "reopen": reverse("api-inspection-session-reopen", args=[found.pk]),
            "end": reverse("api-inspection-session-end", args=[found.pk]),
            "vehicle": reverse("inspection-vehicle", args=[found.vehicle_id]),
            "socket": reverse_socket(found.vehicle_id),
            "suggestions": reverse("api-deficiency-suggestions"),
            "deficiency_detail": reverse("deficiencies-detail", args=["DEFICIENCY"]),
        },
    }
    try:
        options = report_options(user.organization_id, now)
    except (DatabaseError, ValueError):
        options = {"categories": [], "severities": [], "unavailable": True}
    context = {
        "title": found.vehicle.display_name,
        "vehicle": found.vehicle,
        "template_label": template.label if template else "",
        "state": shown,
        "state_label": STATE_LABELS[shown],
        "status": _status(user, found.vehicle, now),
        "config": config,
        "report_options": options,
    }
    return render(request, "inspections/session.html", context)


def reverse_socket(vehicle_id):
    """The path of the vehicle live-update socket (cya.live_updates.routing)."""
    return f"/ws/v1/vehicles/{vehicle_id}/live/"
