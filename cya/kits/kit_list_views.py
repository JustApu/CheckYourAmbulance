"""Vehicle kit list screens (SPEC 4.6 KDEF-9, 4.8 CFG-6; issue #130 part 5c).

Supervisors and Administrators (manage_vehicle_kit_lists, PERM-44) see a vehicle's kit list in
force, any publication scheduled for later and the current draft. They save a whole new draft
(every line, as the command takes it) and publish a draft now or at a station-local date and
time. Publishing never backdates: the command refuses an instant before the publication.
"""

import logging
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits import positions as position_rules
from cya.kits.kit_lists import (
    KitListError,
    kit_list_effective_at,
    publish_vehicle_kit_list,
    save_vehicle_kit_list_draft,
)
from cya.kits.models import (
    STORED_COUNT_MAX,
    KitDefinition,
    VehicleKitListPublication,
    VehicleKitListVersion,
    VehicleKitRequirement,
)
from cya.kits.views import UNAVAILABLE, _context_ids

logger = logging.getLogger(__name__)

ACTION = "manage_vehicle_kit_lists"
DENIAL = "MANAGE_VEHICLE_KIT_LISTS"
BLANK_ROWS = 3
ROW_FIELDS = 5  # definition, compartment, count, order, remove
FORM_FIELDS = 10  # csrf token, stage, operation id, row count, with room to spare
# The editor saves a whole new draft, so it must post every line it shows: never more rows than
# Django accepts in one form (DATA_UPLOAD_MAX_NUMBER_FIELDS), or a save would lose the rest.
_FIELD_LIMIT = settings.DATA_UPLOAD_MAX_NUMBER_FIELDS
MAX_ROWS = 300 if _FIELD_LIMIT is None else min(300, (_FIELD_LIMIT - FORM_FIELDS) // ROW_FIELDS)
INVALID = "the form was not valid; reload the page and try again"
SAVED = {"draft": "Draft saved. Publish it to put it in force.", "publish": "Kit list published."}


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
        title="Expected kit inventory",
        intro="Choose a vehicle to view or edit the kit types and quantities it is expected to carry.",
        url_name="vehicle-kit-list",
    )


def _compartments(org, vehicle):
    """(current rows, {any row id of a slot: current row}) for compartments not retired."""
    current = [
        row
        for row, _key in position_rules.positions(org, str(vehicle.pk), "COMPARTMENT")
        if not row.retired
    ]
    by_slot = {row.stable_slot_id: row for row in current}
    from cya.checklists.models import Compartment

    any_row = {
        str(pk): by_slot[slot]
        for pk, slot in Compartment.objects.filter(vehicle=vehicle).values_list(
            "pk", "stable_slot_id"
        )
        if slot in by_slot
    }
    return current, any_row


def _lines(version, any_row):
    if version is None:
        return []
    return [
        {
            "definition_id": str(line.definition_id),
            "definition": line.definition.label,
            "definition_active": line.definition.active,
            "compartment_id": str(any_row[str(line.compartment_id)].pk)
            if str(line.compartment_id) in any_row
            else "",
            "compartment": any_row[str(line.compartment_id)].label
            if str(line.compartment_id) in any_row
            else "(retired compartment)",
            "required_count": line.required_count,
            "display_order": line.display_order,
        }
        for line in VehicleKitRequirement.objects.filter(version=version)
        .select_related("definition")
        .order_by("display_order", "id")
    ]


def _state(user, vehicle):
    org, now = str(user.organization_id), timezone.now()
    current, any_row = _compartments(org, vehicle)
    in_force = kit_list_effective_at(org, str(vehicle.pk), now)
    published = set(
        VehicleKitListPublication.objects.filter(version__vehicle=vehicle).values_list(
            "version_id", flat=True
        )
    )
    latest = VehicleKitListVersion.objects.filter(vehicle=vehicle).order_by("-revision").first()
    draft = latest if latest is not None and latest.pk not in published else None
    scheduled = list(
        VehicleKitListPublication.objects.filter(version__vehicle=vehicle, effective_at__gt=now)
        .select_related("version")
        .order_by("effective_at")
    )
    return {
        "compartments": current,
        "definitions": list(
            KitDefinition.objects.filter(organization_id=org, active=True).order_by("label", "key")
        ),
        "in_force": in_force,
        "in_force_lines": _lines(in_force, any_row),
        "scheduled": scheduled,
        "draft": draft,
        "draft_lines": _lines(draft, any_row),
    }


def _page(request, user, vehicle, *, status=200, errors=None, rows=None):
    state = _state(user, vehicle)
    source = state["draft_lines"] if state["draft"] else state["in_force_lines"]
    # A list longer than the editor holds is shown, not offered for editing: saving only part of
    # it would replace the draft with that part (PR #172 review).
    editable = len(source) <= MAX_ROWS
    if rows is None and editable:
        rows = [_with_note(line) for line in source]
        rows += [{} for _ in range(min(BLANK_ROWS, MAX_ROWS - len(source)))]
    context = {
        "title": f"Expected kit inventory: {vehicle.display_name}",
        "vehicle": vehicle,
        **state,
        "rows": rows or [],
        "editable": editable,
        "line_count": len(source),
        "max_rows": MAX_ROWS,
        "max_count": STORED_COUNT_MAX,
        "draft_operation_id": admin_pages.new_operation_id(),
        "publish_operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
        "zone": timezone.get_current_timezone_name(),
    }
    return render(request, "kits/kit_list.html", context, status=status)


def _with_note(line):
    """An editor row, saying why a line cannot be kept as it is.

    The pickers offer only active kit types and compartments not retired, so such a line shows
    no choice; the note names what it was, so the person can choose again or remove it.
    """
    row = dict(line)
    if not line["definition_active"]:
        row["note"] = f"{line['definition']} is no longer an active kit type"
        row["definition_id"] = ""
    if not line["compartment_id"]:
        row["note"] = "; ".join(filter(None, [row.get("note"), "its compartment is retired"]))
    if row.get("note"):
        row["note"] += ": choose again or remove this line."
    return row


def _posted_rows(post):
    """The editor's rows as typed, kept for redisplay when saving fails.

    The draft is replaced by exactly these rows, so a missing, malformed or out-of-range row
    count, or a row the form did not send, refuses the save instead of saving part of the list
    (PR #172 review).
    """
    count = post.get("rows", "")
    if not count.isascii() or not count.isdigit() or not 0 <= int(count) <= MAX_ROWS:
        raise KitListError(INVALID)
    count = int(count)
    if any(f"line-{index}-definition" not in post for index in range(count)):
        raise KitListError(INVALID)
    return [
        {
            "definition_id": post.get(f"line-{index}-definition", ""),
            "compartment_id": post.get(f"line-{index}-compartment", ""),
            "required_count": post.get(f"line-{index}-count", "").strip(),
            "display_order": post.get(f"line-{index}-order", "").strip(),
            "remove": post.get(f"line-{index}-remove") == "on",
        }
        for index in range(count)
    ]


def _lines_from(rows):
    """The complete lines to save: every row not removed and not left blank."""
    lines = []
    for index, row in enumerate(rows):
        if row["remove"] or not (row["definition_id"] or row["compartment_id"]):
            continue
        if not (row["definition_id"] and row["compartment_id"]):
            raise KitListError(f"line {index + 1}: choose both a kit and a compartment")
        lines.append(
            {
                "definition_id": row["definition_id"],
                "compartment_id": row["compartment_id"],
                "required_count": _whole(row["required_count"], f"line {index + 1} count"),
                "display_order": _whole(row["display_order"] or "0", f"line {index + 1} order"),
            }
        )
    return lines


def _whole(value, what):
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        raise KitListError(f"{what} must be a whole number")
    return int(value)


def _effective_at(post):
    return admin_pages.local_effective_at(post, KitListError)


def _command(request, user, vehicle, stage):
    operation_id = admin_pages.operation_id_from(request)
    if operation_id is None:
        return _page(
            request, user, vehicle, status=400, errors=["the form was not valid; try again"]
        )
    ids = {**_context_ids(user), "operation_id": operation_id}
    rows = None
    try:
        if stage == "draft":
            rows = _posted_rows(request.POST)
            save_vehicle_kit_list_draft(vehicle_id=str(vehicle.pk), lines=_lines_from(rows), **ids)
        else:
            version = request.POST.get("version_id", "")
            if not VehicleKitListVersion.objects.filter(pk=version, vehicle=vehicle).exists():
                raise KitListError("choose this vehicle's draft to publish")
            publish_vehicle_kit_list(
                version_id=version, effective_at=_effective_at(request.POST), **ids
            )
    except (KitListError, ValueError) as error:
        return _page(request, user, vehicle, status=400, errors=[str(error)], rows=rows)
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
        logger.error("kit list change unavailable (%s)", type(error).__name__)
        return _page(
            request,
            user,
            vehicle,
            status=503,
            errors=["the change could not be completed; nothing was changed. Try again."],
        )
    url = reverse("vehicle-kit-list", args=[vehicle.pk])
    return HttpResponseRedirect(url + "?" + urlencode({"saved": stage}))


@never_cache
@require_http_methods(["GET", "POST"])
def vehicle_kit_list(request, vehicle_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    vehicle = admin_pages.vehicle_of(user, vehicle_id)
    if request.method == "GET":
        return _page(request, user, vehicle)
    stage = request.POST.get("stage")
    if stage in ("draft", "publish"):
        return _command(request, user, vehicle, stage)
    return _page(request, user, vehicle, status=400, errors=["the request was not valid"])
