"""Kit definition screens (SPEC 4.6 KDEF-1, KDEF-2, KDEF-5, 4.8 CFG-6; issue #130 part 7).

Supervisors and Administrators (manage_kit_definitions) list the organization's kit
definitions, create one with its key (KDEF-1, set once), and edit its draft one content entry at
a time: each change saves a whole new draft revision through save_kit_definition_draft, built
from the latest version, carrying the revision it started from so a concurrent change is refused
rather than lost. Publishing is a preview, then the confirmation; duplicating copies a version.

Deactivating or reactivating (set_kit_definition_active) is two steps (CFG-6): the first lists
where the definition is used, read in bounded queries (SPEC NFR-6), and the confirmation carries
a digest of that list and is refused if it changed. Kit instances keep their definition (KDEF-2).
"""

import logging

from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.persistence import digest
from cya.checklists.models import KINDS, ChecklistPublication, Item
from cya.checklists.overlay import effective_template_version
from cya.kits import admin_pages
from cya.kits.definitions import (
    create_kit_definition,
    duplicate_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
    set_kit_definition_active,
)
from cya.kits.kit_lists import kit_list_effective_at
from cya.kits.models import (
    KIT_KINDS,
    ChecklistKitPlacement,
    KitDefinition,
    KitDefinitionItem,
    KitDefinitionPublication,
    KitDefinitionVersion,
    KitInstanceDefinition,
    VehicleKitListPublication,
    VehicleKitRequirement,
)
from cya.kits.views import _can
from cya.operations.template_admin_views import (
    ITEM_CHOICES,
    KIND_LABELS,
    MAX_QUERY,
    OPTIONAL_OUTCOMES,
    SCHEDULED_LIMIT,
    VERSION_PAGE,
    PageError,
    _base_revision,
    _item_choices,
    _revision_param,
    _run,
    _saved,
    _whole,
    field_errors,
)
from cya.shell import navigation

logger = logging.getLogger(__name__)

ACTION = "manage_kit_definitions"
DENIAL = "MANAGE_KIT_DEFINITIONS"
USE_LIMIT = 25  # uses of each kind listed in the activity preview (SPEC NFR-6)
KIT_FIELDS = (("label", ("label", "name")), ("version_id", ("version",)))
CONTENT_FIELDS = (
    "item_id",
    "display_order",
    "kind",
    "instructions",
    "expected_quantity",
    "unit",
    "allowed_outcomes",
    "expiration_tracked",
)
SAVED = {
    "create": "Kit definition created. Add its first content item to start a draft.",
    "content": "Draft saved. Publish it to put it in force.",
    "remove": "Content item removed from the draft.",
    "publish": "Version published.",
    "duplicate": "Kit definition duplicated. Its draft copies the chosen version.",
    "active": "Kit definition reactivated.",
    "inactive": "Kit definition deactivated. Kit instances keep it.",
}
CONTENT_RULES = (
    ("display_order", ("order",)),
    ("expected_quantity", ("quantity",)),
    ("unit", ("unit",)),
    ("item", ("item's name", "new item", "existing item", "already in", "active item", "item_id")),
    ("kind", ("kind",)),
)


# --- reading ---------------------------------------------------------------------------------


def _definition_of(user, definition_id):
    definition = KitDefinition.objects.filter(
        organization_id=user.organization_id, pk=definition_id
    ).first()
    if definition is None:
        raise Http404("no such kit definition")
    return definition


def _latest(definition):
    return KitDefinitionVersion.objects.filter(definition=definition).order_by("-revision").first()


def _is_draft(version):
    return (
        version is not None
        and not KitDefinitionPublication.objects.filter(version=version).exists()
    )


def _contents(version):
    """A version's contents as save_kit_definition_draft takes them, in display order."""
    if version is None:
        return []
    rows = []
    for row in KitDefinitionItem.objects.filter(version=version).order_by("display_order", "id"):
        spec = {name: getattr(row, name) for name in CONTENT_FIELDS}
        spec["item_id"] = str(spec["item_id"])
        spec["allowed_outcomes"] = list(spec["allowed_outcomes"])
        rows.append(spec)
    return rows


def _rows(version):
    specs = _contents(version)
    labels = {
        str(pk): label
        for pk, label in Item.objects.filter(pk__in=[s["item_id"] for s in specs]).values_list(
            "pk", "label"
        )
    }
    return [
        {
            **spec,
            "label": labels.get(spec["item_id"], ""),
            "kind_label": KIND_LABELS.get(spec["kind"], spec["kind"]),
            "target": str(spec["expected_quantity"])
            if spec["kind"] == "QUANTITY"
            else (spec["unit"] or ""),
            "outcomes_text": ", ".join(
                o.replace("_", " ").lower() for o in spec["allowed_outcomes"]
            ),
        }
        for spec in specs
    ]


def _versions(definition, before=None):
    versions = KitDefinitionVersion.objects.filter(definition=definition)
    if before is not None:
        versions = versions.filter(revision__lt=before)
    page = list(versions.order_by("-revision")[: VERSION_PAGE + 1])
    more = len(page) > VERSION_PAGE
    page = page[:VERSION_PAGE]
    publications = {
        p.version_id: p for p in KitDefinitionPublication.objects.filter(version__in=page)
    }
    return [{"version": v, "publication": publications.get(v.pk)} for v in page], (
        page[-1].revision if more else None
    )


def definition_uses(org, definition, now=None):
    """Where a kit definition is in use now or later, read in bounded queries (SPEC NFR-6).

    - templates whose version in force, or one published for later, places the definition;
    - vehicles whose kit list in force, or one published for later, has a line for it;
    - active kit instances of it.
    Each list holds at most USE_LIMIT entries and says when there are more.
    """
    now = now or timezone.now()
    placed = ChecklistKitPlacement.objects.filter(organization_id=org, definition=definition)
    template_ids = list(
        ChecklistPublication.objects.filter(
            organization_id=org,
            checklist_version_id__in=placed.values("checklist_version_id"),
        )
        .order_by("checklist_version__template_id")
        .values_list("checklist_version__template_id", flat=True)
        .distinct()[: USE_LIMIT * 4]
    )
    templates = []
    for template_id in template_ids:
        in_force = effective_template_version(org, template_id, now)
        later = ChecklistPublication.objects.filter(
            checklist_version__template_id=template_id,
            effective_at__gt=now,
            checklist_version__in=placed.values("checklist_version_id"),
        ).exists()
        if later or (in_force and placed.filter(checklist_version=in_force).exists()):
            templates.append(str(template_id))
    lines = VehicleKitRequirement.objects.filter(organization_id=org, definition=definition)
    vehicle_ids = list(
        VehicleKitListPublication.objects.filter(
            organization_id=org, version_id__in=lines.values("version_id")
        )
        .order_by("version__vehicle_id")
        .values_list("version__vehicle_id", flat=True)
        .distinct()[: USE_LIMIT * 4]
    )
    vehicles = []
    for vehicle_id in vehicle_ids:
        in_force = kit_list_effective_at(org, str(vehicle_id), now)
        later = VehicleKitListPublication.objects.filter(
            version__vehicle_id=vehicle_id,
            effective_at__gt=now,
            version_id__in=lines.values("version_id"),
        ).exists()
        if later or (in_force and lines.filter(version=in_force).exists()):
            vehicles.append(str(vehicle_id))
    kits = KitInstanceDefinition.objects.filter(
        organization_id=org, definition=definition, kit__active=True
    )
    kit_labels = list(kits.order_by("kit__label").values_list("kit__label", flat=True)[:USE_LIMIT])
    from cya.checklists.models import ChecklistTemplate, Vehicle

    return {
        "templates": list(
            ChecklistTemplate.objects.filter(pk__in=templates[:USE_LIMIT])
            .order_by("label")
            .values_list("label", flat=True)
        ),
        "more_templates": len(templates) > USE_LIMIT or len(template_ids) >= USE_LIMIT * 4,
        "vehicles": list(
            Vehicle.objects.filter(pk__in=vehicles[:USE_LIMIT])
            .order_by("display_name")
            .values_list("display_name", flat=True)
        ),
        "more_vehicles": len(vehicles) > USE_LIMIT or len(vehicle_ids) >= USE_LIMIT * 4,
        "kits": kit_labels,
        "kit_count": kits.count(),
    }


def _uses_token(uses):
    return digest(uses)


# --- pages -----------------------------------------------------------------------------------


def _op():
    return admin_pages.new_operation_id()


def _generated_key(label, operation_id):
    """Stable machine reference for UI-created definitions; unchanged for an idempotent retry."""
    base = slugify(label)[:75].rstrip("-") or "kit"
    suffix = str(operation_id).replace("-", "").lower()[:24]
    return f"{base}-{suffix}"


def _list_page(request, user, *, status=200, errors=None, form=None):
    org = str(user.organization_id)
    now = timezone.now()
    rows = []
    for definition in KitDefinition.objects.filter(organization_id=org).order_by(
        "-active", "label", "key"
    )[: admin_pages.LIST_LIMIT]:
        latest = _latest(definition)
        in_force = (
            KitDefinitionPublication.objects.filter(
                version__definition=definition, effective_at__lte=now
            )
            .order_by("-effective_at")
            .select_related("version")
            .first()
        )
        rows.append(
            {
                "definition": definition,
                "in_force": in_force.version.revision if in_force else None,
                "scheduled": KitDefinitionPublication.objects.filter(
                    version__definition=definition, effective_at__gt=now
                ).count(),
                "draft": latest.revision if _is_draft(latest) else None,
            }
        )
    context = {
        "title": "Kit definitions",
        "rows": rows,
        "list_limit": admin_pages.LIST_LIMIT,
        "form": form or {},
        "create_operation_id": _op(),
        "errors": errors or [],
        "field_errors": field_errors(errors or [], KIT_FIELDS) if form else {},
        "saved": SAVED.get(request.GET.get("saved", "")),
    }
    return render(request, "kits/admin_definitions.html", context, status=status)


def _page(request, user, definition, *, status=200, errors=None, publish=None, activity=None):
    roles = navigation.roles_for(user)
    now = timezone.now()
    latest = _latest(definition)
    draft = latest if _is_draft(latest) else None
    in_force = (
        KitDefinitionPublication.objects.filter(
            version__definition=definition, effective_at__lte=now
        )
        .order_by("-effective_at")
        .select_related("version")
        .first()
    )
    versions, older = _versions(definition, _revision_param(request.GET.get("before")))
    context = {
        "title": f"Kit definition: {definition.label}",
        "definition": definition,
        "in_force": in_force,
        "in_force_rows": _rows(in_force.version) if in_force else [],
        "scheduled": KitDefinitionPublication.objects.filter(
            version__definition=definition, effective_at__gt=now
        )
        .select_related("version")
        .order_by("effective_at")[:SCHEDULED_LIMIT],
        "latest": latest,
        "draft": draft,
        "edit_rows": [{**row, "operation_id": _op()} for row in _rows(latest)],
        "base_revision": latest.revision if latest else 0,
        "versions": versions,
        "older": older,
        "paged": request.GET.get("before") is not None,
        "can_publish": _can(user, roles, ACTION),
        "publish": publish,
        "activity": activity,
        "publish_operation_id": _op(),
        "activity_operation_id": _op(),
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
        "zone": timezone.get_current_timezone_name(),
    }
    return render(request, "kits/admin_definition.html", context, status=status)


@never_cache
@require_http_methods(["GET", "POST"])
def definitions(request):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    if request.method == "GET":
        return _list_page(request, user)
    if request.POST.get("stage") != "create":
        return _list_page(request, user, status=400, errors=["the request was not valid"])
    form = {"label": request.POST.get("label", "")}
    operation_id = request.POST.get("operation_id", "")
    result, refused = _run(
        request,
        user,
        lambda ids: create_kit_definition(
            key=_generated_key(form["label"], operation_id), label=form["label"], **ids
        ),
        lambda status, errors: _list_page(request, user, status=status, errors=errors, form=form),
    )
    if refused:
        return refused
    return _saved("kit-definition", [result.body["definition_id"]], "create")


@never_cache
@require_http_methods(["GET", "POST"])
def definition(request, definition_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    found = _definition_of(user, definition_id)
    if request.method == "GET":
        return _page(request, user, found)

    def error_page(status, errors):
        return _page(request, user, found, status=status, errors=errors)

    stage = request.POST.get("stage")
    if stage == "remove":
        return _remove(request, user, found, error_page)
    if stage in ("publish-preview", "publish"):
        return _publish(request, user, found, stage, error_page)
    if stage in ("activity-preview", "activity"):
        return _activity(request, user, found, stage, error_page)
    return error_page(400, ["the request was not valid"])


def _save(request, user, found, contents, error_page, stage):
    try:
        base_revision = _base_revision(request.POST)
    except PageError as error:
        return error_page(400, [str(error)])
    if not contents:
        return error_page(400, ["a kit definition needs at least one content item"])
    _result, refused = _run(
        request,
        user,
        lambda ids: save_kit_definition_draft(
            definition_id=str(found.pk), contents=contents, base_revision=base_revision, **ids
        ),
        error_page,
    )
    return refused or _saved("kit-definition", [found.pk], stage)


def _remove(request, user, found, error_page):
    item_id = request.POST.get("item_id", "")
    contents = _contents(_latest(found))
    kept = [c for c in contents if c["item_id"] != item_id]
    if len(kept) == len(contents):
        return error_page(400, ["that item is not in the latest version; reload the page"])
    return _save(request, user, found, kept, error_page, "remove")


def _draft_of(found, post):
    try:
        version = KitDefinitionVersion.objects.filter(
            definition=found, pk=post.get("version_id", "")
        ).first()
    except (ValueError, ValidationError):
        version = None
    if not _is_draft(version):
        raise PageError("choose this kit definition's draft to publish")
    return version


def _publish(request, user, found, stage, error_page):
    try:
        version = _draft_of(found, request.POST)
        effective_at = admin_pages.local_effective_at(request.POST, PageError)
    except PageError as error:
        return error_page(400, [str(error)])
    if stage == "publish-preview":
        publish = {
            "version": version,
            "when": request.POST.get("when", ""),
            "at": request.POST.get("at", ""),
            "effective_at": effective_at,
            "uses": definition_uses(str(user.organization_id), found),
        }
        return _page(request, user, found, publish=publish)
    _result, refused = _run(
        request,
        user,
        lambda ids: publish_kit_definition_version(
            version_id=str(version.pk), effective_at=effective_at, **ids
        ),
        error_page,
    )
    return refused or _saved("kit-definition", [found.pk], "publish")


def _activity(request, user, found, stage, error_page):
    wanted = request.POST.get("active")
    if wanted not in ("true", "false") or (wanted == "true") == found.active:
        return error_page(400, ["the kit definition is already in that state; reload the page"])
    active = wanted == "true"
    uses = definition_uses(str(user.organization_id), found)
    if stage == "activity-preview":
        return _page(
            request,
            user,
            found,
            activity={"active": active, "uses": uses, "token": _uses_token(uses)},
        )
    if request.POST.get("token") != _uses_token(uses):
        return _page(
            request,
            user,
            found,
            status=400,
            errors=["where this kit definition is used has changed; check it again"],
            activity={"active": active, "uses": uses, "token": _uses_token(uses)},
        )
    _result, refused = _run(
        request,
        user,
        lambda ids: set_kit_definition_active(definition_id=str(found.pk), active=active, **ids),
        error_page,
    )
    return refused or _saved("kit-definition", [found.pk], "active" if active else "inactive")


# --- one content item ------------------------------------------------------------------------


def _content_form(spec):
    if spec is None:
        return {"kind": "PRESENCE_CONDITION", "outcomes": {"FAILED", "UNABLE_TO_INSPECT"}}
    return {
        "kind": spec["kind"],
        "expected_quantity": "" if spec["expected_quantity"] is None else spec["expected_quantity"],
        "unit": spec["unit"] or "",
        "instructions": spec["instructions"],
        "outcomes": set(spec["allowed_outcomes"]),
        "expiration_tracked": spec["expiration_tracked"],
        "display_order": spec["display_order"],
    }


def _posted(post):
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
        "display_order": post.get("display_order", ""),
    }


def _content_from(post, item_id, contents):
    kind = post.get("kind", "")
    if kind not in KIT_KINDS:
        raise PageError("choose what kind of check this item is")
    order = post.get("display_order", "").strip()
    spec = {
        "display_order": _whole(order, "order")
        if order
        else max((c["display_order"] for c in contents), default=0) + 10,
        "kind": kind,
        "instructions": post.get("instructions", "").replace("\r\n", "\n"),
        "expected_quantity": _whole(post.get("expected_quantity"), "quantity")
        if kind == "QUANTITY"
        else None,
        "unit": post.get("unit", "").strip() if kind == "MEASUREMENT" else None,
        "allowed_outcomes": ["PASSED"]
        + [code for code, _label in OPTIONAL_OUTCOMES if post.get(f"outcome-{code}") == "on"],
        "expiration_tracked": post.get("expiration_tracked") == "on",
    }
    if item_id is not None:
        return {"item_id": item_id, **spec}
    chosen, typed = post.get("item_id", ""), post.get("new_item", "").strip()
    if bool(chosen) == bool(typed):
        raise PageError("choose an existing item or type a new item's name, not both")
    if chosen:
        if any(c["item_id"] == chosen for c in contents):
            raise PageError("that item is already in this kit; edit it instead")
        return {"item_id": chosen, **spec}
    return {"new_item": typed, **spec}


@never_cache
@require_http_methods(["GET", "POST"])
def definition_content(request, definition_id, item_id=None):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    found = _definition_of(user, definition_id)
    org = str(user.organization_id)
    latest = _latest(found)
    contents = _contents(latest)
    item_id = str(item_id) if item_id is not None else None
    spec = next((c for c in contents if c["item_id"] == item_id), None)
    if item_id is not None and spec is None:
        raise Http404("that item is not in the latest version")
    form = _posted(request.POST) if request.method == "POST" else _content_form(spec)

    def page(status=200, errors=None):
        query = request.GET.get("q", "").strip()[:MAX_QUERY]
        items, more = (
            _item_choices(org, [c["item_id"] for c in contents], query)
            if item_id is None
            else ([], False)
        )
        context = {
            "title": f"{'Edit' if item_id else 'Add'} a content item: {found.label}",
            "definition": found,
            "item_id": item_id,
            "item_label": Item.objects.filter(pk=item_id).values_list("label", flat=True).first(),
            "latest": latest,
            "base_revision": latest.revision if latest else 0,
            "form": form,
            "kinds": [(k, KIND_LABELS[k]) for k in KINDS if k in KIT_KINDS],
            "optional_outcomes": OPTIONAL_OUTCOMES,
            "items": items,
            "more_items": more,
            "query": query,
            "item_choices": ITEM_CHOICES,
            "operation_id": _op(),
            "errors": errors or [],
            "field_errors": field_errors(errors or [], CONTENT_RULES),
        }
        return render(request, "kits/admin_definition_content.html", context, status=status)

    if request.method == "GET":
        return page()
    try:
        posted = _content_from(request.POST, item_id, contents)
    except PageError as error:
        return page(400, [str(error)])
    if item_id is None:
        new_contents = [*contents, posted]
    else:
        new_contents = [posted if c["item_id"] == item_id else c for c in contents]
    return _save(request, user, found, new_contents, page, "content")


@never_cache
@require_http_methods(["GET", "POST"])
def duplicate(request, definition_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    found = _definition_of(user, definition_id)
    versions, older = _versions(found)
    form = {
        "version_id": request.POST.get("version_id", ""),
        "revision": request.POST.get("revision", "").strip(),
        "label": request.POST.get("label", f"Copy of {found.label}"),
    }

    def page(status=200, errors=None):
        context = {
            "title": f"Duplicate a kit definition: {found.label}",
            "definition": found,
            "versions": versions,
            "older": older,
            "form": form,
            "operation_id": _op(),
            "errors": errors or [],
            "field_errors": field_errors(errors or [], KIT_FIELDS),
        }
        return render(request, "kits/admin_definition_duplicate.html", context, status=status)

    if request.method == "GET":
        return page()
    chosen = KitDefinitionVersion.objects.filter(definition=found)
    if form["revision"]:
        number = _revision_param(form["revision"])
        chosen = chosen.filter(revision=number) if number is not None else chosen.none()
    else:
        chosen = chosen.filter(pk=form["version_id"]) if form["version_id"] else chosen.none()
    try:
        source = chosen.first()
    except (ValueError, ValidationError):
        source = None
    if source is None:
        return page(400, ["choose a version of this kit definition to copy"])
    result, refused = _run(
        request,
        user,
        lambda ids: duplicate_kit_definition(
            source_version_id=str(source.pk),
            key=_generated_key(form["label"], request.POST.get("operation_id", "")),
            label=form["label"],
            **ids,
        ),
        lambda status, errors: page(status, errors),
    )
    if refused:
        return refused
    return _saved("kit-definition", [result.body["definition_id"]], "duplicate")
