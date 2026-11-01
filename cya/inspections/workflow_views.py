"""Read-only HTML shells for issue 86 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/86); all writes use existing audited JSON endpoints.

Issue 16 (https://github.com/JustApu/CheckYourAmbulance/issues/16) adds the supervisor and
administrator forms to the deficiency page and the supervisor queue. Form visibility follows the
permission policy and the deficiency's status; every command still rechecks both.
"""

import logging
from functools import wraps

from django.db import DatabaseError
from django.db.models import Exists, OuterRef, Subquery
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.attention.api import _authors, _row
from cya.attention.checklist import vehicle_checklist
from cya.authorization.backend import canonical_id
from cya.authorization.policy import ResourceContext, permitted
from cya.checklists.models import Item, KitInstance, Vehicle
from cya.deficiencies import acknowledgement, overdue, service, suspension
from cya.deficiencies.commands import ASSIGNEE_ROLES
from cya.deficiencies.models import Deficiency, DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.service import DeficiencyError, suggest_related
from cya.deficiencies.transitions import (
    ACTIVE_STATUSES,
    CLOSED_ADMINISTRATOR,
    CLOSED_UNVERIFIABLE,
    CLOSED_VERIFIED,
)
from cya.inspections.views import _authorized, _status, _vehicle, reverse_socket
from cya.inspections.workflows import (
    check_history,
    deficiency_history,
    report_options,
    verification_scope,
)
from cya.notifications.enqueue import resolve_recipients
from cya.shell.navigation import roles_for
from cya.shell.views import deny_section

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
    "CHECKS_SUSPENDED": "Routine checks suspended",
    "ESCALATION_ACKNOWLEDGED": "Escalation acknowledged",
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
            "deficiencies"
            if view.__name__
            in ("deficiencies", "deficiency", "verification", "verification_items", "queue")
            else "inspection"
        )
        user, refusal = _authorized(request, section)
        if refusal is not None:
            return refusal
        try:
            return view(request, user, *args, **kwargs)
        except (DatabaseError, ValueError, DeficiencyError) as error:
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
            "can_view_queue": permitted("decide_extension", str(user.pk), roles_for(user)),
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


MAX_CHOICES = 500  # item, kit and assignee choices one form offers


def _item_choices(organization_id, vehicle_id, now, within=None):
    """[(value "item_id:kit_id", label)] from the vehicle's current checklist, optionally limited
    to the (item, kit) pairs in `within`. The server resolves the same rows again and refuses any
    choice that no longer applies."""
    rows = vehicle_checklist(organization_id, vehicle_id, as_of=now).rows
    choices = {}
    for row in rows:
        key = (str(row.item_id), str(row.kit_id) if row.kit_id else None)
        if within is not None and key not in within:
            continue
        label = row.item_label + (f" — {row.kit_label}" if row.kit_label else "")
        choices[f"{key[0]}:{key[1] or ''}"] = label
    return sorted(choices.items(), key=lambda p: (p[1], p[0]))[:MAX_CHOICES]


def _suspensions(organization_id, deficiency_id):
    rows = suspension.suspension_state(organization_id, deficiency_id)["suspensions"]
    items = {
        str(i.pk): i.label
        for i in Item.objects.filter(
            organization_id=organization_id, pk__in={r["item_id"] for r in rows}
        )
    }
    kits = {
        str(k.pk): k.label
        for k in KitInstance.objects.filter(
            organization_id=organization_id,
            pk__in={r["kit_instance_id"] for r in rows if r["kit_instance_id"]},
        )
    }
    for row in rows:
        row["label"] = items.get(row["item_id"], "Item") + (
            f" — {kits.get(row['kit_instance_id'], 'Kit')}" if row["kit_instance_id"] else ""
        )
        row["end_label"] = EVENT_LABELS.get(row["end_event_type"] or "", "")
    return rows


def _supervisor_controls(user, found, status, roles, context, deadline):
    """What the supervisor and administrator forms need; None for a form the user cannot use.

    Visibility only: each command rechecks the permission, the status and the expected event.
    """
    org, deficiency_id, now = str(user.organization_id), str(found.pk), timezone.now()
    active = status.status in ACTIVE_STATUSES

    def may(action):
        return permitted(action, str(user.pk), roles, context=context)

    controls = {
        "assignees": None,
        "can_set_deadline": False,
        "can_start_work": False,
        "pending_extensions": [],
        "acknowledgement": None,
        "severities": None,
        "verification_items": None,
        "expanding": False,
        "suspension_items": None,
        "can_close": False,
        "can_reopen": False,
    }
    if active and may("assign_deficiency_deadline"):
        controls["assignees"] = sorted(
            (
                (str(u.pk), u.display_name)
                for u in resolve_recipients(org, roles=[r.value for r in ASSIGNEE_ROLES])
            ),
            key=lambda p: (p[1], p[0]),
        )[:MAX_CHOICES]
        controls["can_set_deadline"] = True
        controls["can_start_work"] = status.status == "OPEN"
    if active and may("decide_extension"):
        controls["pending_extensions"] = [
            r for r in deadline["requests"] if r["status"] == "PENDING" and not r["superseded"]
        ]
    if active and may("acknowledge_escalation"):
        controls["acknowledgement"] = acknowledgement.acknowledgement_state(org, deficiency_id)
    if active and may("regrade_deficiency_severity"):
        controls["severities"] = report_options(user.organization_id, now)["severities"]
    if may("expand_verification") and status.status in ("PENDING_VERIFICATION", CLOSED_VERIFIED):
        controls["verification_items"] = _item_choices(org, str(found.vehicle_id), now)
        controls["expanding"] = status.status == "PENDING_VERIFICATION"
    if active and may("suspend_routine_checks"):
        scope = {
            (item_id, str(kit_id) if kit_id else None)
            for item_id, _definition, kit_id in service.correction_scope(org, found, now)
        }
        controls["suspension_items"] = _item_choices(org, str(found.vehicle_id), now, scope)
    if may("explicit_deficiency_close"):
        controls["can_close"] = active
        controls["can_reopen"] = status.status in (CLOSED_ADMINISTRATOR, CLOSED_UNVERIFIABLE)
    # A form is offered when its value is set; an empty choice list still shows its form.
    controls["shown"] = bool(controls["pending_extensions"]) or any(
        value is not None and value is not False
        for key, value in controls.items()
        if key not in ("pending_extensions", "expanding")
    )
    return controls


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
        event.decision_label = {
            "APPROVED": "Approved",
            "DENIED": "Denied",
            "DIFFERENT_DATE": "Different date",
        }.get(event.decision, "")
    status = DeficiencyProjection.objects.filter(
        organization_id=user.organization_id, deficiency=found
    ).first()
    if status is None or not events or status.last_applied_event_id != events[-1].pk:
        raise ValueError("deficiency projection unavailable")
    roles = roles_for(user)
    context = ResourceContext(
        assignee_id=str(status.assignee_id) if status.assignee_id else None,
        supply_related=found.category_version.supply_related,
    )
    active = status.status in ACTIVE_STATUSES

    def allowed(action):
        return active and permitted(action, str(user.pk), roles, context=context)

    deadline = service.extension_state(str(user.organization_id), str(found.pk))
    dependency = service.dependency_state(str(user.organization_id), str(found.pk))["dependency"]
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
            "can_record_repair": status.status == "IN_PROGRESS"
            and allowed("record_deficiency_correction"),
            "can_request_extension": allowed("request_extension"),
            "can_update_dependency": allowed("update_external_dependency"),
            "can_add_note": allowed("add_progress_note"),
            "can_enter_dependency": status.status in ("OPEN", "IN_PROGRESS"),
            "dependency": dependency,
            "deadline": deadline,
            "age_days": deadline["age_seconds"] // 86400,
            "assignee": status.assignee,
            "scope": verification_scope(found) if status.status == "PENDING_VERIFICATION" else [],
            "suspensions": _suspensions(str(user.organization_id), str(found.pk)),
            "supervisor": _supervisor_controls(user, found, status, roles, context, deadline),
            "config": {
                "correction_url": reverse("api-deficiency-corrections", args=[found.pk]),
                "expected_event_id": str(status.last_applied_event_id),
            },
        },
    )


QUEUE_LIMIT = 200  # rows per queue list; a longer list says it is cut short


def _queue_rows(projections):
    rows = list(projections[: QUEUE_LIMIT + 1])
    for row in rows:
        row.status_label = STATUS_LABELS[row.status]
    return rows[:QUEUE_LIMIT], len(rows) > QUEUE_LIMIT


@page
def queue(request, user):
    """Unassigned, overdue and pending-extension deficiencies (issue 16), from existing readers."""
    roles = roles_for(user)
    if not permitted("decide_extension", str(user.pk), roles):
        return deny_section(request, user, roles, "VIEW_DEFICIENCY_QUEUE")
    org, now = str(user.organization_id), timezone.now()
    active = DeficiencyProjection.objects.filter(
        organization_id=org, status__in=ACTIVE_STATUSES
    ).select_related("deficiency__vehicle", "assignee")
    unassigned, unassigned_more = _queue_rows(
        active.filter(assignee__isnull=True).order_by("deficiency__reported_at", "deficiency_id")
    )
    late, late_more = _queue_rows(
        overdue.candidates(org, now)
        .select_related("deficiency__vehicle", "assignee")
        .order_by("governing", "deficiency_id")
    )
    # A request is waiting while no decision answers it and no later deadline revision (an event
    # carrying a deadline that is not a decision) has superseded it: deadlines.deadline_state's rule.
    waiting = (
        DeficiencyEvent.objects.filter(organization_id=org, event_type="EXTENSION_REQUESTED")
        .exclude(Exists(DeficiencyEvent.objects.filter(request_event_id=OuterRef("pk"))))
        .exclude(
            Exists(
                DeficiencyEvent.objects.filter(
                    deficiency_id=OuterRef("deficiency_id"),
                    sequence__gt=OuterRef("sequence"),
                    deadline__isnull=False,
                ).exclude(event_type="EXTENSION_DECIDED")
            )
        )
    )
    pending, pending_more = _queue_rows(
        active.annotate(
            requested_at=Subquery(
                waiting.filter(deficiency_id=OuterRef("deficiency_id"))
                .order_by("sequence")
                .values("recorded_at")[:1]
            )
        )
        .filter(requested_at__isnull=False)
        .order_by("requested_at", "deficiency_id")
    )
    return render(
        request,
        "inspections/deficiency_queue.html",
        {
            "title": "Supervisor queue",
            "lists": [
                ("Unassigned", unassigned, unassigned_more),
                ("Overdue", late, late_more),
                ("Extension requests waiting for a decision", pending, pending_more),
            ],
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


def _verification_deficiency(user, deficiency_id):
    found = (
        Deficiency.objects.filter(organization_id=user.organization_id, pk=_identity(deficiency_id))
        .select_related("vehicle")
        .first()
    )
    if found is None:
        raise Http404
    return found


@page
def verification(request, user, deficiency_id):
    found = _verification_deficiency(user, deficiency_id)
    # This is an independent check screen: no session is created or impersonated.
    verification_scope(found)
    config = {
        "vehicle_id": str(found.vehicle_id),
        "verification_for_deficiency_id": str(found.pk),
        "state": "OPEN",
        "view": "NORMAL",
        "needs_attention_only": False,
        "urls": {
            "checklist": reverse("deficiency-verification-items", args=[found.pk]),
            "checks": reverse("api-checks"),
            "socket": reverse_socket(found.vehicle_id),
            "deficiency_detail": reverse("deficiencies-detail", args=["DEFICIENCY"]),
        },
    }
    return render(
        request,
        "inspections/session.html",
        {
            "title": "Verify: " + found.summary,
            "vehicle": found.vehicle,
            "verification": found,
            "state": "OPEN",
            "config": config,
            "status": _status(user, found.vehicle, timezone.now()),
        },
    )


@page
def verification_items(request, user, deficiency_id):
    found = _verification_deficiency(user, deficiency_id)
    events, _ = deficiency_history(found)
    if not events:
        raise ValueError("deficiency history unavailable")
    pending = events[-1].to_status == "PENDING_VERIFICATION"
    scope = verification_scope(found) if pending else []
    needed = {(str(r.item_id), str(r.kit_instance_id or "")) for r in scope if not r.satisfied}
    result = vehicle_checklist(str(user.organization_id), str(found.vehicle_id))
    rows = [r for r in result.rows if (r.item_id, r.kit_id or "") in needed]
    authors = _authors(str(user.organization_id), rows)
    return JsonResponse(
        {
            "status": "OK",
            "vehicle_status": result.vehicle_status,
            "items": [_row(r, authors.get(r.latest_check_id)) for r in rows],
            "verification_status": STATUS_LABELS[events[-1].to_status],
            "remaining_scope_rows": len(needed),
            "unavailable_scope_rows": len(needed - {(r.item_id, r.kit_id or "") for r in rows}),
        }
    )
