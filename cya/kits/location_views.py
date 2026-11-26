"""Find and move physical kits from one organization-wide list (issue #277).

Supervisors, Supply Officers and Administrators (manage_kit_assignment, PERM-32) see every
active physical kit, including unassigned stock, with its kit type and current vehicle and
compartment. They search by label, kit type, vehicle or compartment, and narrow the list to
stock or one vehicle. Each row has its own Change location step: the row then offers unassigned
stock or a compartment of an active vehicle, with Save and Reset as in role administration. Only
one row is open at a time, so the page carries one option list, not one per row (PR #295
review); it needs no script.

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
from django.db.models import Exists, OuterRef, Q, Subquery
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.backend import canonical_id
from cya.checklists.models import Compartment, KitInstance, Vehicle
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.core.models import CommandReceipt
from cya.kits import admin_pages
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


VEHICLE_LIMIT = admin_pages.LIST_LIMIT


def _latest(org, **slot_filters):
    """{(vehicle_id, stable_slot_id): latest compartment revision effective now}, in one query.

    The same rule positions() applies to one vehicle; retired revisions are kept so callers can
    tell a retired compartment from one that is simply not on the page.
    """
    found = {}
    for row in Compartment.objects.filter(
        organization_id=org, effective_at__lte=timezone.now(), **slot_filters
    ).order_by("vehicle_id", "stable_slot_id", "-revision"):
        found.setdefault((row.vehicle_id, row.stable_slot_id), row)
    return found


def _live_rows(org, vehicle_id):
    """One vehicle's unretired compartments, in display order (as positions() orders them)."""
    rows = [row for row in _latest(org, vehicle_id=vehicle_id).values() if not row.retired]
    return sorted(
        rows, key=lambda r: (r.display_order is None, r.display_order or 0, r.label, str(r.pk))
    )


def _current_rows(org, kits):
    """The latest revision of each listed kit's own compartment slot (PR #295 review).

    Read per kit rather than from a list of offered targets, so a kit on any vehicle shows its
    compartment's current name, and is called retired only when that slot really is retired.
    """
    placed = [kit for kit in kits if kit.vehicle_id and kit.compartment_id]
    if not placed:
        return {}
    return _latest(
        org,
        vehicle_id__in={kit.vehicle_id for kit in placed},
        stable_slot_id__in={kit.compartment.stable_slot_id for kit in placed},
    )


def _compartment_match(query):
    """Kits whose compartment slot's latest name matches; any vehicle, in SQL (PR #295 review).

    A rename appends a revision row and leaves kit_instance.compartment on the older row, so the
    kit's own row would show and match an old name.
    """
    now = timezone.now()
    newer = Compartment.objects.filter(
        vehicle_id=OuterRef("vehicle_id"),
        stable_slot_id=OuterRef("stable_slot_id"),
        effective_at__lte=now,
        revision__gt=OuterRef("revision"),
    )
    named = Compartment.objects.filter(
        vehicle_id=OuterRef("vehicle_id"),
        stable_slot_id=OuterRef("compartment__stable_slot_id"),
        effective_at__lte=now,
        label__icontains=query,
    ).filter(~Exists(newer))
    return Exists(named)


def _kit_rows(org, filters):
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
            | _compartment_match(query)
        )
    if filters["location"] == STOCK:
        kits = kits.filter(vehicle__isnull=True)
    elif filters["location"]:
        kits = kits.filter(vehicle_id=filters["location"])
    return kits.order_by("label", "id")


def _row(kit, current_rows, row_message, pending, editing):
    current, compartment, retired = STOCK, "", False
    if kit.vehicle_id is not None:
        slot = kit.compartment.stable_slot_id if kit.compartment else None
        row = current_rows.get((kit.vehicle_id, slot))
        compartment = row.label if row else (kit.compartment.label if kit.compartment else "")
        retired = row is not None and row.retired
        offered = kit.vehicle.active and row is not None and not row.retired
        current = f"{kit.vehicle_id}:{row.pk}" if offered else ""
    message = row_message if row_message and row_message["kit"] == str(kit.pk) else None
    selected = current
    if message and message["kind"] == "error" and pending is not None:
        selected = pending
    return {
        "id": str(kit.pk),
        "label": kit.label,
        "kit_type": kit.kit_type or NO_KIT_TYPE,
        "vehicle_id": str(kit.vehicle_id) if kit.vehicle_id else "",
        "vehicle": _vehicle_name(kit.vehicle) if kit.vehicle else None,
        "vehicle_inactive": bool(kit.vehicle and not kit.vehicle.active),
        "compartment_retired": retired,
        "compartment": compartment,
        "current": current,
        "selected": selected,
        # Every kit has history from add_kit_instance; one without cannot be moved here.
        "movable": kit.latest_event is not None,
        "editing": editing == str(kit.pk) and kit.latest_event is not None,
        "expected_event": str(kit.latest_event) if kit.latest_event else "",
        "operation_id": admin_pages.new_operation_id(),
        "message": message,
        "invalid": bool(message and message["kind"] == "error" and message.get("invalid")),
    }


def _choice(org, row, to, to_q):
    """The open row's two steps: which active vehicle, then which of its compartments.

    The vehicle list is bounded (VEHICLE_LIMIT) but never hides a vehicle: past the limit, a
    search box narrows it, so any active vehicle can be chosen by name or number. Only the
    chosen vehicle's compartments are offered, so a fleet of any size can be reached (PR #295
    review, Codex P2).
    """
    vehicles = Vehicle.objects.filter(organization_id=org, active=True)
    if to_q:
        vehicles = vehicles.filter(Q(display_name__icontains=to_q) | Q(number__icontains=to_q))
    listed = list(vehicles.order_by("display_name", "id")[: VEHICLE_LIMIT + 1])
    more = len(listed) > VEHICLE_LIMIT
    listed = listed[:VEHICLE_LIMIT]
    chosen = None
    if to:
        chosen = Vehicle.objects.filter(organization_id=org, active=True, pk=to).first()
    elif row["vehicle_id"] and not row["vehicle_inactive"]:
        chosen = Vehicle.objects.filter(organization_id=org, pk=row["vehicle_id"]).first()
    if chosen is not None and all(v.pk != chosen.pk for v in listed):
        listed.insert(0, chosen)  # the chosen vehicle is always listed, whatever the search
    options = []
    if chosen is not None:
        options = [
            {"value": f"{chosen.pk}:{r.pk}", "label": r.label} for r in _live_rows(org, chosen.pk)
        ]
    return {
        "vehicles": [{"id": str(v.pk), "name": _vehicle_name(v)} for v in listed],
        "more": more,
        "limit": VEHICLE_LIMIT,
        "to": str(chosen.pk) if chosen else "",
        "to_q": to_q,
        "vehicle_label": _vehicle_name(chosen) if chosen else "",
        "options": options,
    }


def _edit_target(value):
    try:
        return canonical_id(value) if value else None
    except ValueError:
        return None


def _page(
    request,
    user,
    *,
    filters,
    status=200,
    error=None,
    row_message=None,
    pending=None,
    editing=None,
    to="",
    to_q="",
):
    """row_message is {"kit": id, "kind": "saved" | "error", "text": ...}.

    Only the row named by editing shows the location choice, so the page carries one option
    list rather than one per row (PR #295 review). pending is the location the user chose in
    that row; it is shown again after a refusal that leaves the choice valid.
    """
    org = str(user.organization_id)
    vehicles = list(
        Vehicle.objects.filter(organization_id=org)
        .order_by("-active", "display_name", "id")
        .values("id", "display_name", "number", "active")[: VEHICLE_LIMIT + 1]
    )
    paginator = Paginator(_kit_rows(org, filters), PAGE_SIZE)
    page = paginator.get_page(filters["page"])
    kits = list(page.object_list)
    current_rows = _current_rows(org, kits)
    rows = [_row(kit, current_rows, row_message, pending, editing) for kit in kits]
    for row in rows:
        if row["editing"]:
            row["choice"] = _choice(org, row, to, to_q)
            offered = {STOCK} | {option["value"] for option in row["choice"]["options"]}
            # When the selected value is not an option (a place that cannot be offered again,
            # or another vehicle chosen in the first step), an explicit empty choice is shown
            # selected. Otherwise the browser would select the first option, stock, and an
            # untouched Save would move the kit there (PR #295 review, Codex P2). The server
            # treats an empty choice as "nothing to save", never as stock.
            row["placeholder"] = (
                None
                if row["selected"] in offered
                # A place that can never be offered again; otherwise, another vehicle.
                else "Keep current location"
                if not row["current"]
                else "Choose a destination"
            )
    page_message = row_message if row_message and not any(r["message"] for r in rows) else None
    searched = bool(filters["q"] or filters["location"])
    title = "Find and move physical kits"
    if searched:
        title = f"{title} ({paginator.count} matching)"
    context = {
        "title": title,
        "filters": {**filters, "page": str(page.number) if page.number > 1 else ""},
        "searched": searched,
        "vehicles": [{**v, "id": str(v["id"])} for v in vehicles[:VEHICLE_LIMIT]],
        # Past the limit the filter list is cut short; the search box still finds any vehicle.
        "vehicles_more": len(vehicles) > VEHICLE_LIMIT,
        "vehicle_limit": VEHICLE_LIMIT,
        "rows": rows,
        "list_query": _query_string(filters),
        "query_prefix": _query_string(filters) + "&" if _query_string(filters) else "?",
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


def _parse_target(org, value):
    """(vehicle_id, compartment_id, compartment row) for a chosen location.

    Stock, or "<vehicle>:<compartment>" naming a live, unretired compartment of an active
    vehicle in the organization, whichever vehicle it is; anything else raises ValueError.
    """
    if value == STOCK:
        return None, None, None
    vehicle_part, _sep, compartment_part = value.partition(":")
    vehicle_id, compartment_id = canonical_id(vehicle_part), canonical_id(compartment_part)
    if not Vehicle.objects.filter(organization_id=org, pk=vehicle_id, active=True).exists():
        raise ValueError("not an active vehicle")
    live = {str(row.pk): row for row in _live_rows(org, vehicle_id)}
    if compartment_id not in live:
        raise ValueError("not a live compartment of that vehicle")
    return vehicle_id, compartment_id, live[compartment_id]


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

    def refused(kind, status, *, keep_choice=True, text=None, invalid=False):
        # The row stays open with the refusal beside its choice. invalid marks the choice
        # itself as the problem (aria-invalid on the select).
        message = {
            "kit": kit_id,
            "kind": "error",
            "text": text or ROW_MESSAGES[kind],
            "invalid": invalid,
        }
        return _page(
            request,
            user,
            filters=filters,
            status=status,
            row_message=message,
            pending=target if keep_choice else None,
            editing=kit_id,
            # Show the compartments of the vehicle the refused choice named, if any.
            to=_edit_target(target.partition(":")[0]) or "" if keep_choice else "",
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
    try:
        vehicle_id, compartment_id, row = _parse_target(org, target)
    except ValueError:
        return refused("INVALID_TARGET", 400, keep_choice=False, invalid=True)
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
        return refused("RULE", 400, text=f"Not saved: {error}.", invalid=True)
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
    editing = None if saved else _edit_target(request.GET.get("edit", ""))
    to = _edit_target(request.GET.get("to", "")) or ""
    to_q = request.GET.get("to_q", "").strip()[:MAX_QUERY]
    return _page(
        request, user, filters=filters, row_message=saved, editing=editing, to=to, to_q=to_q
    )
