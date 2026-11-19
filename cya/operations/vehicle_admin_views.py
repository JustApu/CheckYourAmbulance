"""Vehicle screens (SPEC 4.8 CFG-1, CFG-4, CFG-6; issue #130 part 5a).

Supervisors and Administrators (manage_vehicles, PERM-47) list and search the fleet, create a
vehicle with its VIN, edit its name and number, and deactivate or reactivate it. Duplicating a
vehicle needs duplicate_configuration (PERM-48). Anyone else gets 403 and an audited denial.

Every change is a form post carrying an operation id rendered into the form, so a resubmitted
form replays rather than repeats. Deactivating, reactivating and duplicating are two steps
(CFG-6): a preview, then a confirmation that applies exactly what was shown or is refused.
An activity confirmation names the state it changes to, and the command refuses a vehicle
already in that state. Duplication confirms with the preview's base token, which the command
recomputes under its locks.
"""

import logging
from urllib.parse import urlencode

from django.db.models import Max, Q
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.checklists.models import ChecklistTemplate, Vehicle
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.inspections.views import STATUS_LABELS
from cya.kits import admin_pages
from cya.kits.keys import KeyRuleError
from cya.kits.models import KitDefinition
from cya.kits.views import UNAVAILABLE, _can, _context_ids
from cya.operations import duplication
from cya.operations import vehicles as vehicle_rules
from cya.operations.models import VehicleStatusEvent

logger = logging.getLogger(__name__)

ACTION = vehicle_rules.ACTION
DENIAL = "MANAGE_VEHICLES"
DUPLICATE_ACTION = "duplicate_configuration"
DUPLICATE_DENIAL = "DUPLICATE_CONFIGURATION"
MAX_QUERY = 100
SAVED = {
    "created": "Vehicle created. It is active.",
    "updated": "Vehicle saved.",
    "deactivated": "Vehicle deactivated. It owes no routine inspection from now on.",
    "reactivated": "Vehicle reactivated. Routine inspections are owed again from now on.",
    "duplicated": (
        "Vehicle duplicated. The copy is inactive; its kit list and overrides are drafts."
    ),
}
EXPECTED = (vehicle_rules.VehicleError, KeyRuleError, ValueError)
# The vehicle's other configuration pages, each shown only to those who may open it.
CONFIGURATION_LINKS = (
    ("vehicle-bindings", "Checklist bindings", "bind_unbind_template"),
    ("vehicle-positions", "Compartments and systems", "manage_vehicle_positions"),
    ("vehicle-kit-list", "Kit list", "manage_vehicle_kit_lists"),
    ("vehicle-kit-instances", "Kit instances", "manage_kit_assignment"),
)


def _statuses(organization_id, vehicle_ids):
    """{vehicle id: operational status code} from each vehicle's latest status event.

    One grouped query finds each vehicle's latest sequence, then only those rows are read, so
    the cost follows the number of listed vehicles, not the length of their histories.
    """
    if not vehicle_ids:
        return {}
    tops = list(
        VehicleStatusEvent.objects.filter(
            organization_id=organization_id, vehicle_id__in=vehicle_ids
        )
        .values("vehicle_id")
        .annotate(top=Max("sequence"))
        .order_by()
    )
    if not tops:
        return {}
    wanted = Q()
    for top in tops:
        wanted |= Q(vehicle_id=top["vehicle_id"], sequence=top["top"])
    return {
        str(vehicle_id): status
        for vehicle_id, status in VehicleStatusEvent.objects.filter(
            wanted, organization_id=organization_id
        ).values_list("vehicle_id", "status")
    }


def _listing(user, query):
    found = Vehicle.objects.filter(organization_id=user.organization_id)
    if query:
        found = found.filter(
            Q(display_name__icontains=query) | Q(number__icontains=query) | Q(key__icontains=query)
        )
    rows = list(
        found.order_by("-active", "display_name", "id").values(
            "id", "display_name", "number", "key", "active"
        )[: admin_pages.LIST_LIMIT]
    )
    statuses = _statuses(str(user.organization_id), [row["id"] for row in rows])
    for row in rows:
        code = statuses.get(str(row["id"]))
        row["status_label"] = STATUS_LABELS.get(code, "Not recorded")
    return rows


def _just_saved(user, request):
    """The vehicle a name or number save came back from, named on the list (owner, 2026-11-18)."""
    vehicle_id = request.GET.get("saved", "")
    if not vehicle_id:
        return None
    return (
        Vehicle.objects.filter(organization_id=user.organization_id, pk=vehicle_id[:36])
        .values_list("display_name", flat=True)
        .first()
    )


def _list_page(request, user, *, status=200, errors=None, form=None):
    query = request.GET.get("q", "").strip()[:MAX_QUERY]
    context = {
        "saved_name": None if errors else _just_saved(user, request),
        "title": "Vehicles",
        "query": query,
        "vehicles": _listing(user, query),
        "list_limit": admin_pages.LIST_LIMIT,
        "create_operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "form": form or {},
    }
    return render(request, "operations/admin_vehicles.html", context, status=status)


def _number(value):
    value = (value or "").strip()
    return value or None


def _run(request, render_error, call):
    """(the command's result, None) on success, else (None, the error response)."""
    try:
        return call(), None
    except EXPECTED as error:
        return None, render_error(400, [str(error)])
    except CommandDenied:
        return None, render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return None, render_error(
            409, ["this form was already used for a different change; try again"]
        )
    except UNAVAILABLE as error:
        logger.error("vehicle change unavailable (%s)", type(error).__name__)
        return None, render_error(
            503, ["the change could not be completed; nothing was changed. Try again."]
        )


def _ids(request, user):
    operation_id = admin_pages.operation_id_from(request)
    if operation_id is None:
        return None
    return {**_context_ids(user), "operation_id": operation_id}


@never_cache
@require_http_methods(["GET", "POST"])
def vehicles(request):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    if request.method == "GET":
        return _list_page(request, user)
    form = {
        "vin": request.POST.get("vin", ""),
        "display_name": request.POST.get("display_name", ""),
        "number": request.POST.get("number", ""),
    }

    def render_error(status, errors):
        return _list_page(request, user, status=status, errors=errors, form=form)

    if request.POST.get("stage") != "create":
        return render_error(400, ["the request was not valid"])
    ids = _ids(request, user)
    if ids is None:
        return render_error(400, ["the form was not valid; try again"])
    result, failed = _run(
        request,
        render_error,
        lambda: vehicle_rules.create_vehicle(
            vin=form["vin"],
            display_name=form["display_name"],
            number=_number(form["number"]),
            **ids,
        ),
    )
    if failed:
        return failed
    url = reverse("vehicle-admin", args=[result.body["vehicle_id"]])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": "created"}))


def _vehicle_page(
    request, user, roles, vehicle, *, status=200, errors=None, changing=None, edit=None
):
    code = _statuses(str(user.organization_id), [vehicle.pk]).get(str(vehicle.pk))
    context = {
        "title": f"Vehicle: {vehicle.display_name}",
        "vehicle": vehicle,
        "status_label": STATUS_LABELS.get(code, "Not recorded"),
        "edit_operation_id": admin_pages.new_operation_id(),
        # A refused edit shows what was typed, not the stored values (PR #174 review).
        "edit": edit or {"display_name": vehicle.display_name, "number": vehicle.number or ""},
        "changing": changing,
        "can_duplicate": _can(user, roles, DUPLICATE_ACTION),
        "links": [
            {"url": reverse(name, args=[vehicle.pk]), "label": label}
            for name, label, action in CONFIGURATION_LINKS
            if _can(user, roles, action)
        ],
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
    }
    return render(request, "operations/admin_vehicle.html", context, status=status)


def _wanted_active(request):
    """True or False from the form's `active` field, else None."""
    return {"1": True, "0": False}.get(request.POST.get("active"))


@never_cache
@require_http_methods(["GET", "POST"])
def vehicle(request, vehicle_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, roles = allowed
    vehicle = admin_pages.vehicle_of(user, vehicle_id)
    if request.method == "GET":
        return _vehicle_page(request, user, roles, vehicle)

    def render_error(status, errors):
        edit = None
        if request.POST.get("stage") == "update":
            edit = {
                "display_name": request.POST.get("display_name", ""),
                "number": request.POST.get("number", ""),
            }
        return _vehicle_page(request, user, roles, vehicle, status=status, errors=errors, edit=edit)

    stage = request.POST.get("stage")
    active = _wanted_active(request)
    if stage == "active-preview":
        # CFG-6: say what the change does before it is confirmed. The confirmation names the
        # state it changes to, and the command refuses a vehicle already in that state.
        if active is None:
            return render_error(400, ["the request was not valid"])
        if active is vehicle.active:
            return render_error(400, ["the vehicle is already in that state"])
        changing = {"active": active, "operation_id": admin_pages.new_operation_id()}
        return _vehicle_page(request, user, roles, vehicle, changing=changing)
    if stage not in ("update", "active"):
        return render_error(400, ["the request was not valid"])
    ids = _ids(request, user)
    if ids is None:
        return render_error(400, ["the form was not valid; try again"])
    if stage == "update":
        # Both fields are always posted; a missing one is a malformed form, never a request to
        # clear it (a blank number in the form does clear it).
        if "display_name" not in request.POST or "number" not in request.POST:
            return render_error(400, ["the form was not valid; try again"])
        saved = "updated"

        def call():
            return vehicle_rules.update_vehicle(
                vehicle_id=str(vehicle.pk),
                display_name=request.POST.get("display_name", ""),
                number=_number(request.POST.get("number")),
                **ids,
            )

    else:
        if active is None:
            return render_error(400, ["the request was not valid"])
        saved = "reactivated" if active else "deactivated"

        def call():
            return vehicle_rules.set_vehicle_active(
                vehicle_id=str(vehicle.pk), active=active, **ids
            )

    _result, failed = _run(request, render_error, call)
    if failed:
        return failed
    if stage == "update":
        # Renaming is often done for several vehicles in a row, so a saved name or number goes
        # back to the list, which names the vehicle saved (owner, 2026-11-18).
        url = reverse("vehicle-admin-list")
        return HttpResponseRedirect(url + "?" + urlencode({"saved": str(vehicle.pk)}))
    url = reverse("vehicle-admin", args=[vehicle.pk])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": saved}))


def _shown(user, vehicle, preview):
    """The duplication preview with the names a person reads in place of ids."""
    org = user.organization_id
    template_ids = {o["template_id"] for o in preview["overrides"]} | {
        b["template_id"] for b in preview["bindings"]
    }
    templates = dict(
        ChecklistTemplate.objects.filter(organization_id=org, pk__in=template_ids).values_list(
            "pk", "label"
        )
    )
    templates = {str(pk): label for pk, label in templates.items()}
    definitions = {
        str(pk): label
        for pk, label in KitDefinition.objects.filter(
            organization_id=org,
            pk__in={line["definition_id"] for line in preview["kit_lines"]},
        ).values_list("pk", "label")
    }
    compartments = {
        p["stable_slot_id"]: p["label"] for p in preview["positions"] if p["kind"] == "COMPARTMENT"
    }
    bindings = {}
    for binding in preview["bindings"]:
        bindings.setdefault(binding["template_id"], []).append(binding["slot"])
    return {
        "positions": preview["positions"],
        "kit_list_in_force": preview["kit_list_in_force"],
        "kit_lines": [
            {
                "definition": definitions.get(line["definition_id"], line["definition_id"]),
                "compartment": compartments.get(line["stable_slot_id"], ""),
                "required_count": line["required_count"],
            }
            for line in preview["kit_lines"]
        ],
        "overrides": [
            {
                "template": templates.get(o["template_id"], o["template_id"]),
                "entries": len(o["entries"]),
            }
            for o in preview["overrides"]
        ],
        "bindings": [
            {"template": templates.get(template_id, template_id), "slots": slots}
            for template_id, slots in bindings.items()
        ],
        "excluded": preview["excluded"],
        "never_copied": preview["never_copied"],
        "base_token": preview["base_token"],
    }


def _duplicate_page(request, user, vehicle, *, status=200, errors=None, form=None):
    try:
        preview = duplication.preview_vehicle_duplicate(str(user.organization_id), str(vehicle.pk))
    except UNAVAILABLE as error:
        logger.error("vehicle duplication preview unavailable (%s)", type(error).__name__)
        return render(
            request,
            "operations/admin_vehicle_duplicate.html",
            {
                "title": f"Duplicate {vehicle.display_name}",
                "vehicle": vehicle,
                "unavailable": True,
                "errors": errors or [],
            },
            status=503,
        )
    context = {
        "title": f"Duplicate {vehicle.display_name}",
        "vehicle": vehicle,
        "copy": _shown(user, vehicle, preview),
        "operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "form": form or {},
    }
    return render(request, "operations/admin_vehicle_duplicate.html", context, status=status)


@never_cache
@require_http_methods(["GET", "POST"])
def duplicate(request, vehicle_id):
    allowed, refused = admin_pages.allowed(request, DUPLICATE_ACTION, DUPLICATE_DENIAL)
    if refused:
        return refused
    user, roles = allowed
    vehicle = admin_pages.vehicle_of(user, vehicle_id)
    if request.method == "GET":
        return _duplicate_page(request, user, vehicle)
    form = {
        "vin": request.POST.get("vin", ""),
        "display_name": request.POST.get("display_name", ""),
        "number": request.POST.get("number", ""),
    }

    def render_error(status, errors):
        # A fresh preview, so a refused stale confirmation shows what would be copied now.
        return _duplicate_page(request, user, vehicle, status=status, errors=errors, form=form)

    if request.POST.get("stage") != "duplicate":
        return render_error(400, ["the request was not valid"])
    ids = _ids(request, user)
    if ids is None:
        return render_error(400, ["the form was not valid; try again"])
    result, failed = _run(
        request,
        render_error,
        lambda: duplication.duplicate_vehicle(
            source_vehicle_id=str(vehicle.pk),
            vin=form["vin"],
            display_name=form["display_name"],
            number=_number(form["number"]),
            base_token=request.POST.get("base_token", ""),
            **ids,
        ),
    )
    if failed:
        return failed
    copy_id = result.body["vehicle_id"]
    # The copy's page needs manage_vehicles; both roles that may duplicate also hold it.
    if not _can(user, roles, ACTION):
        return HttpResponseRedirect(reverse("vehicle-admin-list"))
    url = reverse("vehicle-admin", args=[copy_id])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": "duplicated"}))
