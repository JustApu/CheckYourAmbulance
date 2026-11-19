"""Vehicle crew screens (SPEC 15.4 ASG-2 to ASG-8, issue #202 part 2).

The crew section of a vehicle's page, the crew column of the vehicle list, and the "your truck"
card on Home all read cya.operations.assignments. Every change posts a plain form to `crew`,
which runs the audited command; hiding a control is only a convenience, because the command
decides who may act (SPEC 3.2). A form carries an "until" time of day in station time, and the
assignment ends the next time the clock reaches it (ASG-4: an end time entered every time, so
an overnight shift is entered as its morning end).
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from cya.accounts.models import User
from cya.authorization.policy import permitted
from cya.checklists.models import Vehicle
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits.views import UNAVAILABLE, _context_ids
from cya.operations import assignments
from cya.shell import navigation

logger = logging.getLogger(__name__)

EXTEND_WINDOW = timedelta(minutes=30)  # ASG-5: Home offers Extend from 30 minutes before the end
SAVED = {
    "assigned": "You're on this vehicle.",
    "added": "Added to the crew.",
    "moved": "Moved to this vehicle.",
    "extended": "Assignment extended.",
    "ended": "Assignment ended.",
}
ERRORS = {
    "END_IN_PAST": "That time has already passed.",
    "END_TOO_LATE": "That time is more than {limit} hours away.",
    "NOT_LATER": "Choose a time later than the current end.",
    "ALREADY_ASSIGNED": "Already on this vehicle. Extend instead.",
    "NOT_ASSIGNED": "That assignment has already ended.",
    "VEHICLE_INACTIVE": "This vehicle is inactive.",
    "PERSON_INACTIVE": "That person can't be assigned.",
    "NOT_FOUND": "That vehicle or person no longer exists.",
    "MALFORMED_REQUEST": "Enter a time such as 18:00, and a date only for a shift past tomorrow.",
    "END_PASSED": "That date and time have already passed.",
    "TIME_SKIPPED": "That time is skipped by the clock change tonight. Choose another time.",
}


def zone_of(user):
    try:
        return ZoneInfo(user.organization.station_zone)
    except Exception:  # an unknown zone falls back to UTC rather than failing the page
        return ZoneInfo("UTC")


class SkippedTime(ValueError):
    """The next time the station clock would read this time, it does not: the spring change."""


def _readings(day, parsed, zone):
    """The UTC instants at which the station clock reads `parsed` on `day` (0, 1 or 2)."""
    wall = datetime.combine(day, parsed)
    found = set()
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold)
        back = candidate.astimezone(UTC).astimezone(zone)
        if back.replace(tzinfo=None, fold=0) == wall:
            found.add(candidate.astimezone(UTC))
    return sorted(found)


def until_on(day_text, text, zone, now):
    """The end at time `text` on station date `day_text` (YYYY-MM-DD), for shifts past a day.

    PR #219 review: a time alone can only reach the next 24 hours, but the limit may be up to
    168. The earliest reading after now on that date is used; a time the clock skips that day
    raises SkippedTime; a date and time already past, or malformed input, gives None.
    """
    try:
        parsed = time.fromisoformat((text or "").strip())
        day = date.fromisoformat((day_text or "").strip())
    except ValueError:
        return None
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        return None
    readings = _readings(day, parsed, zone)
    if not readings:
        raise SkippedTime(text)
    ahead = [r for r in readings if r > now.astimezone(UTC)]
    return ahead[0].astimezone(zone) if ahead else None


def until_instant(text, zone, now):
    """The next instant after now when the station clock reads `text` (HH:MM), or None.

    Daylight saving time (PR #219 review): in the autumn hour that repeats, the earlier of the
    two readings that is still ahead is chosen, so the end is never already past. When the next
    day the clock would show the time skips it (the spring gap), SkippedTime is raised rather
    than silently moving the end. Instants are compared in UTC.
    """
    try:
        parsed = time.fromisoformat((text or "").strip())
    except ValueError:
        return None
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        return None
    now_utc = now.astimezone(UTC)
    local_day = now.astimezone(zone).date()
    for offset in range(3):
        day = local_day + timedelta(days=offset)
        wall = datetime.combine(day, parsed)
        readings = []
        for fold in (0, 1):
            candidate = wall.replace(tzinfo=zone, fold=fold)
            back = candidate.astimezone(UTC).astimezone(zone)
            if back.replace(tzinfo=None, fold=0) == wall:  # this wall time exists on that day
                readings.append(candidate.astimezone(UTC))
        ahead = sorted(r for r in set(readings) if r > now_utc)
        if ahead:
            return ahead[0].astimezone(zone)
        if not readings and wall > now.astimezone(zone).replace(tzinfo=None):
            raise SkippedTime(text)
    return None


def when(instant, zone, now):
    """'18:00', or 'Thu 06:00' when it is not today in station time."""
    local, today = instant.astimezone(zone), now.astimezone(zone).date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%a %H:%M")


def _names(organization_id, person_ids):
    return dict(
        User.objects.filter(organization_id=organization_id, pk__in=person_ids).values_list(
            "pk", "display_name"
        )
    )


def _person_label(name, user_pk, person_pk):
    return "You" if str(user_pk) == str(person_pk) else (name or "Unnamed user")


def crew_names(user, now=None):
    """{vehicle id: [names]} for the vehicle list's crew column (ASG-8)."""
    now = now or timezone.now()
    crew = assignments.current_crew(str(user.organization_id), at=now)
    names = _names(user.organization_id, [a.person_id for a in crew])
    by_vehicle = {}
    for a in crew:
        by_vehicle.setdefault(str(a.vehicle_id), []).append(
            _person_label(names.get(a.person_id), user.pk, a.person_id)
        )
    return by_vehicle


def mine(user, now=None):
    """The signed-in user's assignment for Home's "your truck" card, or None (ASG-7)."""
    now = now or timezone.now()
    current = assignments.current_assignment(str(user.organization_id), str(user.pk), at=now)
    if current is None:
        return None
    vehicle = Vehicle.objects.filter(pk=current.vehicle_id).first()
    zone = zone_of(user)
    return {
        "vehicle_id": str(current.vehicle_id),
        "vehicle": vehicle.display_name if vehicle else "",
        "url": reverse("status-vehicle", args=[current.vehicle_id]),
        "until": when(current.ends_at, zone, now),
        "ending_soon": current.ends_at - now <= EXTEND_WINDOW,
        "operation_id": admin_pages.new_operation_id(),
        "end_operation_id": admin_pages.new_operation_id(),
    }


def vehicle_choices(user):
    return list(
        Vehicle.objects.filter(organization_id=user.organization_id, active=True)
        .order_by("display_name", "pk")
        .values("id", "display_name", "number")[: admin_pages.LIST_LIMIT]
    )


def crew_context(user, vehicle, now=None):
    """The crew section of a vehicle's page (ASG-2, ASG-8)."""
    now = now or timezone.now()
    org, zone = str(user.organization_id), zone_of(user)
    roles = navigation.roles_for(user)
    actor = str(user.pk)
    can_any = permitted(assignments.ANY, actor, roles)
    everyone = assignments.current_crew(org, at=now)
    names = _names(user.organization_id, [a.person_id for a in everyone])
    vehicles = dict(
        Vehicle.objects.filter(organization_id=user.organization_id).values_list(
            "pk", "display_name"
        )
    )
    on = {str(a.person_id): a for a in everyone}
    my = on.get(actor)
    on_this = my is not None and str(my.vehicle_id) == str(vehicle.pk)
    crew = []
    for a in everyone:
        if str(a.vehicle_id) != str(vehicle.pk):
            continue
        person = str(a.person_id)
        crew.append(
            {
                "person_id": person,
                "name": _person_label(names.get(a.person_id), user.pk, person),
                "until": when(a.ends_at, zone, now),
                "is_me": person == actor,
                # ASG-2: yourself, anyone on this vehicle, or a Supervisor or Administrator.
                "can_change": person == actor or on_this or can_any,
                "extend_operation_id": admin_pages.new_operation_id(),
                "end_operation_id": admin_pages.new_operation_id(),
            }
        )
    people = []
    if vehicle.active and (on_this or can_any):
        users = (
            User.objects.filter(organization_id=user.organization_id, active=True)
            .exclude(pk=user.pk)
            .order_by("display_name", "pk")
            .values_list("pk", "display_name")[: admin_pages.LIST_LIMIT]
        )
        for pk, name in users:
            elsewhere = on.get(str(pk))
            if elsewhere is not None and str(elsewhere.vehicle_id) == str(vehicle.pk):
                continue
            note = ""
            if elsewhere is not None:
                # ASG-2: never hide someone who can be added; say where they are now.
                other = vehicles.get(elsewhere.vehicle_id, "another vehicle")
                note = f"on {other} until {when(elsewhere.ends_at, zone, now)}"
            people.append({"id": str(pk), "name": name or "Unnamed user", "note": note})
    history = []
    if permitted(assignments.HISTORY, actor, roles):
        events = assignments.assignment_history(org, vehicle_id=str(vehicle.pk), limit=20)
        who = _names(
            user.organization_id, {e.person_id for e in events} | {e.actor_user_id for e in events}
        )
        for e in events:
            history.append(
                {
                    "at": e.recorded_at,
                    "text": _history_text(e, who, vehicles, zone, now),
                }
            )
    return {
        "crew": crew,
        "crew_vehicle_active": vehicle.active,
        "crew_self_assign": vehicle.active and not on_this,
        "crew_self_moves_from": vehicles.get(my.vehicle_id) if my and not on_this else None,
        "crew_people": people,
        "crew_history": history,
        "crew_limit": assignments.assignment_limit_hours(org),
        "crew_self_operation_id": admin_pages.new_operation_id(),
        "crew_add_operation_id": admin_pages.new_operation_id(),
    }


def _history_text(event, who, vehicles, zone, now):
    person = who.get(event.person_id) or "Unnamed user"
    actor = who.get(event.actor_user_id) or "Unnamed user"
    by = "" if event.source == "SELF" else f" (by {actor})"
    vehicle = vehicles.get(event.vehicle_id, "a vehicle")
    if event.action == "ASSIGN":
        return f"{person} on {vehicle} until {when(event.ends_at, zone, now)}{by}"
    if event.action == "MOVE":
        source = vehicles.get(event.from_vehicle_id, "another vehicle")
        return f"{person} moved from {source} to {vehicle}{by}"
    if event.action == "EXTEND":
        return f"{person} extended until {when(event.ends_at, zone, now)}{by}"
    if event.end_reason == "VEHICLE_INACTIVE":
        return f"{person} ended: {vehicle} was deactivated"
    return f"{person} ended{by}"


def _back(request, vehicle_id, code=None, error=None):
    """Where a crew form returns: Home, or the vehicle page's crew section."""
    query = urlencode({k: v for k, v in (("crew", code), ("crew_error", error)) if v})
    if request.POST.get("next") == "home":
        return HttpResponseRedirect(reverse("home") + (f"?{query}" if query else ""))
    url = reverse("status-vehicle", args=[vehicle_id])
    return HttpResponseRedirect(url + (f"?{query}" if query else "") + "#crew")


def message_for(request, user):
    """(saved text, error text) for a page a crew form returned to."""
    saved = SAVED.get(request.GET.get("crew", ""))
    code = request.GET.get("crew_error", "")
    error = ERRORS.get(code)
    if error:
        error = error.format(limit=assignments.assignment_limit_hours(str(user.organization_id)))
    return saved, error


@never_cache
@require_POST
def crew(request):
    """Assign, add or move, extend or end, from a vehicle page or Home (ASG-2 to ASG-5)."""
    user = request.cya_user
    if user is None:
        return admin_pages.sign_in_redirect(request)
    if not navigation.roles_for(user):
        raise Http404("no such page")
    stage = request.POST.get("stage", "")
    vehicle_id = request.POST.get("vehicle_id", "")[:36]
    vehicle = Vehicle.objects.filter(organization_id=user.organization_id, pk=vehicle_id).first()
    if vehicle is None:
        raise Http404("no such vehicle")
    person_id = request.POST.get("person_id", "")[:36] or str(user.pk)
    operation_id = admin_pages.operation_id_from(request)
    if operation_id is None or stage not in ("assign", "extend", "end"):
        return _back(request, vehicle.pk, error="MALFORMED_REQUEST")
    ids = {**_context_ids(user), "operation_id": operation_id}
    until = None
    if stage != "end":
        try:
            day = request.POST.get("until_date", "").strip()
            until = (
                until_on(day, request.POST.get("until"), zone_of(user), timezone.now())
                if day
                else until_instant(request.POST.get("until"), zone_of(user), timezone.now())
            )
        except SkippedTime:
            return _back(request, vehicle.pk, error="TIME_SKIPPED")
        if until is None:
            day = request.POST.get("until_date", "").strip()
            code = "MALFORMED_REQUEST"
            try:
                if day and time.fromisoformat(request.POST.get("until", "").strip()):
                    date.fromisoformat(day)
                    code = "END_PASSED"
            except ValueError:
                pass
            return _back(request, vehicle.pk, error=code)
    try:
        if stage == "assign":
            result = assignments.assign_vehicle(
                person_id=person_id, vehicle_id=str(vehicle.pk), ends_at=until, **ids
            )
            moved = result.body["code"] == "MOVE"
            code = "moved" if moved else ("assigned" if person_id == str(user.pk) else "added")
        elif stage == "extend":
            assignments.extend_assignment(person_id=person_id, ends_at=until, **ids)
            code = "extended"
        else:
            assignments.end_assignment(person_id=person_id, **ids)
            code = "ended"
    except assignments.AssignmentError as error:
        return _back(request, vehicle.pk, error=error.code)
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return _back(request, vehicle.pk, error="MALFORMED_REQUEST")
    except UNAVAILABLE as error:
        logger.error("crew change unavailable (%s)", type(error).__name__)
        return render(
            request,
            "inspections/workflow_unavailable.html",
            {"title": "Crew temporarily unavailable", "unavailable_content": "Vehicle crew"},
            status=503,
        )
    return _back(request, vehicle.pk, code=code)


LIMIT_DENIAL = "CONFIGURE_ASSIGNMENT_LIMIT"


@never_cache
@require_http_methods(["GET", "POST"])
def limit_settings(request):
    """Administrator sets the longest an assignment may run (ASG-4, OQ-79)."""
    allowed, refused = admin_pages.allowed(request, assignments.LIMIT_ACTION, LIMIT_DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    org = str(user.organization_id)

    def page(status=200, errors=None, saved=None):
        context = {
            "title": "Crew assignments",
            "max_hours": assignments.assignment_limit_hours(org),
            "operation_id": admin_pages.new_operation_id(),
            "errors": errors or [],
            "saved": saved,
        }
        return render(request, "operations/crew_settings.html", context, status=status)

    if request.method == "GET":
        return page(saved="Saved." if request.GET.get("saved") else None)
    operation_id = admin_pages.operation_id_from(request)
    raw = request.POST.get("max_hours", "").strip()
    if operation_id is None or not raw.isdigit():
        return page(400, ["Enter a whole number of hours from 1 to 168."])
    try:
        assignments.set_assignment_limit(
            max_hours=int(raw), **{**_context_ids(user), "operation_id": operation_id}
        )
    except assignments.AssignmentError as error:
        message = (
            "That is already the limit."
            if error.code == "NO_CHANGE"
            else "Enter a whole number of hours from 1 to 168."
        )
        return page(400, [message])
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return page(409, ["This form was already used; try again."])
    except UNAVAILABLE as error:
        logger.error("assignment limit unavailable (%s)", type(error).__name__)
        return page(503, ["The change could not be completed; nothing was changed. Try again."])
    return HttpResponseRedirect(reverse("crew-assignment-settings") + "?saved=1")
