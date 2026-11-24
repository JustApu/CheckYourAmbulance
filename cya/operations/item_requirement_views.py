"""A vehicle's item requirements screen (SPEC 6.0.0 CHK-11, AC-42 (4); issue #256 C4).

Whoever publishes the vehicle's checklists (publish_template, SPEC 3.3) lists its item
requirements, each with its place, values and the checklists that inspect it, and changes one
by publishing a new version through publish_item_requirement_version: from its effective
instant every checklist that inspects that stock uses the new values or place. Two checklists
that share a requirement cannot change it through their own drafts (AC-42 (1)); this page is
how they change it together. The command checks the permission and every rule again.
"""

from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.checklists import item_requirements as rules
from cya.checklists.commands import publish_item_requirement_version
from cya.checklists.models import Item, VehicleItemRequirementVersion
from cya.kits import admin_pages
from cya.operations.template_admin_views import (
    KIND_LABELS,
    OPTIONAL_OUTCOMES,
    PageError,
    _item_form,
    _run,
    _saved,
    _slot_choices,
    _spec_from,
)
from cya.operations.templates import place_label

ACTION = "publish_template"
DENIAL = "MANAGE_CHECKLIST_TEMPLATES"


def _versions(vehicle, limit):
    """{requirement id: its versions in effective order} for at most `limit` requirements."""
    found = {}
    for version in VehicleItemRequirementVersion.objects.filter(vehicle=vehicle).order_by(
        "item_id", "requirement_id", "effective_at"
    ):
        key = str(version.requirement_id)
        if key not in found and len(found) >= limit + 1:
            continue
        found.setdefault(key, []).append(version)
    return found


def _summary(version):
    if version.kind == "QUANTITY":
        target = f"quantity {version.expected_quantity}"
    elif version.unit:
        target = version.unit
    else:
        target = ""
    return ", ".join(filter(None, [KIND_LABELS.get(version.kind, version.kind), target]))


def _rows(vehicle, now):
    versions = _versions(vehicle, admin_pages.LIST_LIMIT)
    more = len(versions) > admin_pages.LIST_LIMIT
    labels = dict(
        Item.objects.filter(pk__in={chain[0].item_id for chain in versions.values()}).values_list(
            "pk", "label"
        )
    )
    rows = []
    for requirement_id, chain in list(versions.items())[: admin_pages.LIST_LIMIT]:
        current = [v for v in chain if v.effective_at <= now][-1:]
        later = [v for v in chain if v.effective_at > now]
        shown = current[0] if current else later[0]
        rows.append(
            {
                "requirement_id": requirement_id,
                "item": labels.get(shown.item_id, ""),
                "place": place_label(str(vehicle.pk), rules._version_place(shown), now),
                "summary": _summary(shown),
                "revision": shown.revision,
                "in_force": bool(current),
                "effective_at": shown.effective_at,
                "later": [
                    {
                        "revision": v.revision,
                        "effective_at": v.effective_at,
                        "place": place_label(str(vehicle.pk), rules._version_place(v), now),
                        "summary": _summary(v),
                    }
                    for v in (later if current else later[1:])
                ],
                "checklists": rules.other_users(requirement_id, None, now),
            }
        )
    rows.sort(key=lambda r: (r["item"].lower(), r["place"].lower()))
    return rows, more


def _latest(vehicle, requirement_id):
    return (
        VehicleItemRequirementVersion.objects.filter(vehicle=vehicle, requirement_id=requirement_id)
        .order_by("-effective_at")
        .first()
    )


def _spec(version):
    return {
        "item_id": str(version.item_id),
        "kind": version.kind,
        "expected_quantity": version.expected_quantity,
        "unit": version.unit,
        "instructions": version.instructions,
        "allowed_outcomes": list(version.allowed_outcomes),
        "expiration_tracked": version.expiration_tracked,
        "compartment_id": str(version.compartment_id) if version.compartment_id else None,
        "system_id": str(version.system_id) if version.system_id else None,
        "kit_id": None,
        "display_order": "",
    }


def _page(
    request, user, vehicle, *, editing=None, status=200, errors=None, form=None, when="", at=""
):
    org = str(user.organization_id)
    now = timezone.now()
    rows, more = _rows(vehicle, now)
    edit = None
    if editing:
        latest = _latest(vehicle, editing)
        if latest is not None:
            spec = _spec(latest)
            keep = (
                ("COMPARTMENT", spec["compartment_id"])
                if spec["compartment_id"]
                else ("SYSTEM", spec["system_id"])
                if spec["system_id"]
                else None
            )
            places, _more = _slot_choices(org, "", keep, str(vehicle.pk))
            edit = {
                "requirement_id": str(latest.requirement_id),
                "item": Item.objects.filter(pk=latest.item_id)
                .values_list("label", flat=True)
                .first(),
                "revision": latest.revision,
                "effective_at": latest.effective_at,
                "form": form or _item_form(spec, places),
                "places": places,
                "checklists": rules.other_users(latest.requirement_id, None, now),
            }
    context = {
        "title": f"Item requirements: {vehicle.display_name}",
        "vehicle": vehicle,
        "uses_requirements": rules.has_cutover(org),
        "rows": rows,
        "more": more,
        "list_limit": admin_pages.LIST_LIMIT,
        "edit": edit,
        "kinds": KIND_LABELS.items(),
        "optional_outcomes": OPTIONAL_OUTCOMES,
        "not_placed": "NOT_PLACED",
        "when": when,
        "at": at,
        "zone": timezone.get_current_timezone_name(),
        "operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "saved": "Item requirement published." if request.GET.get("saved") == "publish" else None,
    }
    return render(request, "operations/admin_item_requirements.html", context, status=status)


def _posted_form(post):
    return {
        "kind": post.get("kind", ""),
        "expected_quantity": post.get("expected_quantity", ""),
        "unit": post.get("unit", ""),
        "instructions": post.get("instructions", ""),
        "outcomes": {code for code, _l in OPTIONAL_OUTCOMES if post.get(f"outcome-{code}") == "on"}
        | {"PASSED"},
        "expiration_tracked": post.get("expiration_tracked") == "on",
        "place": post.get("place", ""),
    }


@never_cache
@require_http_methods(["GET", "POST"])
def item_requirements(request, vehicle_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    vehicle = admin_pages.vehicle_of(user, vehicle_id)
    if request.method == "GET":
        return _page(request, user, vehicle, editing=request.GET.get("edit"))
    requirement_id = request.POST.get("requirement_id", "")
    when, at = request.POST.get("when", ""), request.POST.get("at", "")

    def error_page(status, errors):
        return _page(
            request,
            user,
            vehicle,
            editing=requirement_id,
            status=status,
            errors=errors,
            form=_posted_form(request.POST),
            when=when,
            at=at,
        )

    if _latest(vehicle, requirement_id or None) is None:
        return error_page(400, ["choose one of this vehicle's item requirements"])
    try:
        spec = _spec_from(request.POST, requirement_id, [], allow_unplaced=True)
        if spec["kit_id"]:
            raise PageError("an item requirement is never in a kit; choose no kit")
        effective_at = admin_pages.local_effective_at(request.POST, PageError)
    except (PageError, ValueError) as error:
        return error_page(400, [str(error)])
    _result, refused = _run(
        request,
        user,
        lambda ids: publish_item_requirement_version(
            requirement_id=requirement_id,
            kind=spec["kind"],
            allowed_outcomes=spec["allowed_outcomes"],
            instructions=spec["instructions"],
            expected_quantity=spec["expected_quantity"],
            unit=spec["unit"],
            expiration_tracked=spec["expiration_tracked"],
            compartment_id=spec["compartment_id"],
            system_id=spec["system_id"],
            effective_at=effective_at,
            **ids,
        ),
        error_page,
    )
    return refused or _saved("vehicle-item-requirements", [vehicle.pk], "publish")
