"""Compartments and systems screens (SPEC 4.8 CFG-2, CFG-6; issue #130 part 5b).

Supervisors and Administrators (manage_vehicle_positions, PERM-46) add, rename, reorder and
retire a vehicle's positions. Anyone else gets 403 and an audited denial. Every change is a
form post with an operation id rendered into the form, so a resubmitted form replays rather
than repeats. Retiring is two steps (CFG-6): the preview lists what still uses the position,
which blocks it, or asks for confirmation; the command checks again under the vehicle lock.
An Administrator (set_configuration_keys) may also set a missing key once (issue #238 F1c),
through the existing audited key command, which checks that permission itself.
"""

import logging
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from cya.authorization.policy import permitted
from cya.checklists.ownership import cutover_at
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits import keys as key_rules
from cya.kits import positions as position_rules
from cya.kits.keys import KeyRuleError
from cya.kits.positions import PositionError
from cya.kits.views import UNAVAILABLE, _context_ids
from cya.shell import navigation

logger = logging.getLogger(__name__)

ACTION = "manage_vehicle_positions"
DENIAL = "MANAGE_VEHICLE_POSITIONS"
KINDS = (("COMPARTMENT", "Compartments"), ("SYSTEM", "Systems"))
# Issue #238 F1a: the page speaks of compartments and systems, never "positions".
SAVED = {
    "add": "Compartment or system added.",
    "revise": "Compartment or system saved.",
    "retire": "Compartment or system retired. It is kept in the history and is no longer used.",
    "set-key": "Key set. It can't be changed later.",
}


@never_cache
@require_GET
def vehicles(request):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    return admin_pages.vehicle_choice(
        request,
        user,
        title="Compartments and systems",
        intro="Choose a vehicle to add, rename, reorder or retire its compartments and systems.",
        url_name="vehicle-positions",
    )


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
                        "operation_id": admin_pages.new_operation_id(),
                        "key_operation_id": admin_pages.new_operation_id(),
                    }
                    for row, key in rows
                ],
            }
        )
    return sections


def _page(request, user, vehicle, *, status=200, errors=None, retiring=None, form=None):
    organization_id = str(user.organization_id)
    context = {
        "title": f"Compartments and systems: {vehicle.display_name}",
        "vehicle": vehicle,
        "sections": _sections(user, vehicle),
        "kinds": KINDS,
        "add_operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "retiring": retiring,
        "form": form or {},
        "saved": SAVED.get(request.GET.get("saved", "")),
        # F1b: after the 5.0.0 cutover a key is this vehicle's own (IMP-6); before it, shared.
        "local_keys": cutover_at(organization_id) is not None,
        # F1c: only an Administrator may set a missing key; the command checks it again.
        "can_set_keys": permitted(key_rules.ACTION, str(user.pk), navigation.roles_for(user)),
    }
    return render(request, "kits/positions.html", context, status=status)


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
    operation_id = admin_pages.operation_id_from(request)
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
            result = position_rules.add_position(
                vehicle_id=str(vehicle.pk),
                kind=kind,
                label=form["label"],
                key=form["key"].strip(),
                display_order=_display_order(form["display_order"]),
                **ids,
            )
        elif stage == "revise":
            result = position_rules.revise_position(
                vehicle_id=str(vehicle.pk),
                kind=kind,
                stable_slot_id=request.POST.get("slot", ""),
                label=form["label"],
                display_order=_display_order(form["display_order"]),
                **ids,
            )
        elif stage == "set-key":
            result = key_rules.set_vehicle_position_key(
                vehicle_id=str(vehicle.pk),
                kind=kind,
                stable_slot_id=request.POST.get("slot", ""),
                key=form["key"].strip(),
                **ids,
            )
        else:
            result = position_rules.revise_position(
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
        return _page(request, user, vehicle, status=409, errors=[admin_pages.REUSED])
    except UNAVAILABLE as error:
        logger.error("position change unavailable (%s)", type(error).__name__)
        return _page(
            request,
            user,
            vehicle,
            status=503,
            errors=["the change could not be completed; nothing was changed. Try again."],
        )
    if result.operation_reused:
        return _page(request, user, vehicle, status=409, errors=[admin_pages.REUSED])
    url = reverse("vehicle-positions", args=[vehicle.pk])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": stage}))


@never_cache
@require_http_methods(["GET", "POST"])
def vehicle_positions(request, vehicle_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    vehicle = admin_pages.vehicle_of(user, vehicle_id)
    if request.method == "GET":
        return _page(request, user, vehicle)
    stage = request.POST.get("stage")
    if stage == "retire-preview":
        return _retire_preview(request, user, vehicle)
    if stage in ("add", "revise", "retire", "set-key"):
        return _command(request, user, vehicle, stage)
    return _page(request, user, vehicle, status=400, errors=["the request was not valid"])
