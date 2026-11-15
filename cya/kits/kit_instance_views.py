"""Kit instance screens (SPEC 4.6 KDEF-3, 4.8 CFG-5; issue #130 part 5c).

Supervisors, Supply Officers and Administrators (manage_kit_assignment, PERM-32) see a
vehicle's active kit instances and add one with its kit definition, a compartment and a label.
Duplicating an instance (CFG-5) is an ordinary addition: it picks a target vehicle, then opens
that vehicle's add form with the same definition and a suggested label. No assignment, seal or
check history is copied.
"""

import logging
import uuid
from urllib.parse import urlencode

from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from cya.checklists.models import KitInstance, Vehicle
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits import positions as position_rules
from cya.kits.commands import KitError, add_kit_instance
from cya.kits.models import KitDefinition, KitInstanceDefinition
from cya.kits.views import UNAVAILABLE, _context_ids

logger = logging.getLogger(__name__)

ACTION = "manage_kit_assignment"
DENIAL = "MANAGE_KIT_ASSIGNMENT"
SAVED = {"add": "Kit added."}


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
        title="Kit instances",
        intro="Choose a vehicle to see and add its kits.",
        url_name="vehicle-kit-instances",
    )


def _kits(org, vehicle):
    kits = list(
        KitInstance.objects.filter(organization_id=org, vehicle=vehicle, active=True)
        .select_related("compartment")
        .order_by("label", "id")
    )
    definitions = dict(
        KitInstanceDefinition.objects.filter(kit__in=kits).values_list(
            "kit_id", "definition__label"
        )
    )
    definition_ids = dict(
        KitInstanceDefinition.objects.filter(kit__in=kits).values_list("kit_id", "definition_id")
    )
    return [
        {
            "id": str(kit.pk),
            "label": kit.label,
            "compartment": kit.compartment.label if kit.compartment else "",
            "definition": definitions.get(kit.pk, ""),
            "definition_id": str(definition_ids[kit.pk]) if kit.pk in definition_ids else "",
        }
        for kit in kits
    ]


def _page(request, user, vehicle, *, status=200, errors=None, form=None):
    org = str(user.organization_id)
    prefill = form or {
        "definition_id": request.GET.get("definition", ""),
        "label": request.GET.get("label", "")[:200],
    }
    context = {
        "title": f"Kits: {vehicle.display_name}",
        "vehicle": vehicle,
        "kits": _kits(org, vehicle),
        "definitions": list(
            KitDefinition.objects.filter(organization_id=org, active=True).order_by("label", "key")
        ),
        "compartments": [
            row
            for row, _key in position_rules.positions(org, str(vehicle.pk), "COMPARTMENT")
            if not row.retired
        ],
        "vehicles": list(
            Vehicle.objects.filter(organization_id=org)
            .order_by("-active", "display_name", "id")
            .values("id", "display_name")[: admin_pages.LIST_LIMIT]
        ),
        "form": prefill,
        "add_operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
    }
    return render(request, "kits/kit_instances.html", context, status=status)


def _add(request, user, vehicle):
    operation_id = admin_pages.operation_id_from(request)
    form = {
        "definition_id": request.POST.get("definition_id", ""),
        "compartment_id": request.POST.get("compartment_id", ""),
        "label": request.POST.get("label", ""),
    }
    if operation_id is None:
        return _page(
            request,
            user,
            vehicle,
            status=400,
            errors=["the form was not valid; try again"],
            form=form,
        )
    if not form["definition_id"]:
        return _page(request, user, vehicle, status=400, errors=["choose the kit type"], form=form)
    ids = {**_context_ids(user), "operation_id": operation_id}
    try:
        add_kit_instance(
            label=form["label"],
            vehicle_id=str(vehicle.pk),
            compartment_id=form["compartment_id"],
            definition_id=form["definition_id"],
            **ids,
        )
    except (KitError, ValueError) as error:
        return _page(request, user, vehicle, status=400, errors=[str(error)], form=form)
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return _page(
            request,
            user,
            vehicle,
            status=409,
            errors=["this form was already used for a different change; try again"],
            form=form,
        )
    except UNAVAILABLE as error:
        logger.error("kit addition unavailable (%s)", type(error).__name__)
        return _page(
            request,
            user,
            vehicle,
            status=503,
            errors=["the change could not be completed; nothing was changed. Try again."],
            form=form,
        )
    url = reverse("vehicle-kit-instances", args=[vehicle.pk])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": "add"}))


@never_cache
@require_http_methods(["GET", "POST"])
def vehicle_kit_instances(request, vehicle_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    vehicle = admin_pages.vehicle_of(user, vehicle_id)
    if request.method == "GET":
        return _page(request, user, vehicle)
    if request.POST.get("stage") == "add":
        return _add(request, user, vehicle)
    return _page(request, user, vehicle, status=400, errors=["the request was not valid"])


@never_cache
@require_GET
def duplicate(request):
    """CFG-5: open the chosen vehicle's add form with the source's definition and a label."""
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    try:
        vehicle_id = str(uuid.UUID(request.GET.get("vehicle", "")))
    except ValueError:
        raise Http404("no such vehicle") from None
    target = admin_pages.vehicle_of(user, vehicle_id)
    query = urlencode(
        {"definition": request.GET.get("definition", ""), "label": request.GET.get("label", "")}
    )
    return HttpResponseRedirect(
        reverse("vehicle-kit-instances", args=[target.pk]) + "?" + query + "#add-heading"
    )
