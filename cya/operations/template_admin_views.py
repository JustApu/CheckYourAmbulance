"""Checklist template screens (SPEC 4.3 CHK-2, CHK-3, 4.8 CFG-3; issue #130 part 6a).

Supervisors and Administrators (draft_edit_template) list the organization's checklist
templates, create one with its key (IMP-6) and edit its draft one item at a time: adding,
changing or removing an item saves a whole new draft revision through save_template_draft,
built from the template's latest version. A whole-list form would exceed Django's form field
limit for a checklist of about 150 items. Each save carries the revision it started from, so a
change saved by someone else meanwhile refuses the save instead of being lost.

Publishing (publish_template) is two steps: a read-only impact preview, then the confirmation,
which publish_template_version checks again under its locks. Duplicating a template
(duplicate_configuration, CFG-3) is part 4's duplicate_template. Each form names its own
action; the commands check it on the server.
"""

import logging
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.checklists.commands import publish_template_version, save_template_draft
from cya.checklists.models import (
    KINDS,
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
    Compartment,
    Item,
    KitInstance,
    VehicleSystem,
)
from cya.checklists.validation import ChecklistValidationError
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits.keys import KeyRuleError
from cya.kits.models import KitDefinition, PositionKey
from cya.kits.placements import placements_of
from cya.kits.views import UNAVAILABLE, _can, _context_ids
from cya.operations.duplication import _specs, duplicate_template
from cya.operations.templates import (
    TemplateError,
    create_template,
    publication_impact,
)
from cya.operations.vehicles import VehicleError
from cya.shell import navigation

logger = logging.getLogger(__name__)

ACTION = "draft_edit_template"
DENIAL = "MANAGE_CHECKLIST_TEMPLATES"
KIND_LABELS = {
    "PRESENCE_CONDITION": "Present and in good condition",
    "QUANTITY": "Quantity",
    "MEASUREMENT": "Measurement",
    "SPECIAL_OBSERVATION": "Special observation",
    "VEHICLE_READING": "Vehicle reading",
}
OPTIONAL_OUTCOMES = (("FAILED", "Failed"), ("UNABLE_TO_INSPECT", "Unable to inspect"))
SAVED = {
    "create": "Template created. Add its first item to start a draft.",
    "item": "Draft saved. Publish it to put it in force.",
    "remove": "Item removed from the draft.",
    "publish": "Version published.",
    "duplicate": "Template duplicated. Its draft copies the chosen version.",
}
REFUSALS = (
    ChecklistValidationError,
    TemplateError,
    VehicleError,
    KeyRuleError,
    ValueError,
)


VERSION_PAGE = 25  # versions per page of history, and offered for duplication (SPEC NFR-6)
SCHEDULED_LIMIT = 25
CHOICE_LIMIT = 500  # places and kits offered in the item form
ITEM_CHOICES = 200  # existing items offered at once; a name search reaches the rest
MAX_QUERY = 100


class PageError(ValueError):
    """A form problem found before any command runs."""


# Which field a refusal is about, by words in its message, so the page can show it next to that
# field as well as in the summary (docs/application-shell.md, "Forms"). First match wins.
ITEM_FIELDS = (
    ("display_order", ("order",)),
    ("expected_quantity", ("quantity",)),
    ("unit", ("unit",)),
    ("kit_id", ("kit",)),
    (
        "item",
        (
            "item's name",
            "new item",
            "existing item",
            "already in this checklist",
            "active item",
            "item_id",
            "presence item",
        ),
    ),
    ("place", ("compartment", "system", "slot", "retired", "where the item is")),
    ("kind", ("kind of check", "kind must")),
)
TEMPLATE_FIELDS = (("key", ("key",)), ("label", ("name",)), ("version_id", ("version",)))


def field_errors(errors, rules):
    """{field: message} for a single refusal that names a field of the form, else {}."""
    if len(errors) != 1:
        return {}
    message = str(errors[0])
    lowered = message.lower()
    for name, words in rules:
        if any(word in lowered for word in words):
            return {name: message}
    return {}


# --- reading ---------------------------------------------------------------------------------


def _template_of(user, template_id):
    template = ChecklistTemplate.objects.filter(
        organization_id=user.organization_id, pk=template_id
    ).first()
    if template is None:
        raise Http404("no such template")
    return template


def _is_draft(version):
    """True for a version no publication names."""
    return (
        version is not None
        and not ChecklistPublication.objects.filter(checklist_version=version).exists()
    )


def _revision_param(value):
    """A revision number from a query parameter, or None (SPEC NFR-6 keyset pages)."""
    value = value or ""
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 9 else None


def _versions(template, before=None):
    """One page of versions, newest first, older than revision `before` (SPEC NFR-6).

    Returns (rows with their publication, the revision to continue from, or None at the end).
    """
    versions = ChecklistVersion.objects.filter(template=template)
    if before is not None:
        versions = versions.filter(revision__lt=before)
    page = list(versions.order_by("-revision")[: VERSION_PAGE + 1])
    more = len(page) > VERSION_PAGE
    page = page[:VERSION_PAGE]
    publications = {
        p.checklist_version_id: p
        for p in ChecklistPublication.objects.filter(checklist_version__in=page)
    }
    rows = [{"version": v, "publication": publications.get(v.pk)} for v in page]
    return rows, (page[-1].revision if more else None)


def _latest(template):
    return ChecklistVersion.objects.filter(template=template).order_by("-revision").first()


def _labels_for(definitions):
    items = dict(
        Item.objects.filter(pk__in={d.item_id for d in definitions}).values_list("pk", "label")
    )
    compartments = dict(
        Compartment.objects.filter(
            pk__in={d.compartment_id for d in definitions if d.compartment_id}
        ).values_list("pk", "label")
    )
    systems = dict(
        VehicleSystem.objects.filter(
            pk__in={d.system_id for d in definitions if d.system_id}
        ).values_list("pk", "label")
    )
    kits = dict(
        KitInstance.objects.filter(pk__in={d.kit_id for d in definitions if d.kit_id}).values_list(
            "pk", "label"
        )
    )
    return items, compartments, systems, kits


def _rows(version):
    """A version's items for display, in display order."""
    if version is None:
        return []
    definitions = list(
        ChecklistItemDef.objects.filter(checklist_version=version).order_by("display_order", "id")
    )
    items, compartments, systems, kits = _labels_for(definitions)
    rows = []
    for d in definitions:
        if d.compartment_id:
            where = compartments.get(d.compartment_id, "")
            if d.kit_id:
                where = f"{where}, kit {kits.get(d.kit_id, '')}"
        else:
            where = systems.get(d.system_id, "")
        if d.kind == "QUANTITY":
            target = str(d.expected_quantity)
        elif d.unit:
            target = d.unit
        else:
            target = ""
        rows.append(
            {
                "item_id": str(d.item_id),
                "label": items.get(d.item_id, ""),
                "where": where,
                "kind": KIND_LABELS.get(d.kind, d.kind),
                "target": target,
                "outcomes": ", ".join(o.replace("_", " ").lower() for o in d.allowed_outcomes),
                "display_order": d.display_order,
            }
        )
    return rows


def _slot_choices(org, query="", keep=None):
    """(places, whether more match than are shown): one per compartment or system slot.

    A template definition names one vehicle's row; resolution maps it by stable slot to each
    vehicle's own row (cya.checklists.overlay.vehicle_slots). Each choice is a current row of
    the slot that is not retired. At most CHOICE_LIMIT slots are read (SPEC NFR-6); a name
    query narrows them, so every slot stays reachable. `keep` is the (kind, row id) of the
    item's current place, always offered when it is still in use.
    """
    now = timezone.now()
    choices, more = [], False
    for kind, heading, model in (
        ("COMPARTMENT", "Compartment", Compartment),
        ("SYSTEM", "System", VehicleSystem),
    ):
        rows = model.objects.filter(organization_id=org, effective_at__lte=now)
        matching = rows.filter(label__icontains=query) if query else rows
        slots = list(
            matching.order_by("stable_slot_id")
            .values_list("stable_slot_id", flat=True)
            .distinct()[: CHOICE_LIMIT + 1]
        )
        more = more or len(slots) > CHOICE_LIMIT
        slots = slots[:CHOICE_LIMIT]
        if keep and keep[0] == kind:
            kept = model.objects.filter(pk=keep[1]).values_list("stable_slot_id", flat=True)
            slots += [slot for slot in kept if slot not in slots]
        keys = dict(
            PositionKey.objects.filter(
                organization_id=org, kind=kind, stable_slot_id__in=slots
            ).values_list("stable_slot_id", "key")
        )
        newest = {}
        for row in rows.filter(stable_slot_id__in=slots).order_by(
            "stable_slot_id", "vehicle_id", "-revision"
        ):
            newest.setdefault((row.stable_slot_id, row.vehicle_id), row)
        offered = set()
        for (slot, _vehicle), row in newest.items():
            if row.retired or slot in offered:
                continue
            offered.add(slot)
            key = keys.get(slot)
            label = f"{row.label} ({key})" if key else row.label
            choices.append(
                {
                    "value": f"{kind}:{row.pk}",
                    "slot": (kind, str(slot)),
                    "label": f"{heading}: {label}",
                }
            )
    return sorted(choices, key=lambda c: c["label"].lower()), more


def _kit_choices(org, query="", keep=None):
    """(kits, whether more match than are shown), at most CHOICE_LIMIT; a query narrows them."""
    kits = KitInstance.objects.filter(organization_id=org, active=True, compartment__isnull=False)
    if query:
        kits = kits.filter(label__icontains=query)
    found = list(
        kits.select_related("compartment", "vehicle").order_by("label", "id")[: CHOICE_LIMIT + 1]
    )
    more = len(found) > CHOICE_LIMIT
    found = found[:CHOICE_LIMIT]
    if keep and all(str(k.pk) != keep for k in found):
        found += list(
            KitInstance.objects.filter(organization_id=org, pk=keep, active=True)
            .exclude(compartment__isnull=True)
            .select_related("compartment", "vehicle")
        )
    return [
        {
            "value": str(k.pk),
            "label": f"{k.label} ({k.vehicle.display_name}, {k.compartment.label})",
        }
        for k in found
    ], more


def _item_choices(org, exclude, query=""):
    """(active items to choose from, whether more match than are shown), at most ITEM_CHOICES.

    A query narrows the list by name, so every item stays reachable however many there are.
    """
    presence = KitDefinition.objects.filter(organization_id=org).values_list(
        "presence_item_id", flat=True
    )
    items = (
        Item.objects.filter(organization_id=org, active=True)
        .exclude(pk__in=presence)
        .exclude(pk__in=exclude)
    )
    if query:
        items = items.filter(label__icontains=query)
    found = list(items.order_by("label", "id").values("id", "label")[: ITEM_CHOICES + 1])
    return found[:ITEM_CHOICES], len(found) > ITEM_CHOICES


def _base(template):
    """(the latest version, its specs, its placements or None) an edit starts from."""
    latest = _latest(template)
    if latest is None:
        return None, [], None
    placed = [
        {"definition_id": str(p.definition_id), "display_order": p.display_order}
        for p in placements_of(latest.pk)
    ]
    return latest, _specs(latest), placed or None


def _list_page(request, user, *, status=200, errors=None, form=None):
    org = str(user.organization_id)
    now = timezone.now()
    rows = []
    for template in ChecklistTemplate.objects.filter(organization_id=org).order_by(
        "-active", "label", "id"
    )[: admin_pages.LIST_LIMIT]:
        latest = _latest(template)
        in_force = (
            ChecklistPublication.objects.filter(
                checklist_version__template=template, effective_at__lte=now
            )
            .order_by("-effective_at")
            .select_related("checklist_version")
            .first()
        )
        rows.append(
            {
                "template": template,
                "in_force": in_force.checklist_version.revision if in_force else None,
                "scheduled": ChecklistPublication.objects.filter(
                    checklist_version__template=template, effective_at__gt=now
                ).count(),
                "draft": latest.revision if _is_draft(latest) else None,
            }
        )
    context = {
        "title": "Checklist templates",
        "rows": rows,
        "list_limit": admin_pages.LIST_LIMIT,
        "form": form or {},
        "create_operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "field_errors": field_errors(errors or [], TEMPLATE_FIELDS) if form else {},
        "saved": SAVED.get(request.GET.get("saved", "")),
    }
    return render(request, "operations/admin_templates.html", context, status=status)


def _template_page(
    request, user, template, *, status=200, errors=None, impact=None, when=None, at=""
):
    roles = navigation.roles_for(user)
    now = timezone.now()
    latest = _latest(template)
    draft = latest if _is_draft(latest) else None
    versions, older = _versions(template, _revision_param(request.GET.get("before")))
    in_force = (
        ChecklistPublication.objects.filter(
            checklist_version__template=template, effective_at__lte=now
        )
        .order_by("-effective_at")
        .select_related("checklist_version")
        .first()
    )
    context = {
        "title": f"Checklist template: {template.label}",
        "template": template,
        "in_force": in_force,
        "in_force_rows": _rows(in_force.checklist_version) if in_force else [],
        "scheduled": ChecklistPublication.objects.filter(
            checklist_version__template=template, effective_at__gt=now
        )
        .select_related("checklist_version")
        .order_by("effective_at")[:SCHEDULED_LIMIT],
        "latest": latest,
        "draft": draft,
        "edit_rows": [
            {**row, "operation_id": admin_pages.new_operation_id()} for row in _rows(latest)
        ],
        "base_revision": latest.revision if latest else 0,
        "versions": versions,
        "older": older,
        "paged": request.GET.get("before") is not None,
        "can_publish": _can(user, roles, "publish_template"),
        "can_duplicate": _can(user, roles, "duplicate_configuration"),
        "impact": impact,
        "when": when,
        "at": at,
        "publish_operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
        "zone": timezone.get_current_timezone_name(),
    }
    return render(request, "operations/admin_template.html", context, status=status)


# --- commands --------------------------------------------------------------------------------


def _run(request, user, call, error_page):
    """Run a command; (None, the response to return) when it did not succeed."""
    operation_id = admin_pages.operation_id_from(request)
    if operation_id is None:
        return None, error_page(400, ["the form was not valid; reload the page and try again"])
    ids = {**_context_ids(user), "operation_id": operation_id}
    try:
        return call(ids), None
    except REFUSALS as error:
        return None, error_page(400, [str(error)])
    except CommandDenied:
        return None, render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return None, error_page(
            409, ["this form was already used for a different change; reload and try again"]
        )
    except UNAVAILABLE as error:
        logger.error("checklist template change unavailable (%s)", type(error).__name__)
        return None, error_page(
            503, ["the change could not be completed; nothing was changed. Try again."]
        )


def _saved(url_name, args, stage):
    return HttpResponseRedirect(reverse(url_name, args=args) + "?" + urlencode({"saved": stage}))


def _base_revision(post):
    value = post.get("base_revision", "")
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        raise PageError("the form was not valid; reload the page and try again")
    return int(value)


@never_cache
@require_http_methods(["GET", "POST"])
def templates(request):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    if request.method == "GET":
        return _list_page(request, user)
    if request.POST.get("stage") != "create":
        return _list_page(request, user, status=400, errors=["the request was not valid"])
    form = {"label": request.POST.get("label", ""), "key": request.POST.get("key", "")}

    def error_page(status, errors):
        return _list_page(request, user, status=status, errors=errors, form=form)

    result, refused = _run(
        request,
        user,
        lambda ids: create_template(label=form["label"], key=form["key"].strip(), **ids),
        error_page,
    )
    if refused:
        return refused
    return _saved("checklist-template", [result.body["template_id"]], "create")


@never_cache
@require_http_methods(["GET", "POST"])
def template(request, template_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    template = _template_of(user, template_id)
    if request.method == "GET":
        return _template_page(request, user, template)
    stage = request.POST.get("stage")
    if stage == "remove":
        return _remove(request, user, template)
    if stage in ("preview", "publish"):
        return _publish(request, user, template, stage)
    return _template_page(request, user, template, status=400, errors=["the request was not valid"])


def _remove(request, user, template):
    def error_page(status, errors):
        return _template_page(request, user, template, status=status, errors=errors)

    try:
        base_revision = _base_revision(request.POST)
    except PageError as error:
        return error_page(400, [str(error)])
    item_id = request.POST.get("item_id", "")
    latest, specs, placed = _base(template)
    kept = [s for s in specs if s["item_id"] != item_id]
    if latest is None or len(kept) == len(specs):
        return error_page(400, ["that item is not in the latest version; reload the page"])
    if not kept:
        return error_page(400, ["a version needs at least one item; add another item first"])
    _result, refused = _run(
        request,
        user,
        lambda ids: save_template_draft(
            template_id=str(template.pk),
            items=kept,
            placements=placed,
            base_revision=base_revision,
            **ids,
        ),
        error_page,
    )
    return refused or _saved("checklist-template", [template.pk], "remove")


def _draft_of(template, post):
    try:
        version = ChecklistVersion.objects.filter(
            template=template, pk=post.get("version_id", "")
        ).first()
    except (ValueError, ValidationError):  # a malformed id
        version = None
    if not _is_draft(version):
        raise PageError("choose this template's draft to publish")
    return version


def _publish(request, user, template, stage):
    when, at = request.POST.get("when", ""), request.POST.get("at", "")

    def error_page(status, errors):
        return _template_page(
            request, user, template, status=status, errors=errors, when=when, at=at
        )

    try:
        version = _draft_of(template, request.POST)
        effective_at = admin_pages.local_effective_at(request.POST, PageError)
    except (PageError, ValueError) as error:
        return error_page(400, [str(error)])
    if stage == "preview":
        try:
            impact = publication_impact(str(user.organization_id), str(version.pk), effective_at)
        except TemplateError as error:
            return error_page(400, [str(error)])
        return _template_page(request, user, template, impact=impact, when=when, at=at)
    _result, refused = _run(
        request,
        user,
        lambda ids: publish_template_version(
            version_id=str(version.pk), effective_at=effective_at, **ids
        ),
        error_page,
    )
    return refused or _saved("checklist-template", [template.pk], "publish")


# --- one item --------------------------------------------------------------------------------


def _item_form(spec, choices):
    """The item form's values from a spec, or the defaults for a new item."""
    if spec is None:
        return {
            "kind": "PRESENCE_CONDITION",
            "outcomes": {"FAILED", "UNABLE_TO_INSPECT"},
            "display_order": "",
        }
    place = _choice_for(spec, choices)
    return {
        "item_id": spec["item_id"],
        "kind": spec["kind"],
        "expected_quantity": "" if spec["expected_quantity"] is None else spec["expected_quantity"],
        "unit": spec["unit"] or "",
        "instructions": spec["instructions"],
        "outcomes": set(spec["allowed_outcomes"]),
        "expiration_tracked": spec["expiration_tracked"],
        "place": place,
        "kit_id": spec["kit_id"] or "",
        "display_order": spec["display_order"],
    }


def _choice_for(spec, choices):
    """The place choice for the slot a spec's row belongs to, or "" when it is not offered."""
    for kind, model, row_id in (
        ("COMPARTMENT", Compartment, spec["compartment_id"]),
        ("SYSTEM", VehicleSystem, spec["system_id"]),
    ):
        if row_id:
            slot = model.objects.filter(pk=row_id).values_list("stable_slot_id", flat=True).first()
            wanted = (kind, str(slot))
            return next((c["value"] for c in choices if c["slot"] == wanted), "")
    return ""


def _whole(value, what, *, required=True):
    value = (value or "").strip()
    if not value and not required:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        raise PageError(f"{what} must be a whole number")
    return int(value)


def _spec_from(post, item_id, specs):
    """The posted item as a draft spec, naming a new item by label when one was typed."""
    kind = post.get("kind", "")
    if kind not in KINDS:
        raise PageError("choose what kind of check this item is")
    place = post.get("place", "")
    kind_of_place, _, row_id = place.partition(":")
    if kind_of_place not in ("COMPARTMENT", "SYSTEM") or not row_id:
        raise PageError("choose where the item is: a compartment or a system")
    kit_id = post.get("kit_id", "") or None
    if kit_id and kind_of_place != "COMPARTMENT":
        raise PageError("a kit is in a compartment; choose a compartment or no kit")
    order = post.get("display_order", "").strip()
    spec = {
        "display_order": _whole(order, "order") if order else _next_order(specs),
        "kind": kind,
        "instructions": post.get("instructions", "").replace("\r\n", "\n"),
        "expected_quantity": _whole(post.get("expected_quantity"), "quantity")
        if kind == "QUANTITY"
        else None,
        "unit": post.get("unit", "").strip()
        if kind in ("MEASUREMENT", "VEHICLE_READING")
        else None,
        "allowed_outcomes": ["PASSED"]
        + [code for code, _label in OPTIONAL_OUTCOMES if post.get(f"outcome-{code}") == "on"],
        "expiration_tracked": post.get("expiration_tracked") == "on",
        "compartment_id": row_id if kind_of_place == "COMPARTMENT" else None,
        "system_id": row_id if kind_of_place == "SYSTEM" else None,
        "kit_id": kit_id,
    }
    if item_id is not None:
        return {"item_id": item_id, **spec}
    chosen, typed = post.get("item_id", ""), post.get("new_item", "").strip()
    if bool(chosen) == bool(typed):
        raise PageError("choose an existing item or type a new item's name, not both")
    if chosen:
        if any(s["item_id"] == chosen for s in specs):
            raise PageError("that item is already in this checklist; edit it instead")
        return {"item_id": chosen, **spec}
    return {"new_item": typed, **spec}


def _next_order(specs):
    return max((s["display_order"] for s in specs), default=0) + 10


def _item_page(request, user, template, item_id, *, status=200, errors=None, form=None):
    org = str(user.organization_id)
    latest, specs, _placed = _base(template)
    spec = next((s for s in specs if s["item_id"] == item_id), None)
    if item_id is not None and spec is None:
        raise Http404("that item is not in the latest version")
    place_query = request.GET.get("p", "").strip()[:MAX_QUERY]
    # The item's current place and kit stay on offer whatever the search shows.
    keep_place = keep_kit = None
    if form is not None:
        kind, _, row = form.get("place", "").partition(":")
        keep_place = (kind, row) if kind in ("COMPARTMENT", "SYSTEM") and row else None
        keep_kit = form.get("kit_id") or None
    elif spec is not None:
        keep_place = (
            ("COMPARTMENT", spec["compartment_id"])
            if spec["compartment_id"]
            else ("SYSTEM", spec["system_id"])
        )
        keep_kit = spec["kit_id"]
    try:
        choices, more_places = _slot_choices(org, place_query, keep_place)
        kits, more_kits = _kit_choices(org, place_query, keep_kit)
    except (ValueError, ValidationError):  # a malformed posted id
        choices, more_places = _slot_choices(org, place_query)
        kits, more_kits = _kit_choices(org, place_query)
    if form is None:
        form = _item_form(spec, choices)
    item_label = Item.objects.filter(pk=item_id).values_list("label", flat=True).first()
    query = request.GET.get("q", "").strip()[:MAX_QUERY]
    items, more_items = (
        _item_choices(org, [s["item_id"] for s in specs], query) if item_id is None else ([], False)
    )
    context = {
        "title": f"{'Edit' if item_id else 'Add'} an item: {template.label}",
        "template": template,
        "item_id": item_id,
        "item_label": item_label,
        "latest": latest,
        "base_revision": latest.revision if latest else 0,
        "form": form,
        "kinds": KIND_LABELS.items(),
        "optional_outcomes": OPTIONAL_OUTCOMES,
        "places": choices,
        "kits": kits,
        "more_places": more_places or more_kits,
        "place_query": place_query,
        "choice_limit": CHOICE_LIMIT,
        "items": items,
        "more_items": more_items,
        "query": query,
        "item_choices": ITEM_CHOICES,
        "operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "field_errors": field_errors(errors or [], ITEM_FIELDS),
    }
    return render(request, "operations/admin_template_item.html", context, status=status)


def _posted_form(post):
    return {
        "item_id": post.get("item_id", ""),
        "new_item": post.get("new_item", ""),
        "kind": post.get("kind", ""),
        "expected_quantity": post.get("expected_quantity", ""),
        "unit": post.get("unit", ""),
        "instructions": post.get("instructions", ""),
        "outcomes": {code for code, _l in OPTIONAL_OUTCOMES if post.get(f"outcome-{code}") == "on"}
        | {"PASSED"},
        "expiration_tracked": post.get("expiration_tracked") == "on",
        "place": post.get("place", ""),
        "kit_id": post.get("kit_id", ""),
        "display_order": post.get("display_order", ""),
    }


@never_cache
@require_http_methods(["GET", "POST"])
def template_item(request, template_id, item_id=None):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    template = _template_of(user, template_id)
    item_id = str(item_id) if item_id is not None else None
    if request.method == "GET":
        return _item_page(request, user, template, item_id)
    form = _posted_form(request.POST)

    def error_page(status, errors):
        return _item_page(request, user, template, item_id, status=status, errors=errors, form=form)

    _latest_version, specs, placed = _base(template)
    try:
        base_revision = _base_revision(request.POST)
        posted = _spec_from(request.POST, item_id, specs)
    except PageError as error:
        return error_page(400, [str(error)])
    if item_id is None:
        items = [*specs, posted]
    else:
        items = [posted if s["item_id"] == item_id else s for s in specs]
    _result, refused = _run(
        request,
        user,
        lambda ids: save_template_draft(
            template_id=str(template.pk),
            items=items,
            placements=placed,
            base_revision=base_revision,
            **ids,
        ),
        error_page,
    )
    return refused or _saved("checklist-template", [template.pk], "item")


# --- duplicate -------------------------------------------------------------------------------


@never_cache
@require_http_methods(["GET", "POST"])
def duplicate(request, template_id):
    allowed, refused = admin_pages.allowed(
        request, "duplicate_configuration", "DUPLICATE_CONFIGURATION"
    )
    if refused:
        return refused
    user, _roles = allowed
    template = _template_of(user, template_id)
    # The newest versions are offered in a list; any older one is chosen by its revision number
    # (SPEC NFR-6: the list is bounded, every version stays reachable).
    versions, older = _versions(template)
    form = {
        "version_id": request.POST.get("version_id", ""),
        "revision": request.POST.get("revision", "").strip(),
        "label": request.POST.get("label", f"Copy of {template.label}"),
        "key": request.POST.get("key", ""),
    }

    def page(status=200, errors=None):
        context = {
            "title": f"Duplicate a template: {template.label}",
            "template": template,
            "versions": versions,
            "older": older,
            "form": form,
            "operation_id": admin_pages.new_operation_id(),
            "errors": errors or [],
            "field_errors": field_errors(errors or [], TEMPLATE_FIELDS),
        }
        return render(request, "operations/admin_template_duplicate.html", context, status=status)

    if request.method == "GET":
        return page()
    chosen = ChecklistVersion.objects.filter(template=template)
    if form["revision"]:
        number = _revision_param(form["revision"])
        chosen = chosen.filter(revision=number) if number is not None else chosen.none()
    else:
        chosen = chosen.filter(pk=form["version_id"]) if form["version_id"] else chosen.none()
    try:
        source = chosen.first()
    except (ValueError, ValidationError):  # a malformed id
        source = None
    if source is None:
        return page(400, ["choose a version of this template to copy"])
    result, refused = _run(
        request,
        user,
        lambda ids: duplicate_template(
            source_version_id=str(source.pk),
            label=form["label"],
            key=form["key"].strip(),
            **ids,
        ),
        lambda status, errors: page(status, errors),
    )
    if refused:
        return refused
    return _saved("checklist-template", [result.body["template_id"]], "duplicate")
