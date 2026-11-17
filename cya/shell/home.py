"""The signed-in Home screen (issue #194 items 3 and 11): what to do next, at a glance.

Everything here only reads. Each section is computed on its own, and a section whose reader
fails shows a short "unavailable" note instead of breaking the page; Home must always render,
because other pages and tests rely on it.

Home always covers every vehicle (owner decision, 2026-11-17); there is no per-person vehicle list.

Progress is about task completion only: no scores, rankings or comparisons between people.
"""

import logging

from django.db import DatabaseError
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from cya.shell import navigation

logger = logging.getLogger(__name__)

MAX_IN_PROGRESS = 3
MAX_PROBLEMS = 5
MAX_REQUESTS = 5
SUPERVISING = frozenset({navigation.SUPERVISOR, navigation.ADMINISTRATOR})


class Unavailable:
    """Marks a section whose data could not be read."""

    unavailable = True


def _guarded(name, read):
    from cya.deficiencies.service import DeficiencyError

    try:
        return read()
    except (DatabaseError, DeficiencyError, ValueError) as error:
        logger.error("home section %s unavailable (%s)", name, type(error).__name__)
        return Unavailable()


def first_name(user):
    name = (getattr(user, "display_name", "") or "").strip()
    return name.split()[0] if name else ""


def greeting(user, now):
    local = timezone.localtime(now)  # the station zone is active for signed-in requests
    hour = local.hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 17 else "evening"
    name = first_name(user)
    return {"text": f"Good {part}, {name}" if name else f"Good {part}", "now": now}


def in_progress(user, now):
    """The user's open sessions, most recently active first, with checked counts."""
    from cya.checks.models import CheckRecord
    from cya.inspections.models import SessionParticipant, SessionProjection
    from cya.inspections.projections import INACTIVITY

    org = user.organization_id
    mine = SessionParticipant.objects.filter(organization_id=org, user=user).values("session_id")
    found = (
        SessionProjection.objects.filter(
            organization_id=org,
            session__in=mine,
            state="OPEN",
            last_activity_at__gt=now - INACTIVITY,  # past this a session reads as Incomplete
        )
        .select_related("session__vehicle", "session__checklist_version__template")
        .order_by("-last_activity_at", "-pk")[:MAX_IN_PROGRESS]
    )
    rows = []
    for projection in found:
        session = projection.session
        counts = CheckRecord.objects.filter(organization_id=org, session=session).aggregate(
            checked=Count("root_check", distinct=True),
            by_others=Count("root_check", distinct=True, filter=~Q(actor_user=user)),
        )
        rows.append(
            {
                "vehicle": session.vehicle.display_name,
                "check": session.checklist_version.template.label,
                "checked": counts["checked"],
                "by_others": counts["by_others"],
                "last_activity_at": projection.last_activity_at,
                "url": reverse("inspection-session", args=[session.pk]),
            }
        )
    return rows


def _readiness(user, now):
    from cya.attention.readiness import fleet_readiness

    return fleet_readiness(str(user.organization_id), now=now)


def due_today(picture):
    """Per vehicle: overdue, due now, due soon and done, from the readiness picture."""
    rows = []
    for vehicle in picture.vehicles:
        counts = vehicle.counts
        rows.append(
            {
                "vehicle": vehicle.display_name,
                "url": reverse("inspection-vehicle", args=[vehicle.vehicle_id]),
                "overdue": counts.get("OVERDUE", 0) + counts.get("MISSED", 0),
                "due": counts.get("DUE", 0),
                "soon": counts.get("CURRENT", 0),
                "done": counts.get("PASSED", 0),
            }
        )
    return {"rows": rows, "truncated": picture.truncated}


def problems(user):
    from cya.deficiencies.models import DeficiencyProjection
    from cya.deficiencies.transitions import ACTIVE_STATUSES
    from cya.inspections.workflow_views import STATUS_LABELS

    found = DeficiencyProjection.objects.filter(
        organization_id=user.organization_id, status__in=ACTIVE_STATUSES
    )
    total = found.count()
    rows = [
        {
            "summary": row.deficiency.summary,
            "vehicle": row.deficiency.vehicle.display_name,
            "status": STATUS_LABELS.get(row.status, row.status),
            "severity": row.severity_version.label,
            "url": reverse("deficiencies-detail", args=[row.deficiency_id]),
        }
        for row in found.select_related("deficiency__vehicle", "severity_version").order_by(
            "-deficiency__reported_at", "-pk"
        )[:MAX_PROBLEMS]
    ]
    return {"rows": rows, "total": total}


def last_check(user):
    from cya.checks.models import CheckRecord

    record = (
        CheckRecord.objects.filter(organization_id=user.organization_id, actor_user=user)
        .select_related("vehicle")
        .order_by("-recorded_at", "-pk")
        .first()
    )
    if record is None:
        return None
    return {"vehicle": record.vehicle.display_name, "at": record.recorded_at}


def supervisor(user, picture):
    """Tiles, the vehicles table and the Needs-you list for Supervisors and Administrators."""
    from cya.checklists.models import Vehicle
    from cya.deficiencies.models import DeficiencyProjection
    from cya.deficiencies.transitions import ACTIVE_STATUSES, PENDING_VERIFICATION
    from cya.inspections.views import STATUS_LABELS as VEHICLE_STATUS
    from cya.operations.service import pending_requests

    org = user.organization_id
    # The readiness picture lists at most VEHICLE_LIMIT vehicles; the fleet total is counted.
    fleet = Vehicle.objects.filter(organization_id=org, active=True).count()
    active = DeficiencyProjection.objects.filter(organization_id=org, status__in=ACTIVE_STATUSES)
    counts = active.aggregate(
        open=Count("pk"),
        verifying=Count("pk", filter=Q(status=PENDING_VERIFICATION)),
        unassigned=Count("pk", filter=Q(assignee__isnull=True)),
    )
    per_vehicle = {
        str(vehicle_id): n
        for vehicle_id, n in active.values("deficiency__vehicle_id")
        .annotate(n=Count("pk"))
        .values_list("deficiency__vehicle_id", "n")
    }
    pending = pending_requests(str(org))
    vehicles = []
    ready = checked = owed = 0
    for vehicle in picture.vehicles:
        ready += vehicle.status == "IN_SERVICE"
        checked += vehicle.progress.get("checked", 0)
        owed += vehicle.progress.get("owed", 0)
        vehicles.append(
            {
                "vehicle": vehicle.display_name,
                "url": reverse("status-vehicle", args=[vehicle.vehicle_id]),
                "status": vehicle.status,
                "status_label": VEHICLE_STATUS.get(vehicle.status, vehicle.status),
                "checked": vehicle.progress.get("checked", 0),
                "owed": vehicle.progress.get("owed", 0),
                "overdue": vehicle.counts.get("OVERDUE", 0) + vehicle.counts.get("MISSED", 0),
                "problems": per_vehicle.get(str(vehicle.vehicle_id), 0),
            }
        )
    requests = [
        {
            "vehicle": row["vehicle"],
            "requested": VEHICLE_STATUS.get(row["requested_status"], row["requested_status"]),
            "url": reverse("status-vehicle", args=[row["vehicle_id"]]),
        }
        for row in pending["requests"][:MAX_REQUESTS]
    ]
    return {
        "tiles": {
            "ready": ready,
            "listed": len(picture.vehicles),
            "fleet": fleet,
            "partial": picture.truncated,
            "checked": checked,
            "owed": owed,
            "open_problems": counts["open"],
            "for_you": len(pending["requests"]) + counts["verifying"] + counts["unassigned"],
        },
        "vehicles": vehicles,
        "truncated": picture.truncated,
        "requests": requests,
        "requests_total": len(pending["requests"]),
        "verifying": counts["verifying"],
        "unassigned": counts["unassigned"],
    }


def build(user, now=None):
    """Context for the Home template; None for a user with no role (no sections)."""
    roles = navigation.roles_for(user)
    if not roles:
        return None
    now = now or timezone.now()
    picture = _guarded("readiness", lambda: _readiness(user, now))
    context = {
        "greeting": greeting(user, now),
        "in_progress": _guarded("in progress", lambda: in_progress(user, now)),
        "due": picture if isinstance(picture, Unavailable) else due_today(picture),
        "problems": _guarded("problems", lambda: problems(user)),
        "last_check": _guarded("last check", lambda: last_check(user)),
        "supervisor": None,
    }
    if roles & SUPERVISING:
        context["supervisor"] = (
            picture
            if isinstance(picture, Unavailable)
            else _guarded("supervisor", lambda: supervisor(user, picture))
        )
    return context
