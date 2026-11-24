"""Physical kit stock screen (issue #273).

Creating a labeled physical unit is separate from assigning it to a vehicle. An active kit with no
vehicle/compartment is unassigned stock; its assignment history remains the source of truth.
"""

import logging
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits.commands import KitError, add_kit_instance
from cya.kits.models import KitDefinition, KitInstanceDefinition
from cya.kits.views import UNAVAILABLE, _context_ids

logger = logging.getLogger(__name__)
ACTION = "manage_kit_assignment"
DENIAL = "MANAGE_KIT_ASSIGNMENT"


def _inventory(org):
    kits = list(
        __import__("cya.checklists.models", fromlist=["KitInstance"])
        .KitInstance.objects.filter(organization_id=org, active=True, vehicle__isnull=True)
        .order_by("label", "id")
    )
    names = dict(
        KitInstanceDefinition.objects.filter(kit__in=kits).values_list(
            "kit_id", "definition__label"
        )
    )
    return [{"label": kit.label, "definition": names.get(kit.pk, "(no kit type)")} for kit in kits]


def _page(request, user, *, status=200, errors=None, form=None):
    org = str(user.organization_id)
    return render(
        request,
        "kits/kit_inventory.html",
        {
            "title": "Physical kit inventory",
            "definitions": list(
                KitDefinition.objects.filter(organization_id=org, active=True).order_by("label")
            ),
            "kits": _inventory(org),
            "form": form or {
                "definition_id": request.POST.get("definition_id", request.GET.get("definition", "")),
                "label": request.POST.get("label", request.GET.get("label", ""))[:200],
            },
            "operation_id": admin_pages.new_operation_id(),
            "errors": errors or [],
            "saved": "Physical kit created in stock."
            if request.GET.get("saved") == "created"
            else None,
        },
        status=status,
    )


@never_cache
@require_http_methods(["GET", "POST"])
def inventory(request):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    if request.method == "GET":
        return _page(request, user)
    if request.POST.get("stage") != "create":
        return _page(request, user, status=400, errors=["the request was not valid"])
    form = {
        "definition_id": request.POST.get("definition_id", ""),
        "label": request.POST.get("label", ""),
    }
    operation_id = admin_pages.operation_id_from(request)
    if operation_id is None:
        return _page(request, user, status=400, errors=["the form was not valid; try again"], form=form)
    if not form["definition_id"]:
        return _page(request, user, status=400, errors=["choose the kit type"], form=form)
    try:
        add_kit_instance(
            label=form["label"],
            definition_id=form["definition_id"],
            **{**_context_ids(user), "operation_id": operation_id},
        )
    except (KitError, ValueError) as error:
        return _page(request, user, status=400, errors=[str(error)], form=form)
    except CommandDenied:
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return _page(request, user, status=409, errors=["this form was already used; try again"], form=form)
    except UNAVAILABLE as error:
        logger.error("kit stock creation unavailable (%s)", type(error).__name__)
        return _page(
            request, user, status=503,
            errors=["the change could not be completed; nothing was changed. Try again."], form=form,
        )
    return HttpResponseRedirect(reverse("kit-instance-inventory") + "?" + urlencode({"saved": "created"}))
