"""Read-only HTML shells for issue 86 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/86); all writes use existing audited JSON endpoints.

Issue 16 (https://github.com/JustApu/CheckYourAmbulance/issues/16) adds the supervisor and
administrator forms to the deficiency page and the supervisor queue. Form visibility follows the
permission policy and the deficiency's status; every command still rechecks both.
"""

import logging
from functools import wraps
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Subquery, When
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.accounts.models import User
from cya.attention.api import check_authors, checklist_row
from cya.attention.checklist import vehicle_checklist, vehicle_label_rows
from cya.authorization.backend import canonical_id
from cya.authorization.models import RoleGrant
from cya.authorization.policy import ResourceContext, Role, permitted
from cya.checklists.models import Item, KitInstance, Vehicle
from cya.deficiencies import acknowledgement, overdue, service, suspension
from cya.deficiencies.commands import ASSIGNEE_ROLES
from cya.deficiencies.models import Deficiency, DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.service import DeficiencyError, suggest_related
from cya.deficiencies.severity import levels_in_force, top_active_levels
from cya.deficiencies.transitions import (
    ACTIVE_STATUSES,
    CLOSED_ADMINISTRATOR,
    CLOSED_UNVERIFIABLE,
    CLOSED_VERIFIED,
    EMPTY_SCOPE_CLOSURE_STATUSES,
    STATUSES,
)
from cya.inspections.views import _authorized, _status, _vehicle, reverse_socket
from cya.inspections.workflows import (
    check_history,
    deficiency_history,
    report_options,
    verification_scope,
)
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
    "SUSPENSION_ENDED": "Check suspension ended early",
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
DEFICIENCY_PAGE_SIZE = 25
SEARCH_LIMIT = 100
FILTER_FIELDS = ("q", "vehicle", "severity", "assignee", "status", "overdue")


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
        # verification_items is re-read by the checklist script (issue 37).
        user, refusal = _authorized(
            request, section, background=view.__name__ == "verification_items"
        )
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


def _severity_levels(organization_id, now):
    return sorted(
        levels_in_force(organization_id, now).values(),
        key=lambda level: (-level.rank, level.code),
    )


def _deficiency_filters(
    request, organization_id, queryset, *, now, allowed_statuses=STATUSES, severity_options=()
):
    """Apply bounded, organization-scoped GET filters to a current projection query."""
    values, errors = {}, []
    for field in FILTER_FIELDS:
        supplied = request.GET.getlist(field)
        if len(supplied) > 1:
            errors.append("A filter was supplied more than once. Remove the duplicate and retry.")
            values[field] = ""
            continue
        value = supplied[0].strip() if supplied else ""
        if field != "overdue" and len(value) > SEARCH_LIMIT:
            errors.append(f"Filter text is limited to {SEARCH_LIMIT} characters.")
            values[field] = ""
        else:
            values[field] = value

    status = values["status"] or "active"
    valid_statuses = {*allowed_statuses, "active", "all"}
    if status not in valid_statuses:
        errors.append("Choose a valid lifecycle status.")
    elif status == "active":
        queryset = queryset.filter(status__in=ACTIVE_STATUSES)
    elif status != "all":
        queryset = queryset.filter(status=status)

    overdue_only = values["overdue"] in ("1", "true", "on")
    if values["overdue"] and not overdue_only:
        errors.append("Choose a valid overdue filter value.")
    if overdue_only:
        candidates = overdue.candidates(organization_id, now).values("pk")
        queryset = queryset.filter(pk__in=candidates)

    if values["q"]:
        query = values["q"]
        queryset = queryset.filter(
            Q(deficiency__summary__icontains=query) | Q(deficiency__description__icontains=query)
        )
    if values["vehicle"]:
        vehicle = values["vehicle"]
        queryset = queryset.filter(
            Q(deficiency__vehicle__display_name__icontains=vehicle)
            | Q(deficiency__vehicle__number__icontains=vehicle)
        )
    if values["severity"]:
        configured_codes = {code for code, _label in severity_options}
        if values["severity"] not in configured_codes:
            errors.append("Choose a configured severity.")
        else:
            queryset = queryset.filter(severity_version__code=values["severity"])
    if values["assignee"]:
        queryset = queryset.filter(assignee__display_name__icontains=values["assignee"])

    if errors:
        queryset = queryset.none()

    status_options = [(code, STATUS_LABELS[code]) for code in allowed_statuses]
    applied = []
    for field, label in (
        ("q", "Text"),
        ("vehicle", "Vehicle"),
        ("severity", "Severity"),
        ("assignee", "Assigned to"),
    ):
        if values[field]:
            applied.append((label, values[field]))
    if status != "active" and status in valid_statuses:
        applied.append(("Status", "All statuses" if status == "all" else STATUS_LABELS[status]))
    if overdue_only:
        applied.append(("Overdue", "Yes"))
    query_values = {field: values[field] for field in FILTER_FIELDS if values[field]}
    if not values["status"]:
        query_values["status"] = "active"
    return queryset, {
        "filters": values | {"status": status, "overdue": overdue_only},
        "filter_errors": errors,
        "filter_status_options": status_options,
        "filter_severity_options": severity_options,
        "applied_filters": applied,
        "filter_query": urlencode(query_values),
    }


def _page_url(request, filter_query, key, number):
    query = f"{filter_query}&" if filter_query else ""
    return f"{request.path}?{query}{key}_page={number}"


def _paginated_page(request, queryset, *, filter_query, key="results"):
    """Return one bounded page and its filtered total in deterministic queryset order."""
    page_values = request.GET.getlist(f"{key}_page")
    page_error = ""
    if len(page_values) > 1:
        requested = 1
        page_error = "That results-page link was invalid. Showing the first page instead."
    elif page_values:
        try:
            requested = int(page_values[0])
            if requested < 1:
                requested = 1
                page_error = "That results-page link was invalid. Showing the first page instead."
        except ValueError:
            requested = 1
            page_error = "That results-page link was invalid. Showing the first page instead."
    else:
        requested = 1

    paginator = Paginator(queryset, DEFICIENCY_PAGE_SIZE)
    if requested > paginator.num_pages:
        requested = paginator.num_pages
        page_error = "That results-page link was out of range. Showing the last page instead."
    result_page = paginator.page(requested)
    previous_url = (
        _page_url(request, filter_query, key, result_page.previous_page_number())
        if result_page.has_previous()
        else None
    )
    next_url = (
        _page_url(request, filter_query, key, result_page.next_page_number())
        if result_page.has_next()
        else None
    )
    return {
        "rows": list(result_page.object_list),
        "total": paginator.count,
        "has_previous": previous_url is not None,
        "has_next": next_url is not None,
        "previous_url": previous_url,
        "next_url": next_url,
        "page_error": page_error,
    }


@page
def deficiencies(request, user):
    org, now = str(user.organization_id), timezone.now()
    severity_levels = _severity_levels(org, now)
    queryset = DeficiencyProjection.objects.filter(organization_id=org).select_related(
        "deficiency__vehicle", "severity_version", "assignee"
    )
    queryset, filter_context = _deficiency_filters(
        request,
        org,
        queryset,
        now=now,
        allowed_statuses=STATUSES,
        severity_options=[
            (
                level.code,
                f"{level.label} ({level.code})"
                if level.active
                else f"{level.label} ({level.code}, retired)",
            )
            for level in severity_levels
        ],
    )
    queryset = queryset.order_by("-deficiency__reported_at", "-deficiency_id")
    page_data = _paginated_page(request, queryset, filter_query=filter_context["filter_query"])
    for row in page_data["rows"]:
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
            "page": page_data,
            "filter_action": reverse("deficiencies"),
            "filter_all_statuses_label": "All statuses, including closed and Reported in Error",
            "filter_total_label": "matching deficiencies",
            "pagination_label": "Deficiency result pages",
            **filter_context,
            "vehicles": vehicles,
        },
    )


@page
def report(request, user, vehicle_id):
    vehicle = _vehicle(user, vehicle_id)
    now = timezone.now()
    rows = vehicle_label_rows(str(user.organization_id), str(vehicle.pk))
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


def _item_choices(organization_id, vehicle_id, within=None):
    """[(value "item_id:kit_id", label)] from the vehicle's current checklist, optionally limited
    to the (item, kit) pairs in `within`. The server resolves the same rows again and refuses any
    choice that no longer applies."""
    rows = vehicle_label_rows(organization_id, vehicle_id)
    choices = {}
    for row in rows:
        key = (str(row.item_id), str(row.kit_id) if row.kit_id else None)
        if within is not None and key not in within:
            continue
        label = row.item_label + (f" — {row.kit_label}" if row.kit_label else "")
        choices[f"{key[0]}:{key[1] or ''}"] = label
    return sorted(choices.items(), key=lambda p: (p[1], p[0]))[:MAX_CHOICES]


def _assignees(organization_id, search=""):
    """([(user id, name)], more) for the assignment selector: active users currently holding Crew
    (or EMT), Supervisor or Supply Officer, by name, at most MAX_CHOICES; `more` says the list was
    cut short. One query: a current grant is a GRANT row no REVOKE row answers (prior_grant), the
    rule the role replay applies. The assignment command checks the chosen person again.
    """
    held = RoleGrant.objects.filter(
        organization_id=organization_id,
        user_id=OuterRef("pk"),
        action="GRANT",
        role__in=[*(r.value for r in ASSIGNEE_ROLES), Role.EMT.value],
    ).exclude(Exists(RoleGrant.objects.filter(prior_grant_id=OuterRef("pk"))))
    users = User.objects.filter(organization_id=organization_id, active=True).filter(Exists(held))
    if search:
        users = users.filter(display_name__icontains=search)
    found = list(
        users.order_by("display_name", "id").values_list("id", "display_name")[: MAX_CHOICES + 1]
    )
    return [(str(pk), name) for pk, name in found[:MAX_CHOICES]], len(found) > MAX_CHOICES


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


def _supervisor_controls(user, found, status, roles, context, deadline, assignee_search=""):
    """What the supervisor and administrator forms need; None for a form the user cannot use.

    Visibility only: each command rechecks the permission, the status and the expected event.
    """
    org, deficiency_id, now = str(user.organization_id), str(found.pk), timezone.now()
    active = status.status in ACTIVE_STATUSES

    def may(action):
        return permitted(action, str(user.pk), roles, context=context)

    controls = {
        "assignees": None,
        "assignees_more": False,
        "assignee_search": "",
        "can_set_deadline": False,
        "can_start_work": False,
        "pending_extensions": [],
        "acknowledgement": None,
        "severities": None,
        "verification_items": None,
        "expanding": False,
        "suspension_items": None,
        "can_end_suspensions": False,
        "can_close": False,
        "can_reopen": False,
    }
    if active and may("assign_deficiency_deadline"):
        controls["assignees"], controls["assignees_more"] = _assignees(org, assignee_search)
        controls["assignee_search"] = assignee_search
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
        controls["verification_items"] = _item_choices(org, str(found.vehicle_id))
        controls["expanding"] = status.status == "PENDING_VERIFICATION"
    if active and may("suspend_routine_checks"):
        scope = {
            (item_id, str(kit_id) if kit_id else None)
            for item_id, _definition, kit_id in service.correction_scope(org, found, now)
        }
        controls["suspension_items"] = _item_choices(org, str(found.vehicle_id), scope)
        controls["can_end_suspensions"] = True  # one at a time, beside each in force (SUS-5)
    if may("explicit_deficiency_close"):
        controls["can_close"] = active
        controls["can_reopen"] = status.status in (CLOSED_ADMINISTRATOR, CLOSED_UNVERIFIABLE)
    # A form is offered when its value is set; an empty choice list still shows its form.
    controls["shown"] = bool(controls["pending_extensions"]) or any(
        value is not None and value is not False
        for key, value in controls.items()
        if key not in ("pending_extensions", "expanding", "assignees_more", "assignee_search")
    )
    return controls


@page
def deficiency(request, user, deficiency_id):
    found = (
        Deficiency.objects.filter(organization_id=user.organization_id, pk=_identity(deficiency_id))
        .select_related(
            "vehicle",
            "reporter",
            "severity_version",
            "category_version",
            "item",
            "kit",
            "compartment",
        )
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
    # The server refuses a past follow-up (INVALID_FOLLOW_UP), so a passed one is not prefilled.
    follow_up = dependency.get("follow_up_at") if dependency else None
    follow_up_passed = follow_up is not None and follow_up <= timezone.now()
    scope = verification_scope(found) if status.status == "PENDING_VERIFICATION" else []
    verification_progress = {
        "total": len(scope),
        "verified": sum(row.satisfied for row in scope),
        "outstanding": sum(not row.satisfied for row in scope),
    }
    suspensions = _suspensions(str(user.organization_id), str(found.pk))
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
            # With no deadline the server answers NO_DEADLINE, so the form is not offered.
            # Issue 21: Supervisors and Administrators, only while the closure is a valid move.
            "can_close_empty_scope": status.status in EMPTY_SCOPE_CLOSURE_STATUSES
            and allowed("close_empty_scope_deficiency"),
            "can_request_extension": allowed("request_extension")
            and deadline["governing_deadline"] is not None,
            "can_update_dependency": allowed("update_external_dependency"),
            "can_add_note": allowed("add_progress_note"),
            "can_enter_dependency": status.status in ("OPEN", "IN_PROGRESS"),
            "dependency": dependency,
            "follow_up_prefill": None if follow_up_passed else follow_up,
            "follow_up_passed": follow_up if follow_up_passed else None,
            "deadline": deadline,
            "age_days": deadline["age_seconds"] // 86400,
            "assignee": status.assignee,
            "scope": scope,
            "verification_progress": verification_progress,
            "suspensions": suspensions,
            "active_suspension_count": sum(not row.ended_at for row in suspensions),
            "supervisor": _supervisor_controls(
                user,
                found,
                status,
                roles,
                context,
                deadline,
                request.GET.get("assignee_search", "").strip()[:100],
            ),
            "config": {
                "correction_url": reverse("api-deficiency-corrections", args=[found.pk]),
                "expected_event_id": str(status.last_applied_event_id),
            },
        },
    )


def _queue_rows(request, projections, *, filter_query, key):
    page_data = _paginated_page(request, projections, filter_query=filter_query, key=key)
    for row in page_data["rows"]:
        row.status_label = STATUS_LABELS[row.status]
        row.queue_action = {
            "pending": "Decide the requested deadline change",
            "overdue": "Review the current deadline",
            "severity": "Review the active high-severity issue",
            "unassigned": "Assign an owner",
        }[key]
    return page_data


@page
def queue(request, user):
    """Prioritized, searchable active deficiencies for Supervisors and Administrators."""
    roles = roles_for(user)
    if not permitted("decide_extension", str(user.pk), roles):
        return deny_section(request, user, roles, "VIEW_DEFICIENCY_QUEUE")
    org, now = str(user.organization_id), timezone.now()
    severity_levels = _severity_levels(org, now)
    active = DeficiencyProjection.objects.filter(organization_id=org).select_related(
        "deficiency__vehicle", "assignee", "severity_version"
    )
    active, filter_context = _deficiency_filters(
        request,
        org,
        active,
        now=now,
        allowed_statuses=ACTIVE_STATUSES,
        severity_options=[
            (
                level.code,
                f"{level.label} ({level.code})"
                if level.active
                else f"{level.label} ({level.code}, retired)",
            )
            for level in severity_levels
        ],
    )
    active = active.filter(status__in=ACTIVE_STATUSES)
    active = active.order_by("-deficiency__reported_at", "-deficiency_id")

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
    pending = (
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
    late = overdue.candidates(org, now).filter(pk__in=active.values("pk"))
    late = late.select_related("deficiency__vehicle", "assignee", "severity_version").order_by(
        "governing", "deficiency_id"
    )
    top_levels = top_active_levels({level.code: level for level in severity_levels})
    severity_threshold = min((level.rank for level in top_levels), default=None)
    top_codes = {
        level.code
        for level in severity_levels
        if severity_threshold is not None and level.rank >= severity_threshold
    }
    severity_label = " / ".join(
        dict.fromkeys(
            level.label for level in sorted(top_levels, key=lambda level: (-level.rank, level.code))
        )
    )
    severity_priority = Case(
        *[
            When(severity_version__code=level.code, then=position)
            for position, level in enumerate(
                sorted(severity_levels, key=lambda level: (-level.rank, level.code))
            )
        ],
        default=len(severity_levels),
        output_field=IntegerField(),
    )
    high_severity = (
        active.filter(severity_version__code__in=top_codes)
        .annotate(severity_priority=severity_priority)
        .order_by("severity_priority", "deficiency__reported_at", "deficiency_id")
    )
    unassigned = active.filter(assignee__isnull=True).order_by(
        "deficiency__reported_at", "deficiency_id"
    )
    groups = [
        ("pending", "Extension requests waiting for a decision", pending),
        ("overdue", "Overdue", late),
    ]
    if top_codes:
        groups.append(
            (
                "severity",
                f"Top configured severity: {severity_label}",
                high_severity,
            )
        )
    groups.append(("unassigned", "Unassigned", unassigned))
    lists = [
        {
            "heading": heading,
            "page": _queue_rows(
                request,
                rows.select_related("deficiency__vehicle", "assignee", "severity_version"),
                filter_query=filter_context["filter_query"],
                key=key,
            ),
        }
        for key, heading, rows in groups
    ]
    return render(
        request,
        "inspections/deficiency_queue.html",
        {
            "title": "Supervisor queue",
            "lists": lists,
            "filter_action": reverse("deficiency-queue"),
            "filter_all_statuses_label": "All active statuses",
            "filter_total_label": "matching active deficiencies",
            "pagination_label": "Supervisor queue result pages",
            **filter_context,
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
    current = (
        DeficiencyProjection.objects.filter(organization_id=user.organization_id, deficiency=found)
        .values_list("status", flat=True)
        .first()
    )
    if current is None:
        raise ValueError("deficiency projection unavailable")
    if current != "PENDING_VERIFICATION":
        # A settled state, not an outage: say so instead of the 503 "temporarily unavailable" page.
        return render(
            request,
            "inspections/verification_not_pending.html",
            {
                "title": "Not awaiting verification",
                "deficiency": found,
                "status": STATUS_LABELS[current],
            },
            status=409,
        )
    # This is an independent check screen: no session is created or impersonated.
    verification_scope(found)
    config = {
        "vehicle_id": str(found.vehicle_id),
        "time_zone": user.organization.station_zone,
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
    authors = check_authors(str(user.organization_id), rows)
    return JsonResponse(
        {
            "status": "OK",
            "vehicle_status": result.vehicle_status,
            "items": [checklist_row(r, authors.get(r.latest_check_id)) for r in rows],
            "verification_status": STATUS_LABELS[events[-1].to_status],
            "remaining_scope_rows": len(needed),
            "unavailable_scope_rows": len(needed - {(r.item_id, r.kit_id or "") for r in rows}),
        }
    )
