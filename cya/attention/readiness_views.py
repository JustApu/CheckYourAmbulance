"""Fleet readiness endpoint and screen for Supervisors and Administrators (issue 17).

GET /api/v1/fleet-readiness[?as_of=]   the picture as JSON
GET /fleet/readiness/[?as_of=]         the screen
GET /fleet/readiness/panel/            the screen's live part, which the open page re-reads

Read-only. Crew and Supply Officers are refused server-side (403; the screen audits the refusal
like any refused section). as_of answers for a past instant (AC-27); without it the answer is
for now and the screen stays live.
"""

import logging
import threading
import uuid
from concurrent.futures import Future
from datetime import datetime
from time import monotonic

from django.db import DatabaseError
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from cya.attention.readiness import CATEGORIES, fleet_readiness
from cya.authorization.policy import Role
from cya.checks.api import _reply
from cya.deficiencies.api import _stamp
from cya.deficiencies.service import DeficiencyError
from cya.inspections.views import STATUS_LABELS as VEHICLE_STATUS_LABELS
from cya.inspections.views import _authorized, reverse_socket
from cya.inspections.workflow_views import STATUS_LABELS
from cya.shell.navigation import roles_for
from cya.shell.views import deny_section

logger = logging.getLogger(__name__)
READERS = frozenset({Role.SUPERVISOR.value, Role.ADMINISTRATOR.value})
LIVE_SOCKETS = 3  # one fewer than the default per-user socket limit, leaving room for one tab
COALESCED_READ_WAIT_SECONDS = 20  # return before the issue #70 rehearsal client's 30 s timeout
_CURRENT_READS_CHANGED = threading.Condition()
_CURRENT_READS = {}


class ReadinessWaitExpired(Exception):
    """A coalesced readiness request waited too long behind another calculation."""


class _ReadGeneration:
    def __init__(self, *, claimed=False):
        self.future = Future()
        self.claimed = claimed
        self.waiters = 0


class _ReadFlight:
    def __init__(self, generation):
        self.current = generation
        self.next = None


def _current_read(key, calculate):
    """Coalesce callers by calculation generation without returning an older in-flight result."""
    with _CURRENT_READS_CHANGED:
        flight = _CURRENT_READS.get(key)
        if flight is None:
            generation = _ReadGeneration(claimed=True)
            flight = _ReadFlight(generation)
            _CURRENT_READS[key] = flight
            owner = True
        else:
            if flight.current.claimed:
                if flight.next is None:
                    flight.next = _ReadGeneration()
                generation = flight.next
            else:
                generation = flight.current
            generation.waiters += 1
            owner = False

    deadline = monotonic() + COALESCED_READ_WAIT_SECONDS
    while not owner:
        with _CURRENT_READS_CHANGED:
            while not generation.future.done():
                if flight.current is generation and not generation.claimed:
                    generation.waiters -= 1
                    generation.claimed = True
                    owner = True
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    generation.waiters -= 1
                    if generation.waiters == 0:
                        if flight.next is generation:
                            flight.next = None
                        elif flight.current is generation and not generation.claimed:
                            if _CURRENT_READS.get(key) is flight:
                                del _CURRENT_READS[key]
                    _CURRENT_READS_CHANGED.notify_all()
                    raise ReadinessWaitExpired
                _CURRENT_READS_CHANGED.wait(timeout=remaining)
            if not owner:
                generation.waiters -= 1
                return generation.future.result()

    try:
        result = calculate()
    except BaseException as failure:
        with _CURRENT_READS_CHANGED:
            generation.future.set_exception(failure)
            if flight.current is generation:
                if flight.next is None:
                    if _CURRENT_READS.get(key) is flight:
                        del _CURRENT_READS[key]
                else:
                    flight.current = flight.next
                    flight.next = None
            _CURRENT_READS_CHANGED.notify_all()
        raise

    with _CURRENT_READS_CHANGED:
        generation.future.set_result(result)
        if flight.current is generation:
            if flight.next is None:
                if _CURRENT_READS.get(key) is flight:
                    del _CURRENT_READS[key]
            else:
                flight.current = flight.next
                flight.next = None
        _CURRENT_READS_CHANGED.notify_all()
    return result


def _as_of(request):
    """(instant or None, error) from ?as_of=, the only query parameter."""
    if set(request.GET) - {"as_of"} or len(request.GET.getlist("as_of")) > 1:
        return None, "INVALID_FIELDS"
    text = request.GET.get("as_of")
    if text is None:
        return None, None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None, "INVALID_AS_OF"
    if value.utcoffset() is None or value > timezone.now():
        return None, "INVALID_AS_OF"
    return value, None


def _picture(user, as_of, *, consumer):
    organization_id = str(user.organization_id)
    if as_of is not None:
        return fleet_readiness(organization_id, as_of=as_of, now=timezone.now())

    # Current readiness is identical for every authorized reader in one organization. When
    # several supervisors refresh together, let them share the in-flight calculation; do not
    # retain a completed picture, so later requests always read current state again.
    # Keep API and HTML screen work separate because the screen adds display-only fields to it.
    key = (organization_id, consumer)
    return _current_read(
        key,
        lambda: fleet_readiness(organization_id, now=timezone.now()),
    )


def _json_vehicle(vehicle):
    def stamp(value):
        return None if value is None else _stamp(value)

    return {
        "vehicle_id": vehicle.vehicle_id,
        "display_name": vehicle.display_name,
        "active": vehicle.active,
        "status": vehicle.status,
        "restrictions": vehicle.restrictions,
        "obligations": vehicle.counts,
        "progress": vehicle.progress,
        "deficiencies": [
            {
                **row,
                "severity": row["severity"]["code"],
                "severity_label": row["severity"]["label"],
                "governing_deadline": stamp(row["governing_deadline"]),
                "follow_up_at": stamp(row["follow_up_at"]),
                "acknowledged_until": stamp(row["acknowledged_until"]),
            }
            for row in vehicle.deficiencies
        ],
        "deficiencies_truncated": vehicle.deficiencies_truncated,
        "deficiencies_by_severity": vehicle.by_severity,
        "deficiencies_by_status": vehicle.by_status,
        "suspensions": [
            {**row, "started_at": stamp(row["started_at"])} for row in vehicle.suspensions
        ],
        "suspensions_truncated": vehicle.suspensions_truncated,
    }


@never_cache
@require_GET
def api(request):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    if not roles_for(user) & READERS:
        return _reply(403, "FORBIDDEN", correlation_id)
    as_of, error = _as_of(request)
    if error:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("as_of", error)])
    try:
        picture = _picture(user, as_of, consumer="api")
    except (DatabaseError, DeficiencyError, ReadinessWaitExpired, ValueError) as failure:
        logger.error("fleet readiness unavailable (%s)", type(failure).__name__)
        return _reply(503, "RETRY_LATER", correlation_id)
    return JsonResponse(
        {
            "status": "OK",
            "correlation_id": correlation_id,
            "as_of": _stamp(picture.as_of),
            "window_start": _stamp(picture.window_start),
            "window_end": _stamp(picture.window_end),
            "categories": [{"code": c, "label": label} for c, label in CATEGORIES],
            "vehicles": [_json_vehicle(v) for v in picture.vehicles],
            "vehicles_truncated": picture.truncated,
        }
    )


def _screen(request, template, background=False):
    user, refusal = _authorized(request, "fleet", background=background)
    if refusal is not None:
        return refusal
    roles = roles_for(user)
    if not roles & READERS:
        return deny_section(request, user, roles, "VIEW_FLEET_READINESS")
    as_of, error = _as_of(request)
    if error:
        raise Http404
    try:
        picture = _picture(user, as_of, consumer="screen")
    except (DatabaseError, DeficiencyError, ReadinessWaitExpired, ValueError) as failure:
        logger.error("fleet readiness unavailable (%s)", type(failure).__name__)
        return render(
            request,
            "inspections/workflow_unavailable.html",
            {
                "title": "Readiness temporarily unavailable",
                "unavailable_content": "The readiness picture",
            },
            status=503,
        )
    for vehicle in picture.vehicles:
        vehicle.status_label = VEHICLE_STATUS_LABELS.get(vehicle.status, vehicle.status)
        vehicle.states = [
            (label, vehicle.counts[code]) for code, label in CATEGORIES if vehicle.counts[code]
        ]
        for row in vehicle.deficiencies:
            row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
    live = as_of is None
    return render(
        request,
        template,
        {
            "title": "Fleet readiness",
            "picture": picture,
            "live": live,
            "config": {
                "panel_url": reverse("fleet-readiness-panel"),
                "sockets": [reverse_socket(v.vehicle_id) for v in picture.vehicles][:LIVE_SOCKETS]
                if live
                else [],
                "vehicle_count": len(picture.vehicles),
                "refresh_seconds": 60,
            },
        },
    )


@never_cache
@require_GET
def screen(request):
    return _screen(request, "attention/readiness.html")


@never_cache
@require_GET
def panel(request):
    return _screen(request, "attention/readiness_panel.html", background=True)
