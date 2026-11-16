"""Vehicle override screens (SPEC 4.3 CHK-1, CHK-6; issue #130 part 6b).

A vehicle override is an overlay on one published template version: it adds, removes or
replaces items for that vehicle (save_override_draft, publish_override_version). Supervisors and
Administrators (draft_edit_template) edit a vehicle's override of a template one entry at a
time; each change saves a whole new draft revision, built from the vehicle's latest override of
that template, and carries the vehicle's latest override revision so a concurrent change is
refused rather than lost.

An override is drafted over the template's latest published version. A draft or override over an
older version is stale: resolution fails closed once a newer version governs
(StaleOverrideError), so the page offers to carry its entries over to the latest version, listing
what cannot be carried, before confirming.

Owner decision (2026-11-15): drafts are allowed for any vehicle, but publishing (publish_template)
is offered only for a vehicle bound to the template now or by a binding recorded for later, so
every published override is one the template's publication preview (part 6a) watches.
"""

import logging

from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.checklists.commands import publish_override_version, save_override_draft
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    Compartment,
    Item,
    KitInstance,
    VehicleOverrideItem,
    VehicleOverrideVersion,
    VehicleSystem,
)
from cya.checklists.overlay import OverrideEntry, apply_overlay, requirement_from_definition
from cya.kits import admin_pages
from cya.kits import positions as position_rules
from cya.kits.views import _can
from cya.operations.duplication import DEFINITION_FIELDS
from cya.operations.template_admin_views import (
    ACTION,
    DENIAL,
    KIND_LABELS,
    SCHEDULED_LIMIT,
    PageError,
    _base_revision,
    _posted_form,
    _run,
    _saved,
    _spec_from,
    _template_of,
    item_form_context,
)
from cya.shell import navigation

logger = logging.getLogger(__name__)

SAVED = {
    "entry": "Override draft saved. Publish it to put it in force for this vehicle.",
    "undo": "Change removed from the override draft.",
    "carry": "A new override draft over the latest template version was saved.",
    "publish": "Override published.",
}
ACTION_LABELS = {"ADD": "Added", "REMOVE": "Removed", "REPLACE": "Changed"}


# --- reading ---------------------------------------------------------------------------------


def _target_version(template):
    """The template's latest published version (by effective instant), which overrides overlay."""
    publication = (
        ChecklistPublication.objects.filter(checklist_version__template=template)
        .order_by("-effective_at", "-checklist_version__revision")
        .select_related("checklist_version")
        .first()
    )
    return publication.checklist_version if publication else None


def _latest_override(vehicle, template):
    return (
        VehicleOverrideVersion.objects.filter(vehicle=vehicle, template_version__template=template)
        .order_by("-revision")
        .select_related("template_version")
        .first()
    )


def _vehicle_revision(vehicle):
    """The vehicle's latest override revision across all templates, 0 when none."""
    latest = VehicleOverrideVersion.objects.filter(vehicle=vehicle).order_by("-revision").first()
    return latest.revision if latest else 0


def _is_published(override):
    return ChecklistPublication.objects.filter(override_version=override).exists()


def _spec(definition):
    spec = {name: getattr(definition, name) for name in DEFINITION_FIELDS}
    for name in ("item_id", "compartment_id", "system_id", "kit_id"):
        spec[name] = str(spec[name]) if spec[name] is not None else None
    spec["allowed_outcomes"] = list(spec["allowed_outcomes"])
    return spec


def _entries(override):
    """An override's entries as save_override_draft takes them, in a stable order."""
    if override is None:
        return []
    definitions = {d.pk: d for d in ChecklistItemDef.objects.filter(override_version=override)}
    entries = []
    for row in VehicleOverrideItem.objects.filter(override_version=override).order_by("item_id"):
        definition = definitions.get(row.definition_id)
        entries.append(
            {
                "item_id": str(row.item_id),
                "action": row.action,
                "definition": _spec(definition) if definition else None,
            }
        )
    return entries


def _template_specs(version):
    return [
        _spec(d)
        for d in ChecklistItemDef.objects.filter(checklist_version=version).order_by(
            "display_order", "id"
        )
    ]


def carry_over(entries, target_item_ids):
    """(entries that still apply to the target version, [(entry, why it is dropped)]).

    REMOVE and REPLACE need the item in the target version; ADD needs it absent.
    """
    kept, dropped = [], []
    for entry in entries:
        present = entry["item_id"] in target_item_ids
        if entry["action"] == "ADD" and present:
            dropped.append((entry, "the template now has this item; change it instead"))
        elif entry["action"] != "ADD" and not present:
            dropped.append((entry, "the template no longer has this item"))
        else:
            kept.append(entry)
    return kept, dropped


def _labels(model, ids):
    ids = {str(i) for i in ids if i}
    return {
        str(pk): label for pk, label in model.objects.filter(pk__in=ids).values_list("pk", "label")
    }


def _display(version, entries):
    """Rows for the vehicle's resulting checklist and the removed items, for display."""
    requirements = [
        requirement_from_definition(d, "TEMPLATE")
        for d in ChecklistItemDef.objects.filter(checklist_version=version)
    ]
    overlay = [
        OverrideEntry(
            entry["item_id"],
            entry["action"],
            requirement_from_definition(ChecklistItemDef(**entry["definition"]), "OVERRIDE")
            if entry["definition"] is not None
            else None,
        )
        for entry in entries
    ]
    resolved = apply_overlay(requirements, overlay)
    actions = {entry["item_id"]: entry["action"] for entry in entries}
    removed_ids = [e["item_id"] for e in entries if e["action"] == "REMOVE"]
    items = _labels(Item, [r.item_id for r in resolved] + removed_ids)
    compartments = _labels(Compartment, [r.compartment_id for r in resolved])
    systems = _labels(VehicleSystem, [r.system_id for r in resolved])
    kits = _labels(KitInstance, [r.kit_id for r in resolved])

    def where(r):
        if r.compartment_id:
            text = compartments.get(str(r.compartment_id), "")
            return f"{text}, kit {kits.get(str(r.kit_id), '')}" if r.kit_id else text
        return systems.get(str(r.system_id), "")

    rows = [
        {
            "item_id": str(r.item_id),
            "label": items.get(str(r.item_id), ""),
            "where": where(r),
            "kind": KIND_LABELS.get(r.kind, r.kind),
            "target": str(r.expected_quantity) if r.kind == "QUANTITY" else (r.unit or ""),
            "display_order": r.display_order,
            "source": ACTION_LABELS.get(actions.get(str(r.item_id)), "Template"),
        }
        for r in resolved
    ]
    removed = [{"item_id": i, "label": items.get(i, "")} for i in removed_ids]
    return rows, removed


def _state(vehicle, template):
    """What the page shows: the target version, the base entries and the override status."""
    now = timezone.now()
    target = _target_version(template)
    latest = _latest_override(vehicle, template)
    publications = ChecklistPublication.objects.filter(
        override_version__vehicle=vehicle, override_version__template_version__template=template
    ).select_related("override_version")
    in_force = (
        publications.filter(effective_at__lte=now)
        .order_by("-effective_at", "-override_version__revision")
        .first()
    )
    scheduled = list(
        publications.filter(effective_at__gt=now).order_by("effective_at")[:SCHEDULED_LIMIT]
    )
    draft = latest if latest is not None and not _is_published(latest) else None
    stale = latest is not None and target is not None and latest.template_version_id != target.pk
    # Edits start from the latest override of this template when it overlays the target.
    base = latest if latest is not None and not stale else None
    return {
        "target": target,
        "latest": latest,
        "draft": draft,
        "stale": stale,
        "entries": _entries(base),
        "in_force": in_force,
        "scheduled": scheduled,
    }


def _bound(org, vehicle, template):
    bound = position_rules.vehicles_bound_now_or_later(
        org, str(template.pk), vehicle_ids=[vehicle.pk]
    )
    return str(vehicle.pk) in {str(v) for v in bound}


# --- the override page -----------------------------------------------------------------------


def _op():
    return admin_pages.new_operation_id()


def _url(template, vehicle):
    return reverse("checklist-override", args=[template.pk, vehicle.pk])


def _page(request, user, template, vehicle, *, status=200, errors=None, carry=None, publish=None):
    org = str(user.organization_id)
    roles = navigation.roles_for(user)
    state = _state(vehicle, template)
    rows, removed = [], []
    if state["target"] is not None:
        rows, removed = _display(state["target"], state["entries"])
    entry_ids = {e["item_id"] for e in state["entries"]}
    context = {
        "title": f"Override: {template.label}, {vehicle.display_name}",
        "template": template,
        "vehicle": vehicle,
        **state,
        "rows": [
            {**row, "has_entry": row["item_id"] in entry_ids, "operation_id": _op()} for row in rows
        ],
        "removed": [{**row, "operation_id": _op()} for row in removed],
        "base_revision": _vehicle_revision(vehicle),
        "bound": _bound(org, vehicle, template),
        "can_publish": _can(user, roles, "publish_template"),
        "carry": carry,
        "publish": publish,
        "carry_operation_id": _op(),
        "publish_operation_id": _op(),
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
        "zone": timezone.get_current_timezone_name(),
    }
    return render(request, "operations/admin_override.html", context, status=status)


def _allowed(request, template_id, vehicle_id):
    allowed, refused = admin_pages.allowed(request, ACTION, DENIAL)
    if refused:
        return None, refused
    user, _roles = allowed
    found = (user, _template_of(user, template_id), admin_pages.vehicle_of(user, vehicle_id))
    return found, None


def _save(request, user, template, vehicle, target, entries, error_page, stage):
    try:
        base_revision = _base_revision(request.POST)
    except PageError as error:
        return error_page(400, [str(error)])
    if not entries:
        return error_page(400, ["an override needs at least one change"])
    _result, refused = _run(
        request,
        user,
        lambda ids: save_override_draft(
            vehicle_id=str(vehicle.pk),
            template_version_id=str(target.pk),
            entries=entries,
            base_revision=base_revision,
            **ids,
        ),
        error_page,
    )
    return refused or _saved("checklist-override", [template.pk, vehicle.pk], stage)


@never_cache
@require_http_methods(["GET", "POST"])
def override(request, template_id, vehicle_id):
    found, refused = _allowed(request, template_id, vehicle_id)
    if refused:
        return refused
    user, template, vehicle = found
    if request.method == "GET":
        return _page(request, user, template, vehicle)

    def error_page(status, errors):
        return _page(request, user, template, vehicle, status=status, errors=errors)

    state = _state(vehicle, template)
    target = state["target"]
    if target is None:
        return error_page(400, ["publish a version of this template before overriding it"])
    stage = request.POST.get("stage")
    if stage in ("remove-item", "undo"):
        if state["stale"]:
            return error_page(400, ["carry the override over to the latest version first"])
        item_id = request.POST.get("item_id", "")
        entries = [e for e in state["entries"] if e["item_id"] != item_id]
        if stage == "remove-item":
            in_template = any(s["item_id"] == item_id for s in _template_specs(target))
            if len(entries) != len(state["entries"]) or not in_template:
                return error_page(400, ["that item cannot be removed here; reload the page"])
            entries.append({"item_id": item_id, "action": "REMOVE", "definition": None})
            saved = "entry"
        else:
            if len(entries) == len(state["entries"]):
                return error_page(400, ["that change is not in the override; reload the page"])
            saved = "undo"
        return _save(request, user, template, vehicle, target, entries, error_page, saved)
    if stage in ("carry-preview", "carry"):
        return _carry(request, user, template, vehicle, state, stage, error_page)
    if stage in ("publish-preview", "publish"):
        return _publish(request, user, template, vehicle, state, stage, error_page)
    return error_page(400, ["the request was not valid"])


def _carry(request, user, template, vehicle, state, stage, error_page):
    latest = state["latest"]
    if not state["stale"] or latest is None:
        return error_page(400, ["the override already overlays the latest version"])
    target_ids = {s["item_id"] for s in _template_specs(state["target"])}
    kept, dropped = carry_over(_entries(latest), target_ids)
    if stage == "carry-preview":
        labels = _labels(Item, [e["item_id"] for e, _why in dropped])
        carry = {
            "kept": len(kept),
            "dropped": [
                {"label": labels.get(e["item_id"], ""), "action": e["action"], "why": why}
                for e, why in dropped
            ],
        }
        return _page(request, user, template, vehicle, carry=carry)
    return _save(request, user, template, vehicle, state["target"], kept, error_page, "carry")


def _publish(request, user, template, vehicle, state, stage, error_page):
    draft = state["draft"]
    if draft is None or state["stale"]:
        return error_page(400, ["there is no current override draft to publish"])
    if request.POST.get("override_version_id") != str(draft.pk):
        return error_page(400, ["the override draft changed; reload the page"])
    if not _bound(str(user.organization_id), vehicle, template):
        return error_page(
            400,
            [
                "this vehicle is not bound to this template, now or later; an override is "
                "published only for a bound vehicle"
            ],
        )
    try:
        effective_at = admin_pages.local_effective_at(request.POST, PageError)
    except PageError as error:
        return error_page(400, [str(error)])
    if stage == "publish-preview":
        publication = ChecklistPublication.objects.filter(checklist_version=state["target"]).first()
        publish = {
            "when": request.POST.get("when", ""),
            "at": request.POST.get("at", ""),
            "effective_at": effective_at,
            "template_effective_at": publication.effective_at if publication else None,
        }
        return _page(request, user, template, vehicle, publish=publish)
    _result, refused = _run(
        request,
        user,
        lambda ids: publish_override_version(
            override_version_id=str(draft.pk), effective_at=effective_at, **ids
        ),
        error_page,
    )
    return refused or _saved("checklist-override", [template.pk, vehicle.pk], "publish")


# --- one entry -------------------------------------------------------------------------------


@never_cache
@require_http_methods(["GET", "POST"])
def override_item(request, template_id, vehicle_id, item_id=None):
    found, refused = _allowed(request, template_id, vehicle_id)
    if refused:
        return refused
    user, template, vehicle = found
    org = str(user.organization_id)
    state = _state(vehicle, template)
    target = state["target"]
    if target is None or state["stale"]:
        raise Http404("no current override to edit")
    item_id = str(item_id) if item_id is not None else None
    template_specs = {s["item_id"]: s for s in _template_specs(target)}
    entries = {e["item_id"]: e for e in state["entries"]}
    current = None
    if item_id is not None:
        entry = entries.get(item_id)
        current = (entry or {}).get("definition") or template_specs.get(item_id)
        if current is None:
            raise Http404("that item is not in this vehicle's checklist")
    form = _posted_form(request.POST) if request.method == "POST" else None

    def page(status=200, errors=None):
        context = {
            "title": f"{'Change' if item_id else 'Add'} an item for {vehicle.display_name}",
            "template": template,
            "vehicle": vehicle,
            "back_url": _url(template, vehicle),
            "scope_note": (
                f"This changes the checklist for {vehicle.display_name} only, as a new override "
                f"draft over revision {target.revision} of the template."
            ),
            "base_revision": _vehicle_revision(vehicle),
            **item_form_context(
                request,
                org,
                spec=current,
                item_id=item_id,
                exclude=list(template_specs) + list(entries),
                form=form,
                errors=errors,
            ),
        }
        return render(request, "operations/admin_template_item.html", context, status=status)

    if request.method == "GET":
        return page()
    try:
        posted = _spec_from(request.POST, item_id, list(template_specs.values()))
    except PageError as error:
        return page(400, [str(error)])
    others = [e for e in state["entries"] if e["item_id"] != item_id]
    if item_id is not None:
        action = "REPLACE" if item_id in template_specs else "ADD"
        new = {"item_id": item_id, "action": action, "definition": posted}
    elif "new_item" in posted:
        definition = {k: v for k, v in posted.items() if k != "new_item"}
        new = {"new_item": posted["new_item"], "action": "ADD", "definition": definition}
    elif posted["item_id"] in entries:
        return page(400, ["that item already has a change here; edit it instead"])
    else:
        new = {"item_id": posted["item_id"], "action": "ADD", "definition": posted}
    return _save(request, user, template, vehicle, target, [*others, new], page, "entry")
