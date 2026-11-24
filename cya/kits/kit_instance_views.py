"""Kit instance screens (SPEC 4.6 KDEF-3, 4.8 CFG-5; issue #130 part 5c).

Supervisors, Supply Officers and Administrators (manage_kit_assignment, PERM-32) see a
vehicle's active kit instances and add one with its kit definition, a compartment and a label.
Duplicating an instance (CFG-5) is an ordinary addition: it picks a target vehicle, then opens
that vehicle's add form with the same definition and a suggested label. No assignment, seal or
check history is copied.
"""

import logging
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from cya.checklists.models import KitInstance
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits import positions as position_rules
from cya.kits.commands import KitError, record_kit_assignment
from cya.kits.models import KitInstanceDefinition
from cya.kits.views import UNAVAILABLE, _context_ids
from cya.shell import navigation

logger = logging.getLogger(__name__)

ACTION = "manage_kit_assignment"
DENIAL = "MANAGE_KIT_ASSIGNMENT"
SAVED = {"assign": "Physical kit assigned.", "stock": "Physical kit returned to unassigned stock."}


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
        title="Assign physical kits",
        intro="Choose a vehicle to assign an existing physical kit from stock.",
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
            "operation_id": admin_pages.new_operation_id(),
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
        "kit_id": request.POST.get("kit_id", ""),
        "compartment_id": request.POST.get("compartment_id", ""),
    }
    stock = list(
        KitInstance.objects.filter(organization_id=org, active=True, vehicle__isnull=True)
        .order_by("label", "id")
    )
    stock_definitions = dict(
        KitInstanceDefinition.objects.filter(kit__in=stock).values_list(
            "kit_id", "definition__label"
        )
    )
    roles = navigation.roles_for(user)
    context = {
        "title": f"Physical kit assignments: {vehicle.display_name}",
        "can_manage_positions": permitted("manage_vehicle_positions", str(user.pk), roles),

        "vehicle": vehicle,
        "kits": _kits(org, vehicle),
        "stock_kits": [
            {"id": str(k.pk), "label": k.label, "definition": stock_definitions.get(k.pk, "(no kit type)")}
            for k in stock
        ],
        "compartments": [
            row
            for row, _key in position_rules.positions(org, str(vehicle.pk), "COMPARTMENT")
            if not row.retired
        ],
        "form": prefill,
        "add_operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
    }
    return render(request, "kits/kit_instances.html", context, status=status)


def _assign(request, user, vehicle):
    operation_id = admin_pages.operation_id_from(request)
    form = {
        "kit_id": request.POST.get("kit_id", ""),
        "compartment_id": request.POST.get("compartment_id", ""),
    }
    if operation_id is None:
        return _page(request, user, vehicle, status=400, errors=["the form was not valid; try again"], form=form)
    if not form["kit_id"] or not form["compartment_id"]:
        return _page(request, user, vehicle, status=400, errors=["choose a physical kit and compartment"], form=form)
    try:
        record_kit_assignment(
            kit_id=form["kit_id"],
            active=True,
            vehicle_id=str(vehicle.pk),
            compartment_id=form["compartment_id"],
            **{**_context_ids(user), "operation_id": operation_id},
        )
    except (KitError, ValueError) as error:
        return _page(request, user, vehicle, status=400, errors=[str(error)], form=form)
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return _page(request, user, vehicle, status=409, errors=["this form was already used for a different change; try again"], form=form)
    except UNAVAILABLE as error:
        logger.error("kit assignment unavailable (%s)", type(error).__name__)
        return _page(request, user, vehicle, status=503, errors=["the change could not be completed; nothing was changed. Try again."], form=form)
    url = reverse("vehicle-kit-instances", args=[vehicle.pk])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": "assign"}))


def _return_to_stock(request, user, vehicle):
    operation_id = admin_pages.operation_id_from(request)
    kit_id = request.POST.get("kit_id", "")
    if operation_id is None:
        return _page(request, user, vehicle, status=400, errors=["the form was not valid; try again"])
    try:
        record_kit_assignment(
            kit_id=kit_id,
            active=True,
            **{**_context_ids(user), "operation_id": operation_id},
        )
    except (KitError, ValueError) as error:
        return _page(request, user, vehicle, status=400, errors=[str(error)])
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return _page(request, user, vehicle, status=409, errors=["this form was already used; try again"])
    except UNAVAILABLE as error:
        logger.error("kit return to stock unavailable (%s)", type(error).__name__)
        return _page(request, user, vehicle, status=503, errors=["the change could not be completed; nothing was changed. Try again."])
    url = reverse("vehicle-kit-instances", args=[vehicle.pk])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": "stock"}))


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
    if request.POST.get("stage") == "assign":
        return _assign(request, user, vehicle)
    if request.POST.get("stage") == "stock":
        return _return_to_stock(request, user, vehicle)
    return _page(request, user, vehicle, status=400, errors=["the request was not valid"])

