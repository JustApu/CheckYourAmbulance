"""Vehicle files with the vehicle's own checklists, after the cutover (SPEC 5.0.0 IMP-6, IMP-7).

cya.kits.vehicle_import plans and applies a vehicle file. Once the organization's cutover is
recorded it calls the functions here for the checklist part (positions use the vehicle's own
keys there, and the kit list is unchanged):

- plan_checklists compares each checklist in the file, matched to the vehicle's checklist with
  the same label, with that checklist's current version (in force, else its latest). A
  different one, or a new one, becomes one draft version; publication stays a separate step
  (IMP-3). Each listed schedule slot the checklist is not yet assigned to is assigned, effective
  when the import is recorded. Nothing the file omits is removed, unassigned or deactivated.
  A new checklist gets the vehicle's next precedence ordinals in file order (CHK-10).
- legacy_to_v2 is the versioned compatibility path for a file from before 5.0.0 (schema 1).
  Such a file holds the vehicle's effective checklist for one template (template items with its
  override applied) and one schedule. Its template key is resolved in the importing
  organization; a key that does not resolve makes the file ambiguous and it is refused. The
  result is one independent vehicle checklist, labelled like the template and inspecting the
  kit definitions the template's version in force inspects, assigned to the file's schedule. No
  binding to the legacy template is created.
- vehicle_file_v2 exports a vehicle in schema 2: its positions with their keys, each checklist
  in precedence order with its current version and the slots it is assigned to now, and its
  kit list. Physical kits and history are never exported.
"""

from django.db.models import Max

from cya.accounts.fields import new_id
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
    Item,
)
from cya.checklists.validation import MAX_ITEMS
from cya.kits.library_import import ImportRefused
from cya.kits.models import ChecklistKitPlacement, KitDefinition
from cya.obligations.models import RequirementBinding
from cya.schedules.models import ScheduleRule

MAX_CHECKLISTS = 100
MAX_LABEL = 200


# --- the legacy path ------------------------------------------------------------------------


def _default_schedule(org, schedule, errors):
    known = sorted(
        set(ScheduleRule.objects.filter(organization_id=org).values_list("slot", flat=True))
    )
    if schedule is None:
        if len(known) == 1:
            return [known[0]]
        errors.append("schedule: name the schedule to use (the organization has several)")
        return []
    return [schedule]


def legacy_to_v2(org, vehicle, errors):
    """A schema 1 vehicle file as one independent vehicle checklist (IMP-7 legacy files)."""
    from django.utils import timezone

    from cya.checklists.overlay import effective_template_version

    key = vehicle.get("template")
    template = (
        ChecklistTemplate.objects.filter(organization_id=org, key=key, vehicle__isnull=True).first()
        if isinstance(key, str)
        else None
    )
    if template is None:
        errors.append(
            "template: this file is from before vehicle checklists and names a template this "
            "organization does not have, so it is ambiguous and refused"
        )
        return {**vehicle, "schema_version": 2, "checklists": []}
    version = effective_template_version(org, template.pk, timezone.now())
    kits = (
        [
            p.definition.key
            for p in ChecklistKitPlacement.objects.filter(checklist_version=version)
            .select_related("definition")
            .order_by("display_order", "id")
        ]
        if version
        else []
    )
    schedule = vehicle.get("schedule")
    if schedule is not None and not isinstance(schedule, str):
        errors.append("schedule: must be a schedule name")
        schedule = None
    checklist = {
        "id": template.key,
        "label": template.label,
        "schedules": _default_schedule(org, schedule, errors),
        "kits": kits,
        "items": list(vehicle.get("items") or []),
    }
    return {
        "schema_version": 2,
        **{k: vehicle.get(k) for k in ("vin", "display_name", "number")},
        "compartments": vehicle.get("compartments") or [],
        "systems": vehicle.get("systems") or [],
        "kits": vehicle.get("kits") or [],
        "checklists": [checklist],
    }


# --- planning --------------------------------------------------------------------------------


def current_version(checklist, now):
    """The version an import compares with and an export writes: in force, else the latest."""
    publication = (
        ChecklistPublication.objects.filter(
            checklist_version__template=checklist, effective_at__lte=now
        )
        .order_by("-effective_at")
        .first()
    )
    if publication is not None:
        return publication.checklist_version
    return ChecklistVersion.objects.filter(template=checklist).order_by("-revision").first()


def _signature(fields, kind, slot):
    return (
        fields["kind"],
        fields["expected_quantity"],
        fields["unit"],
        tuple(fields["allowed_outcomes"]),
        fields["expiration_tracked"],
        fields["instructions"],
        kind,
        str(slot) if slot else None,
    )


def _definition_signature(definition, slot_of):
    if definition.compartment_id:
        kind, row = "COMPARTMENT", definition.compartment_id
    elif definition.system_id:
        kind, row = "SYSTEM", definition.system_id
    else:
        kind, row = None, None
    return _signature(
        {
            "kind": definition.kind,
            "expected_quantity": definition.expected_quantity,
            "unit": definition.unit,
            "allowed_outcomes": list(definition.allowed_outcomes),
            "expiration_tracked": definition.expiration_tracked,
            "instructions": definition.instructions,
        },
        kind,
        slot_of.get(str(row)) if row else None,
    )


def _bound_slots(org, vehicle, checklist):
    if vehicle is None or checklist is None:
        return set()
    latest = {}
    for binding in RequirementBinding.objects.filter(
        organization_id=org, vehicle=vehicle, template=checklist
    ).order_by("slot", "-revision"):
        latest.setdefault(binding.slot, binding)
    return {slot for slot, b in latest.items() if b.action == "BIND"}


class _Context:
    """What plan_checklists needs from the vehicle plan."""

    def __init__(self, org, found, now, position, slots, slot_of, by_label, errors):
        self.org, self.found, self.now = org, found, now
        self.position, self.slots, self.slot_of = position, slots, slot_of
        self.by_label, self.errors = by_label, errors


def _file_items(ctx, entries, where, check_item, new_items):
    """[(item label, item id or None, place kind or None, key or None, fields)] for one list."""
    found, seen = [], set()
    for index, entry in enumerate(entries):
        at = f"{where}.items[{index}]"
        label = entry.get("item")
        if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL:
            ctx.errors.append(f"{at}.item: must be nonblank text of at most {MAX_LABEL} characters")
            continue
        label = label.strip()
        fields = check_item(entry, at, ctx.errors)
        compartment, system = entry.get("compartment"), entry.get("system")
        if compartment is not None and system is not None:
            ctx.errors.append(f"{at}: name at most one of compartment or system")
            continue
        kind = "COMPARTMENT" if compartment else "SYSTEM" if system else None
        key = ctx.position(kind, compartment or system, f"{at}.{kind.lower()}") if kind else None
        if fields is None or (kind and key is None):
            continue
        if label in seen:
            ctx.errors.append(f"{at}.item: {label!r} is listed twice in this checklist")
            continue
        seen.add(label)
        matches = ctx.by_label.get(label, [])
        active = [pk for pk, is_active in matches if is_active]
        if len(active) > 1:
            ctx.errors.append(f"{at}.item: several active items are labelled {label!r}")
            continue
        if matches and not active:
            ctx.errors.append(f"{at}.item: {label!r} names only inactive items")
            continue
        item_id = str(active[0]) if active else None
        if item_id is None and label not in new_items:
            new_items.append(label)
        found.append((label, item_id, kind, key, fields))
    return found


def _kit_definitions(ctx, keys, where):
    definitions = []
    for index, key in enumerate(keys):
        definition = (
            KitDefinition.objects.filter(organization_id=ctx.org, key=key).first()
            if isinstance(key, str)
            else None
        )
        if definition is None or not definition.active:
            ctx.errors.append(f"{where}.kits[{index}]: no active kit definition has that key")
            continue
        if definition in definitions:
            ctx.errors.append(f"{where}.kits[{index}]: {key!r} is listed twice")
            continue
        definitions.append(definition)
    return definitions


def _schedules(ctx, slots, where):
    known = set(ScheduleRule.objects.filter(organization_id=ctx.org).values_list("slot", flat=True))
    found = []
    for index, slot in enumerate(slots):
        if not isinstance(slot, str) or slot not in known:
            ctx.errors.append(f"{where}.schedules[{index}]: no schedule has that name")
        elif slot not in found:
            found.append(slot)
    return found


def _changed(ctx, existing, items, definitions):
    """Whether the file's checklist differs from the existing checklist's current version."""
    version = current_version(existing, ctx.now) if existing else None
    if version is None:
        return True
    current = {
        str(d.item_id): _definition_signature(d, ctx.slot_of)
        for d in ChecklistItemDef.objects.filter(checklist_version=version, kit__isnull=True)
    }
    wanted = {}
    for _label, item_id, kind, key, fields in items:
        if item_id is None:
            return True
        wanted[item_id] = _signature(fields, kind, ctx.slots[kind].get(key) if kind else None)
    placed = set(
        ChecklistKitPlacement.objects.filter(checklist_version=version).values_list(
            "definition_id", flat=True
        )
    )
    return current != wanted or placed != {d.pk for d in definitions}


def plan_checklists(ctx, checklists, check_item):
    """(checklist plans, new item labels) for the file's checklists (IMP-7, after the cutover)."""
    if len(checklists) > MAX_CHECKLISTS:
        ctx.errors.append(f"checklists: more than {MAX_CHECKLISTS} checklists")
        return [], []
    existing = {}
    if ctx.found is not None:
        for checklist in ChecklistTemplate.objects.filter(vehicle=ctx.found):
            existing.setdefault(checklist.label, []).append(checklist)
    plans, new_items, ids, labels = [], [], set(), set()
    for index, entry in enumerate(checklists):
        where = f"checklists[{index}]"
        local, label = entry.get("id"), entry.get("label")
        if not isinstance(local, str) or not local.strip() or local in ids:
            ctx.errors.append(f"{where}.id: each checklist needs its own id in the file")
            continue
        ids.add(local)
        if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL:
            ctx.errors.append(f"{where}.label: must be nonblank text of at most {MAX_LABEL}")
            continue
        label = label.strip()
        if label in labels:
            ctx.errors.append(f"{where}.label: two checklists in the file are named {label!r}")
            continue
        labels.add(label)
        same = existing.get(label, [])
        if len(same) > 1:
            ctx.errors.append(f"{where}.label: the vehicle has several checklists named {label!r}")
            continue
        current = same[0] if same else None
        if len(entry.get("items") or []) > MAX_ITEMS:
            # The bound a checklist draft has (cya.checklists.validation.MAX_ITEMS).
            ctx.errors.append(f"{where}.items: more than {MAX_ITEMS} items")
            continue
        items = _file_items(ctx, entry.get("items") or [], where, check_item, new_items)
        definitions = _kit_definitions(ctx, entry.get("kits") or [], where)
        slots = _schedules(ctx, entry.get("schedules") or [], where)
        if not items and current is None:
            ctx.errors.append(f"{where}.items: a new checklist needs at least one item")
        plans.append(
            {
                "local": local,
                "label": label,
                "existing": current,
                "items": items,
                "definitions": definitions,
                "changed": _changed(ctx, current, items, definitions),
                "assign": sorted(set(slots) - _bound_slots(ctx.org, ctx.found, current)),
            }
        )
    return plans, new_items


# --- applying --------------------------------------------------------------------------------


def _carried_kit_items(version):
    """Item rows of the current version that a file cannot express (a kit reference): kept."""
    if version is None:
        return []
    return list(ChecklistItemDef.objects.filter(checklist_version=version, kit__isnull=False))


def _write_draft(org, checklist, entry, rows, created, at):
    from cya.checks.locks import ensure_item_locks

    current = current_version(checklist, at)
    top = ChecklistVersion.objects.filter(template=checklist).aggregate(m=Max("revision"))
    draft = ChecklistVersion.objects.create(
        id=new_id(),
        organization_id=org,
        template=checklist,
        revision=(top["m"] or 0) + 1,
        created_at=at,
    )
    orders = {}
    if current is not None:
        orders = dict(
            ChecklistItemDef.objects.filter(checklist_version=current).values_list(
                "item_id", "display_order"
            )
        )
    item_ids = []
    for index, (label, item_id, kind, key, fields) in enumerate(entry["items"]):
        item_id = item_id or str(created[label].pk)
        row = rows[(kind, key)] if kind else None
        ChecklistItemDef.objects.create(
            id=new_id(),
            organization_id=org,
            checklist_version=draft,
            item_id=item_id,
            display_order=orders.get(item_id, (index + 1) * 10),
            compartment_id=row.pk if kind == "COMPARTMENT" else None,
            system_id=row.pk if kind == "SYSTEM" else None,
            kit_id=None,
            bulk_eligible=False,
            **fields,
        )
        item_ids.append(item_id)
    listed = set(item_ids)
    for kept in _carried_kit_items(current):
        if str(kept.item_id) in listed:
            continue
        ChecklistItemDef.objects.create(
            id=new_id(),
            organization_id=org,
            checklist_version=draft,
            **{
                name: getattr(kept, name)
                for name in (
                    "item_id",
                    "display_order",
                    "kind",
                    "instructions",
                    "expected_quantity",
                    "unit",
                    "allowed_outcomes",
                    "expiration_tracked",
                    "compartment_id",
                    "system_id",
                    "kit_id",
                    "bulk_eligible",
                )
            },
        )
    ChecklistKitPlacement.objects.bulk_create(
        ChecklistKitPlacement(
            id=new_id(),
            organization_id=org,
            checklist_version=draft,
            definition=definition,
            display_order=(index + 1) * 10,
        )
        for index, definition in enumerate(entry["definitions"])
    )
    ensure_item_locks(org, [str(checklist.vehicle_id)], item_ids)


def apply_checklists(org, vehicle, plans, rows, created, evidence):
    """Write the checklist part of a vehicle import (IMP-3, IMP-7, after the cutover)."""
    at = evidence["recorded_at"]
    top = ChecklistTemplate.objects.filter(vehicle=vehicle).aggregate(m=Max("precedence"))
    precedence = top["m"] or 0
    for entry in plans:
        checklist = entry["existing"]
        if checklist is None:
            precedence += 1
            checklist = ChecklistTemplate.objects.create(
                id=new_id(),
                organization_id=org,
                label=entry["label"],
                active=True,
                vehicle=vehicle,
                precedence=precedence,
            )
        if entry["changed"]:
            _write_draft(org, checklist, entry, rows, created, at)
        for slot in entry["assign"]:
            prior = (
                RequirementBinding.objects.filter(vehicle=vehicle, template=checklist, slot=slot)
                .order_by("-revision")
                .first()
            )
            RequirementBinding.objects.create(
                organization_id=org,
                vehicle=vehicle,
                template=checklist,
                slot=slot,
                revision=prior.revision + 1 if prior else 1,
                action="BIND",
                effective_at=at,
                **evidence,
            )


# --- export ----------------------------------------------------------------------------------


def _item_entry(definition, labels, key_of_row):
    row = definition.compartment_id or definition.system_id
    return {
        "item": labels.get(str(definition.item_id), ""),
        "compartment": key_of_row.get(str(row)) if definition.compartment_id else None,
        "system": key_of_row.get(str(row)) if definition.system_id else None,
        "kind": definition.kind,
        "expected_quantity": definition.expected_quantity,
        "unit": definition.unit,
        "allowed_outcomes": list(definition.allowed_outcomes),
        "expiration_tracked": definition.expiration_tracked,
        "instructions": definition.instructions,
    }


def _requirement_versions(org, vehicle, now):
    """{requirement id: version} to export (in force now, else first scheduled), or {} before
    the organization's item-requirement cutover."""
    from cya.checklists import item_requirements as rules

    if not rules.has_cutover(org):
        return {}
    return rules.current_or_next(str(vehicle.pk), now)


def export_requirements(org, vehicle, now, key_of_row):
    """Every item requirement of the vehicle as an item dict (SPEC 6.0.0 IMP-7, CHK-11),
    including one no checklist inspects any more."""
    labels = dict(Item.objects.filter(organization_id=org).values_list("pk", "label"))
    labels = {str(pk): label for pk, label in labels.items()}
    versions = sorted(
        _requirement_versions(org, vehicle, now).values(),
        key=lambda v: (labels.get(str(v.item_id), ""), v.place_kind, str(v.place_slot_id)),
    )
    return [_item_entry(v, labels, key_of_row) for v in versions]


def export_checklists(org, vehicle, now, key_of_row):
    """The vehicle's checklists in schema 2 normal form, in precedence order (CHK-10).

    After the item-requirement cutover a mapped row is written with its requirement's place and
    values, in force now (else first scheduled): a row keeps the values it recorded, while
    reads follow the requirement (CHK-5), so the file never carries a stale copy.
    """
    from cya.checklists.models import ItemRequirementMap

    labels = {
        str(pk): label
        for pk, label in Item.objects.filter(organization_id=org).values_list("pk", "label")
    }
    versions = _requirement_versions(org, vehicle, now)
    found = []
    for checklist in ChecklistTemplate.objects.filter(vehicle=vehicle).order_by("precedence"):
        version = current_version(checklist, now)
        definitions = (
            ChecklistItemDef.objects.filter(checklist_version=version, kit__isnull=True).order_by(
                "display_order", "id"
            )
            if version
            else []
        )
        kits = (
            [
                p.definition.key
                for p in ChecklistKitPlacement.objects.filter(checklist_version=version)
                .select_related("definition")
                .order_by("display_order", "id")
            ]
            if version
            else []
        )
        mapped = (
            {
                str(row): versions.get(str(requirement))
                for row, requirement in ItemRequirementMap.objects.filter(
                    checklist_version=version, requirement__isnull=False
                ).values_list("item_def_id", "requirement_id")
            }
            if version and versions
            else {}
        )
        found.append(
            {
                "id": f"checklist-{checklist.precedence}",
                "label": checklist.label,
                "schedules": sorted(_bound_slots(org, vehicle, checklist)),
                "kits": kits,
                "items": [
                    _item_entry(mapped.get(str(d.pk)) or d, labels, key_of_row) for d in definitions
                ],
            }
        )
    return found


def requirement_notes(ctx, plans, vehicle):
    """What publishing the import's drafts would do to the vehicle's item requirements.

    SPEC 6.0.0 CHK-11: a vehicle has one item requirement per item and place, so two of the
    file's checklists that give one item and place different values are an error (AC-42 (1); a
    schema 3 file cannot express that). For an existing vehicle each item and place is compared
    with its requirement in force: new, changed (and refused at publication while a checklist
    not in the file inspects it), or unchanged (no note). A file requirement no checklist
    inspects is not imported: requirements arise only when a checklist is published.
    """
    from cya.checklists import item_requirements as rules

    notes, seen = [], {}
    in_file = {p["label"] for p in plans}
    for plan in plans:
        for label, item_id, kind, key, fields in plan["items"]:
            where = f"{kind.lower()} {key}" if kind else "Not placed"
            if (label, kind, key) in seen:
                first, first_fields = seen[(label, kind, key)]
                if first_fields != fields:
                    ctx.errors.append(
                        f"checklists {first!r} and {plan['label']!r} give {label!r} ({where}) "
                        "different requirements; a vehicle has one item requirement per item "
                        "and place (SPEC 6.0.0 CHK-11)"
                    )
                continue
            seen[(label, kind, key)] = (plan["label"], fields)
            if ctx.found is None or item_id is None or not rules.has_cutover(ctx.org):
                continue
            slot = ctx.slots[kind].get(key) if kind else None
            version = (
                None
                if kind and slot is None
                else rules.in_force_here(
                    str(ctx.found.pk),
                    item_id,
                    (kind, str(slot)) if kind else ("NOT_PLACED", None),
                    ctx.now,
                )
            )
            if version is None:
                notes.append(f"{label} ({where}): a new item requirement")
            elif any(
                _value(getattr(version, name)) != _value(fields[name]) for name in rules.FIELDS
            ):
                others = [
                    other
                    for other in rules.other_users(version.requirement_id, None, ctx.now)
                    if other not in in_file
                ]
                if others:
                    notes.append(
                        f"{label} ({where}): changes its item requirement, which "
                        f"{', '.join(others)} also inspect; publishing the draft is refused "
                        "until the requirement is changed on the vehicle's item requirements page"
                    )
                else:
                    notes.append(f"{label} ({where}): publishing the draft changes its requirement")
    used = {
        entry.get("requirement")
        for checklist in vehicle.get("checklists") or []
        for entry in checklist.get("items") or []
    }
    for requirement in vehicle.get("requirements") or []:
        if requirement.get("id") not in used:
            notes.append(
                f"{requirement.get('item')} (requirement {requirement.get('id')}): no checklist "
                "in the file inspects it, so it is not imported"
            )
    return notes


def _value(value):
    return list(value) if isinstance(value, (list, tuple)) else value


__all__ = [
    "ImportRefused",
    "apply_checklists",
    "export_checklists",
    "export_requirements",
    "legacy_to_v2",
    "plan_checklists",
    "requirement_notes",
]
