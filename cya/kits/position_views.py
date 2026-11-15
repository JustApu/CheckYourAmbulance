"""Compartments and systems screens (SPEC 4.8 CFG-2, CFG-6; issue #130 part 5b).

Supervisors and Administrators (manage_vehicle_positions, PERM-46) add, rename, reorder and
retire a vehicle's positions. Anyone else gets 403 and an audited denial. Every change is a
form post with an operation id rendered into the form, so a resubmitted form replays rather
than repeats. Retiring is two steps (CFG-6): the preview lists what still uses the position,
which blocks it, or asks for confirmation; the command checks again under the vehicle lock.
"""

import logging
import uuid
from urllib.parse import urlencode

from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from cya.accounts.returns import sign_in_redirect
from cya.checklists.models import Vehicle
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import positions as position_rules
from cya.kits.keys import KeyRuleError
from cya.kits.positions import PositionError
from cya.kits.views import UNAVAILABLE, _can, _context_ids
from cya.shell import navigation
from cya.shell.views import deny_section

logger = logging.getLogger(__name__)

ACTION = "manage_vehicle_positions"
LIST_LIMIT = 500
KINDS = (("COMPARTMENT", "Compartments"), ("SYSTEM", "Systems"))
SAVED = {
    "add": "Position added.",
    "revise": "Position saved.",
    "retire": "Position retired. It is kept in the history and is no longer used.",
}


def _allowed(request):
    """(user, roles) when the user may manage positions, else the response to return."""
    user = request.cya_user
    if user is None:
        return None, sign_in_redirect(request)
    roles = navigation.roles_for(user)
    if not _can(user, roles, ACTION):
        return None, deny_section(request, user, roles, "MANAGE_VEHICLE_POSITIONS")
    return (user, roles), None


@never_cache
@require_GET
def vehicles(request):
    allowed, refused = _allowed(request)
    if refused:
        return refused
    user, _roles = allowed
    found = list(
        Vehicle.objects.filter(organization_id=user.organization_id)
        .order_by("-active", "display_name", "id")
        .values("id", "display_name", "number", "key", "active")[:LIST_LIMIT]
    )
    context = {"title": "Compartments and systems", "vehicles": found, "list_limit": LIST_LIMIT}
    return render(request, "kits/position_vehicles.html", context)


def _vehicle(user, vehicle_id):
    vehicle = Vehicle.objects.filter(organization_id=user.organization_id, pk=vehicle_id).first()
    if vehicle is None:
        raise Http404("no such vehicle")
    return vehicle


def _sections(user, vehicle):
    sections = []
    for kind, heading in KINDS:
        rows = position_rules.positions(str(user.organization_id), str(vehicle.pk), kind)
        sections.append(
            {
                "kind": kind,
                "heading": heading,
                "rows": [
                    {
                        "slot": str(row.stable_slot_id),
                        "label": row.label,
                        "key": key,
                        "display_order": row.display_order,
                        "retired": row.retired,
                        "sealable": getattr(row, "sealable", False),
                        "operation_id": str(uuid.uuid4()),
                    }
                    for row, key in rows
                ],
            }
        )
    return sections


def _page(request, user, vehicle, *, status=200, errors=None, retiring=None, form=None):
    context = {
        "title": f"Compartments and systems: {vehicle.display_name}",
        "vehicle": vehicle,
        "sections": _sections(user, vehicle),
        "kinds": KINDS,
        "add_operation_id": str(uuid.uuid4()),
        "errors": errors or [],
        "retiring": retiring,
        "form": form or {},
        "saved": SAVED.get(request.GET.get("saved", "")),
    }
    return render(request, "kits/positions.html", context, status=status)


def _operation_id(request):
    """The form's operation id, only when it is exactly a canonical UUID v4 (PR #170 review).

    UUID(value, version=4) would rewrite another version's bits into a different v4 id, so
    several submitted strings could map to one idempotency key; anything else is refused.
    """
    raw = request.POST.get("operation_id", "")
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    return raw if parsed.version == 4 and str(parsed) == raw else None


def _display_order(value):
    """None for a blank field, else the whole number; a PositionError otherwise."""
    value = (value or "").strip()
    if not value:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 6:
        raise PositionError("order must be a whole number")
    return int(value)


def _retire_preview(request, user, vehicle):
    kind, slot = request.POST.get("kind"), request.POST.get("slot", "")
    row = next(
        (
            r
            for s in _sections(user, vehicle)
            if s["kind"] == kind
            for r in s["rows"]
            if r["slot"] == slot and not r["retired"]
        ),
        None,
    )
    if row is None:
        return _page(request, user, vehicle, status=400, errors=["choose a position to retire"])
    blockers = position_rules.retired_blockers(
        str(user.organization_id), str(vehicle.pk), kind, slot, timezone.now()
    )
    retiring = {"kind": kind, "row": row, "blockers": blockers}
    return _page(request, user, vehicle, retiring=retiring)


def _command(request, user, vehicle, stage):
    operation_id = _operation_id(request)
    if operation_id is None:
        return _page(
            request, user, vehicle, status=400, errors=["the form was not valid; try again"]
        )
    ids = {**_context_ids(user), "operation_id": operation_id}
    kind = request.POST.get("kind")
    form = {
        "kind": kind,
        "label": request.POST.get("label", ""),
        "key": request.POST.get("key", ""),
        "display_order": request.POST.get("display_order", ""),
    }
    try:
        if stage == "add":
            position_rules.add_position(
                vehicle_id=str(vehicle.pk),
                kind=kind,
                label=form["label"],
                key=form["key"].strip(),
                display_order=_display_order(form["display_order"]),
                **ids,
            )
        elif stage == "revise":
            position_rules.revise_position(
                vehicle_id=str(vehicle.pk),
                kind=kind,
                stable_slot_id=request.POST.get("slot", ""),
                label=form["label"],
                display_order=_display_order(form["display_order"]),
                **ids,
            )
        else:
            position_rules.revise_position(
                vehicle_id=str(vehicle.pk),
                kind=kind,
                stable_slot_id=request.POST.get("slot", ""),
                retired=True,
                **ids,
            )
    except (PositionError, KeyRuleError, ValueError) as error:
        keep = form if stage == "add" else None
        return _page(request, user, vehicle, status=400, errors=[str(error)], form=keep)
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return _page(
            request,
            user,
            vehicle,
            status=409,
            errors=["this form was already used for a different change; try again"],
        )
    except UNAVAILABLE as error:
        logger.error("position change unavailable (%s)", type(error).__name__)
        return _page(
            request,
            user,
            vehicle,
            status=503,
            errors=["the change could not be completed; nothing was changed. Try again."],
        )
    url = reverse("vehicle-positions", args=[vehicle.pk])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": stage}))


@never_cache
@require_http_methods(["GET", "POST"])
def vehicle_positions(request, vehicle_id):
    allowed, refused = _allowed(request)
    if refused:
        return refused
    user, _roles = allowed
    vehicle = _vehicle(user, vehicle_id)
    if request.method == "GET":
        return _page(request, user, vehicle)
    stage = request.POST.get("stage")
    if stage == "retire-preview":
        return _retire_preview(request, user, vehicle)
    if stage in ("add", "revise", "retire"):
        return _command(request, user, vehicle, stage)
    return _page(request, user, vehicle, status=400, errors=["the request was not valid"])
