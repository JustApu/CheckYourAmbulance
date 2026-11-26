"""Find and move physical kits from one organization-wide list (issue #277).

Supervisors, Supply Officers and Administrators (manage_kit_assignment, PERM-32) see every
active physical kit, including unassigned stock, with its kit type and current vehicle and
compartment. They search by label, kit type, vehicle or compartment, and narrow the list to
stock or one vehicle. Each row has its own form: choose unassigned stock or a compartment of an
active vehicle, then Save or Reset, as in role administration.

A Save is one record_kit_assignment call, so a move from one vehicle to another is a single
appended assignment event that ends the old placement and starts the new one at the same
instant (TIME-1a); no stock step is recorded. The form carries the kit's latest assignment
event as it was shown. If the kit has changed since, the command refuses with KitChanged and
nothing is recorded. The command reconciles the affected vehicles' obligations afterwards.

Locations are compared by vehicle and compartment slot (stable_slot_id), not by compartment
row: a renamed compartment is a new revision row of the same slot, so it is not a move.
"""

import logging
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import OuterRef, Q, Subquery
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.backend import canonical_id
from cya.checklists.models import KitInstance, Vehicle
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.core.models import CommandReceipt
from cya.kits import admin_pages
from cya.kits import positions as position_rules
from cya.kits.commands import ASSIGN_COMMAND, KitChanged, KitError, record_kit_assignment
from cya.kits.models import KitAssignmentEvent, KitInstanceDefinition
from cya.kits.views import UNAVAILABLE, _context_ids

logger = logging.getLogger(__name__)

ACTION = "manage_kit_assignment"
DENIAL = "MANAGE_KIT_ASSIGNMENT"
PAGE_SIZE = 25
MAX_QUERY = 100
STOCK = "stock"
NO_KIT_TYPE = "(no kit type)"

# Shown in the kit's row (or as a page banner when the row is not listed). Nothing was applied.
ROW_MESSAGES = {
    "NO_CHANGE": "Nothing to save: choose a different location for this kit first.",
    "CHANGED": (
        "Not saved: this kit was moved or changed since the page loaded. Its current location "
        "is shown now; make the change again if it is still needed."
    ),
    "INVALID_TARGET": "Not saved: choose unassigned stock or a compartment from the list.",
}
MESSAGES = {
    "INVALID": "The request was not valid. Reload the page and try again. Nothing was changed.",
    "NOT_FOUND": "That physical kit is not available. Reload the page. Nothing was changed.",
    "OPERATION_CONFLICT": (
        "This form was already used for a different change. Reload the page and try again. "
        "Nothing was changed."
    ),
    "UNAVAILABLE": "The change could not be confirmed. Do not assume it was saved; reload to check.",
}


class _BadFilter(ValueError):
    pass


def _filters(values):
    """{"q", "location", "page"} from a query string or a row form's hidden fields."""
    query = values.get("q", "").strip()
    if len(query) > MAX_QUERY:
        raise _BadFilter(f"Search text can be at most {MAX_QUERY} characters.")
    location = values.get("location", "")
    if location not in ("", STOCK):
        try:
            location = canonical_id(location)
        except ValueError:
            raise _BadFilter("Choose a location from the list.") from None
    return {"q": query, "location": location, "page": values.get("page", "")}


def _query_string(filters, **extra):
    pairs = {key: filters[key] for key in ("q", "location", "page") if filters.get(key)}
    pairs.update({key: value for key, value in extra.items() if value})
    return "?" + urlencode(pairs) if pairs else ""


def _vehicle_name(vehicle):
    return f"{vehicle.display_name} ({vehicle.number})" if vehicle.number else vehicle.display_name


def _targets(org):
    """Active vehicles' live compartments: (groups for the selects, {value: (vehicle, row)})."""
    groups, by_value = [], {}
    vehicles = Vehicle.objects.filter(organization_id=org, active=True).order_by(
        "display_name", "id"
    )[: admin_pages.LIST_LIMIT]
    for vehicle in vehicles:
        options = []
        for row, _key in position_rules.positions(org, str(vehicle.pk), "COMPARTMENT"):
            if row.retired:
                continue
            value = f"{vehicle.pk}:{row.pk}"
            options.append({"value": value, "label": row.label})
            by_value[value] = (vehicle, row)
        if options:
            groups.append({"label": _vehicle_name(vehicle), "options": options})
    return groups, by_value


def _slot_value(by_value, vehicle_id, compartment):
    """The select value for a kit's current compartment slot, or "" when it cannot be chosen."""
    if compartment is None:
        return ""
    for value, (vehicle, row) in by_value.items():
        if str(vehicle.pk) == str(vehicle_id) and row.stable_slot_id == compartment.stable_slot_id:
            return value
    return ""


def _compartment_match(query, by_value):
    """Kits in a live compartment whose current label matches, by vehicle and slot.

    A rename appends a revision row and leaves kit_instance.compartment on the older row, so
    matching only that row's label would miss the name the page shows.
    """
    needle = query.casefold()
    found = Q(compartment__label__icontains=query)
    for vehicle, row in by_value.values():
        if needle in row.label.casefold():
            found |= Q(vehicle_id=vehicle.pk, compartment__stable_slot_id=row.stable_slot_id)
    return found


def _kit_rows(org, filters, by_value):
    kit_type = KitInstanceDefinition.objects.filter(kit=OuterRef("pk")).values("definition__label")[
        :1
    ]
    latest = (
        KitAssignmentEvent.objects.filter(kit=OuterRef("pk")).order_by("-sequence").values("pk")[:1]
    )
    kits = (
        KitInstance.objects.filter(organization_id=org, active=True)
        .annotate(kit_type=Subquery(kit_type), latest_event=Subquery(latest))
        .select_related("vehicle", "compartment")
    )
    query = filters["q"]
    if query:
        kits = kits.filter(
            Q(label__icontains=query)
            | Q(kit_type__icontains=query)
            | Q(vehicle__display_name__icontains=query)
            | Q(vehicle__number__icontains=query)
            | _compartment_match(query, by_value)
        )
    if filters["location"] == STOCK:
        kits = kits.filter(vehicle__isnull=True)
    elif filters["location"]:
        kits = kits.filter(vehicle_id=filters["location"])
    return kits.order_by("label", "id")


def _row(kit, by_value, row_message, pending):
    current = (
        STOCK if kit.vehicle_id is None else _slot_value(by_value, kit.vehicle_id, kit.compartment)
    )
    if current in by_value:
        compartment = by_value[current][1].label
    else:
        compartment = kit.compartment.label if kit.compartment else ""
    message = row_message if row_message and row_message["kit"] == str(kit.pk) else None
    selected = current
    if message and message["kind"] == "error" and pending is not None:
        selected = pending
    return {
        "id": str(kit.pk),
        "label": kit.label,
        "kit_type": kit.kit_type or NO_KIT_TYPE,
        "vehicle": _vehicle_name(kit.vehicle) if kit.vehicle else None,
        "vehicle_inactive": bool(kit.vehicle and not kit.vehicle.active),
        "compartment": compartment,
        "current": current,
        "selected": selected,
        "expected_event": str(kit.latest_event) if kit.latest_event else "",
        "operation_id": admin_pages.new_operation_id(),
        "message": message,
    }


def _page(request, user, *, filters, status=200, error=None, row_message=None, pending=None):
    """row_message is {"kit": id, "kind": "saved" | "error", "text": ...}.

    pending is the location the user chose in that row; it is shown again after a refusal
    that leaves the choice valid, so the user need not choose it again.
    """
    org = str(user.organization_id)
    groups, by_value = _targets(org)
    vehicles = list(
        Vehicle.objects.filter(organization_id=org)
        .order_by("-active", "display_name", "id")
        .values("id", "display_name", "number", "active")[: admin_pages.LIST_LIMIT]
    )
    listing = _kit_rows(org, filters, by_value)
    paginator = Paginator(listing, PAGE_SIZE)
    page = paginator.get_page(filters["page"])
    rows = [_row(kit, by_value, row_message, pending) for kit in page.object_list]
    page_message = row_message if row_message and not any(r["message"] for r in rows) else None
    searched = bool(filters["q"] or filters["location"])
    title = "Find and move physical kits"
    if searched:
        title = f"{title} ({paginator.count} matching)"
    context = {
        "title": title,
        "filters": {**filters, "page": str(page.number) if page.number > 1 else ""},
        "searched": searched,
        "vehicles": [{**v, "id": str(v["id"])} for v in vehicles],
        "groups": groups,
        "rows": rows,
        "page": page,
        "total": paginator.count,
        "page_size": PAGE_SIZE,
        "max_query": MAX_QUERY,
        "stock": STOCK,
        "error": error,
        "page_message": page_message,
        "previous_query": _query_string(filters, page=page.previous_page_number())
        if page.has_previous()
        else "",
        "next_query": _query_string(filters, page=page.next_page_number())
        if page.has_next()
        else "",
    }
    return render(request, "kits/kit_locations.html", context, status=status)


def _filter_error_page(request, user, message, status):
    """The search form alone: an invalid filter lists nothing rather than everything."""
    context = {
        "title": "Find and move physical kits",
        "filters": {"q": "", "location": "", "page": ""},
        "filter_error": message,
        "vehicles": [],
        "groups": [],
        "rows": [],
        "max_query": MAX_QUERY,
        "stock": STOCK,
    }
    return render(request, "kits/kit_locations.html", context, status=status)


def _saved_message(user, value):
    """The confirmation for a Save this user made, read from its receipt (as role administration)."""
    try:
        operation_id = canonical_id(value)
    except ValueError:
        return None
    receipt = CommandReceipt.objects.filter(
        organization_id=user.organization_id,
        operation_id=operation_id,
        actor_user_id=user.pk,
        command_type=ASSIGN_COMMAND,
    ).first()
    if receipt is None or receipt.response_body.get("code") != "KIT_ASSIGNMENT_RECORDED":
        return None
    event = (
        KitAssignmentEvent.objects.filter(
            organization_id=user.organization_id, pk=receipt.response_body.get("event_id")
        )
        .select_related("kit", "vehicle", "compartment")
        .first()
    )
    if event is None:
        return None
    if not event.active:
        text = f"Saved: {event.kit.label} is no longer an active physical kit."
    elif event.vehicle is None:
        text = f"Saved: {event.kit.label} is now in unassigned stock."
    else:
        where = f"{_vehicle_name(event.vehicle)}, {event.compartment.label}"
        text = f"Saved: {event.kit.label} is now on {where}."
    return {"kit": str(event.kit_id), "kind": "saved", "text": text}


def _parse_target(value, by_value):
    """(vehicle_id, compartment_id, compartment row) for a chosen location, or None."""
    if value == STOCK:
        return None, None, None
    if value not in by_value:
        raise ValueError("not an offered location")
    vehicle, row = by_value[value]
    return str(vehicle.pk), str(row.pk), row


def _same_place(event, vehicle_id, row):
    if vehicle_id is None:
        return event.active and event.vehicle_id is None
    if not event.active or str(event.vehicle_id or "") != vehicle_id:
        return False
    return event.compartment is not None and event.compartment.stable_slot_id == row.stable_slot_id


def _save(request, user):
    org = str(user.organization_id)
    try:
        filters = _filters(request.POST)
    except _BadFilter:
        filters = {"q": "", "location": "", "page": ""}

    def failed(outcome, status):
        return _page(request, user, filters=filters, status=status, error=MESSAGES[outcome])

    operation_id = admin_pages.operation_id_from(request)
    try:
        kit_id = canonical_id(request.POST.get("kit_id", ""))
        expected_event_id = canonical_id(request.POST.get("expected_event", ""))
    except ValueError:
        return failed("INVALID", 400)
    if operation_id is None:
        return failed("INVALID", 400)
    target = request.POST.get("target", "")

    def refused(kind, status, *, keep_choice=True, text=None):
        message = {"kit": kit_id, "kind": "error", "text": text or ROW_MESSAGES[kind]}
        return _page(
            request,
            user,
            filters=filters,
            status=status,
            row_message=message,
            pending=target if keep_choice else None,
        )

    kit = KitInstance.objects.filter(organization_id=org, pk=kit_id, active=True).first()
    if kit is None:
        return failed("NOT_FOUND", 404)
    shown = (
        KitAssignmentEvent.objects.filter(organization_id=org, kit_id=kit_id, pk=expected_event_id)
        .select_related("compartment")
        .first()
    )
    if shown is None:
        return refused("CHANGED", 409, keep_choice=False)
    if target == "":
        # The "keep current location" choice of a kit whose place cannot be offered again.
        return refused("NO_CHANGE", 400)
    _groups, by_value = _targets(org)
    try:
        vehicle_id, compartment_id, row = _parse_target(target, by_value)
    except ValueError:
        return refused("INVALID_TARGET", 400, keep_choice=False)
    if _same_place(shown, vehicle_id, row):
        return refused("NO_CHANGE", 400)
    try:
        result = record_kit_assignment(
            kit_id=kit_id,
            active=True,
            vehicle_id=vehicle_id,
            compartment_id=compartment_id,
            expected_event_id=expected_event_id,
            **{**_context_ids(user), "operation_id": operation_id},
        )
    except KitChanged:
        return refused("CHANGED", 409, keep_choice=False)
    except CommandDenied:
        # run_command already committed the denial audit after the rollback.
        return render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return failed("OPERATION_CONFLICT", 409)
    except UNAVAILABLE as error:
        logger.error("kit move unavailable (%s)", type(error).__name__)
        return failed("UNAVAILABLE", 503)
    except (KitError, ValueError) as error:
        return refused("RULE", 400, text=f"Not saved: {error}.")
    if result.operation_reused:
        # The same user sent this operation id with another request: run_command replayed the
        # earlier receipt and applied nothing, so this is not a save.
        return failed("OPERATION_CONFLICT", 409)
    # Post/redirect/get: the page names the saved move and never resubmits it.
    url = reverse("kit-instance-locations") + _query_string(filters, saved=operation_id)
    return HttpResponseRedirect(f"{url}#kit-{kit_id}")


@never_cache
@require_http_methods(["GET", "POST"])
def kit_locations(request):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    if request.method == "POST":
        if request.POST.get("stage") == "save":
            return _save(request, user)
        return _page(
            request,
            user,
            filters={"q": "", "location": "", "page": ""},
            status=400,
            error=MESSAGES["INVALID"],
        )
    try:
        filters = _filters(request.GET)
    except _BadFilter as error:
        return _filter_error_page(request, user, str(error), 400)
    saved = _saved_message(user, request.GET["saved"]) if "saved" in request.GET else None
    return _page(request, user, filters=filters, row_message=saved)
