"""Checklist template administration (SPEC 4.3, 4.0.0 IMP-6; issue #130 part 6a).

Supervisors and Administrators (draft_edit_template, SPEC 3.3) create a checklist template with
its label and the key its creator enters (IMP-6). Its first version is a draft saved through
save_template_draft, as every later one is.

publication_impact only reads: it shows, before a draft is published, what the publication
would change and which vehicles it would reach, with whatever would make
publish_template_version refuse. The command re-checks all of it under its locks, so the
preview is information, not a promise.
"""

from dataclasses import dataclass, field

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
    Compartment,
    Item,
    KitInstance,
    Vehicle,
    VehicleSystem,
)
from cya.checklists.overlay import effective_template_version, vehicle_slots
from cya.checklists.ownership import cutover_at, require_legacy_writable
from cya.kits import keys as key_rules
from cya.kits import positions as position_rules
from cya.kits.models import KitDefinition, KitDefinitionPublication
from cya.kits.placements import placements_of
from cya.operations.vehicles import MAX_NAME, VehicleError, _plan, _run

ACTION = "draft_edit_template"
VEHICLE_LIMIT = 500  # vehicles the impact preview checks (SPEC NFR-6)
LATER_OVERRIDES = 5  # scheduled overrides listed per vehicle (SPEC NFR-6)
CREATE_COMMAND = "TEMPLATE_CREATE"
CREATE_VEHICLE_CHECKLIST = "VEHICLE_CHECKLIST_CREATE"
# The definition fields a reader compares between two versions of an item.
COMPARED = (
    "display_order",
    "kind",
    "instructions",
    "expected_quantity",
    "unit",
    "allowed_outcomes",
    "expiration_tracked",
)


class TemplateError(VehicleError):
    """A template rule was violated; the command rolls back and is audited as failed."""


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_NAME:
        raise TemplateError(f"name must be nonblank text of at most {MAX_NAME} characters")
    return value.strip()


def create_template(*, label, key, **context):
    """A new, active checklist template with no versions yet (IMP-6: its creator's key)."""
    name = _label(label)
    key = key_rules.check_key(key, "key")
    org = context["organization_id"]

    def build(actor):
        # SPEC 5.0.0: no shared template is created once checklists belong to each vehicle.
        require_legacy_writable(org, TemplateError)
        if ChecklistTemplate.objects.filter(organization_id=org, key=key).exists():
            raise TemplateError("another template has that key")
        template_id = new_id()

        def apply(receipt, _current):
            ChecklistTemplate.objects.create(
                id=template_id, organization_id=org, label=name, active=True, key=key
            )

        body = {"code": CREATE_COMMAND, "template_id": template_id, "key": key, "label": name}
        return _plan(org, body, apply)

    # Template keys are unique across the organization: take its lock first.
    return _run(
        context,
        CREATE_COMMAND,
        {"label": name, "key": key},
        build,
        lock_organization=True,
        action=ACTION,
    )


def create_vehicle_checklist(*, vehicle_id, label, **context):
    """A new, active checklist owned by one vehicle, with no versions yet (SPEC 5.0.0 CHK-1).

    Supervisors and Administrators (draft_edit_template, SPEC 3.3 and CFG-9). It gets the
    vehicle's next stable precedence ordinal, so it sorts after the vehicle's existing
    checklists when two require the same routine anchor (CHK-10). It has no key (IMP-6) and
    belongs to that vehicle for good. The vehicle row is locked first, so two concurrent
    creations get distinct ordinals. Vehicle checklists exist only once the organization's
    cutover is recorded (SPEC 18.2).
    """
    canonical_id(vehicle_id)
    name = _label(label)
    org = context["organization_id"]

    def build(actor):
        vehicle = (
            Vehicle.objects.select_for_update().filter(organization_id=org, pk=vehicle_id).first()
        )
        if vehicle is None:
            raise TemplateError("vehicle not found")
        if cutover_at(org) is None:
            raise TemplateError("vehicle checklists start at the cutover")
        top = (
            ChecklistTemplate.objects.filter(organization_id=org, vehicle=vehicle)
            .order_by("-precedence")
            .values_list("precedence", flat=True)
            .first()
        )
        precedence = (top or 0) + 1
        checklist_id = new_id()

        def apply(receipt, _current):
            ChecklistTemplate.objects.create(
                id=checklist_id,
                organization_id=org,
                label=name,
                active=True,
                vehicle=vehicle,
                precedence=precedence,
            )

        body = {
            "code": CREATE_VEHICLE_CHECKLIST,
            "checklist_id": checklist_id,
            "vehicle_id": str(vehicle.pk),
            "label": name,
            "precedence": precedence,
        }
        return _plan(org, body, apply)

    return _run(
        context,
        CREATE_VEHICLE_CHECKLIST,
        {"vehicle_id": vehicle_id, "label": name},
        build,
        action=ACTION,
    )


# --- the impact preview ----------------------------------------------------------------------


@dataclass
class VehicleImpact:
    vehicle: Vehicle
    blocking: list = field(default_factory=list)  # would make the publication refuse
    notes: list = field(default_factory=list)  # follow-up the publication makes necessary


@dataclass
class PublicationImpact:
    version: ChecklistVersion
    effective_at: object  # the instant judged: the requested one, or now
    in_force: ChecklistVersion | None  # the version that governs then without this one
    added: list = field(default_factory=list)  # item labels
    removed: list = field(default_factory=list)
    changed: list = field(default_factory=list)  # (item label, [what changed])
    vehicles: list = field(default_factory=list)  # VehicleImpact, by name
    blocking: list = field(default_factory=list)  # refusals not tied to one vehicle
    more_vehicles: bool = False  # more than VEHICLE_LIMIT vehicles; only those were checked
    # SPEC 6.0.0 CHK-11 (issue #256 C4): what the publication does to the vehicle's item
    # requirements, one phrase per row that changes one; refusals go to `blocking`.
    requirements: list = field(default_factory=list)

    @property
    def refused(self):
        return bool(self.blocking) or any(v.blocking for v in self.vehicles)


def _place(definition, slot_labels, kit_labels):
    if definition.compartment_id:
        where = slot_labels.get(("COMPARTMENT", str(definition.compartment_id)), "a compartment")
        if definition.kit_id:
            return f"{where}, kit {kit_labels.get(str(definition.kit_id), 'not found')}"
        return where
    if not definition.system_id:
        return "Not placed"
    return slot_labels.get(("SYSTEM", str(definition.system_id)), "a system")


def _differences(old, new, labels):
    """What changed in one item between two versions, as short phrases."""
    found = [
        name.replace("_", " ") for name in COMPARED if getattr(old, name) != getattr(new, name)
    ]
    if _slot(old) != _slot(new) or str(old.kit_id or "") != str(new.kit_id or ""):
        found.append(f"place ({labels(old)} to {labels(new)})")
    return found


def _slot(definition):
    if definition.compartment_id:
        return ("COMPARTMENT", definition.compartment.stable_slot_id)
    if definition.system_id:
        return ("SYSTEM", definition.system.stable_slot_id)
    return ("NOT_PLACED", None)  # SPEC 5.0.0 CHK-9: a vehicle checklist item may be Not placed


def _definitions(version):
    if version is None:
        return {}
    return {
        str(d.item_id): d
        for d in ChecklistItemDef.objects.filter(checklist_version=version).select_related(
            "compartment", "system"
        )
    }


def _item_changes(impact, org, draft, in_force):
    new, old = _definitions(draft), _definitions(in_force)
    names = dict(
        Item.objects.filter(organization_id=org, pk__in=set(new) | set(old)).values_list(
            "pk", "label"
        )
    )
    names = {str(pk): label for pk, label in names.items()}
    rows = list(new.values()) + list(old.values())
    slot_labels = {
        ("COMPARTMENT", str(pk)): label
        for pk, label in Compartment.objects.filter(
            pk__in={d.compartment_id for d in rows if d.compartment_id}
        ).values_list("pk", "label")
    }
    slot_labels.update(
        {
            ("SYSTEM", str(pk)): label
            for pk, label in VehicleSystem.objects.filter(
                pk__in={d.system_id for d in rows if d.system_id}
            ).values_list("pk", "label")
        }
    )
    kit_labels = {
        str(pk): label
        for pk, label in KitInstance.objects.filter(
            pk__in={d.kit_id for d in rows if d.kit_id}
        ).values_list("pk", "label")
    }

    def where(definition):
        return _place(definition, slot_labels, kit_labels)

    impact.added = sorted(names.get(i, i) for i in set(new) - set(old))
    impact.removed = sorted(names.get(i, i) for i in set(old) - set(new))
    impact.changed = sorted(
        (names.get(i, i), changes)
        for i in set(new) & set(old)
        if (changes := _differences(old[i], new[i], where))
    )


def _placement_refusals(version):
    """What check_publishable would refuse, read without its locks."""
    refusals = []
    for placement in placements_of(version.pk):
        definition = KitDefinition.objects.get(pk=placement.definition_id)
        if not definition.active:
            refusals.append(f"placed kit definition {definition.key!r} is inactive")
        elif not KitDefinitionPublication.objects.filter(version__definition=definition).exists():
            refusals.append(f"placed kit definition {definition.key!r} has no published version")
    return refusals


def _overrides_from(org, vehicle_id, template_id, at):
    """The vehicle's override publications for the template in force at `at`, then later ones.

    The draft is unpublished, so every one of them overlays another version: from `at` (or from
    its own effective instant) the vehicle's checklist fails closed until an override over the
    new version is published (StaleOverrideError).
    """
    publications = ChecklistPublication.objects.filter(
        organization_id=org,
        override_version__vehicle_id=vehicle_id,
        override_version__template_version__template_id=template_id,
    ).select_related("override_version")
    current = (
        publications.filter(effective_at__lte=at)
        .order_by("-effective_at", "-override_version__revision")
        .first()
    )
    # SPEC NFR-6: at most LATER_OVERRIDES scheduled ones are read; the caller says when more exist.
    later = list(
        publications.filter(effective_at__gt=at).order_by("effective_at")[: LATER_OVERRIDES + 1]
    )
    more = len(later) > LATER_OVERRIDES
    return ([current] if current else []) + later[:LATER_OVERRIDES], more


def publication_impact(organization_id, version_id, effective_at=None):
    """What publishing a draft version at `effective_at` (None: now) would do, read only."""
    canonical_id(version_id)
    org = str(organization_id)
    version = (
        ChecklistVersion.objects.filter(organization_id=org, pk=version_id)
        .select_related("template", "template__vehicle")
        .first()
    )
    if version is None:
        raise TemplateError("no such checklist version")
    if ChecklistPublication.objects.filter(checklist_version=version).exists():
        raise TemplateError("that version is already published")
    now = timezone.now()
    at = effective_at or now
    impact = PublicationImpact(
        version=version,
        effective_at=at,
        in_force=effective_template_version(org, version.template_id, at),
    )
    _item_changes(impact, org, version, impact.in_force)
    definitions = list(ChecklistItemDef.objects.filter(checklist_version=version))
    if not definitions:
        impact.blocking.append("the draft has no items")
    if effective_at is not None and effective_at < now:
        impact.blocking.append("the effective time is in the past")
    latest = (
        ChecklistPublication.objects.filter(checklist_version__template_id=version.template_id)
        .order_by("-effective_at")
        .values_list("effective_at", flat=True)
        .first()
    )
    if latest is not None and at <= latest:
        impact.blocking.append(
            f"it must take effect after the latest publication, which takes effect {_local(latest)}"
        )
    impact.blocking.extend(_placement_refusals(version))
    _vehicle_impacts(impact, org, version, definitions, at)
    _requirement_impacts(impact, org, version, definitions, at)
    return impact


# --- item requirements (SPEC 6.0.0 CHK-11; issue #256 C4) --------------------------------------


def place_label(vehicle_id, place, at=None):
    """A place's label on the vehicle: its position's latest label, or "Not placed"."""
    kind, slot = place
    if kind == "NOT_PLACED" or not slot:
        return "Not placed"
    model = Compartment if kind == "COMPARTMENT" else VehicleSystem
    rows = model.objects.filter(vehicle_id=vehicle_id, stable_slot_id=slot).order_by("-revision")
    row = (rows.filter(effective_at__lte=at).first() if at is not None else None) or rows.first()
    return row.label if row else "a removed position"


def _field_changes(old, new):
    from cya.checklists.item_requirements import FIELDS

    return [name.replace("_", " ") for name in FIELDS if getattr(old, name) != getattr(new, name)]


def _requirement_impacts(impact, org, version, definitions, at):
    """Each row's rule (a) to (d) at `at`, as publish_template_version will apply it.

    A refusal blocks the publication; a row that differs from the requirement it would share
    with other checklists is such a refusal (AC-42 (1)), so a draft saved before a requirement
    was edited is called out here rather than reverting the edit.
    """
    from cya.checklists import item_requirements as rules

    vehicle_id = version.template.vehicle_id
    if vehicle_id is None or not rules.has_cutover(org):
        return
    labels = {
        str(pk): label
        for pk, label in Item.objects.filter(pk__in={d.item_id for d in definitions}).values_list(
            "pk", "label"
        )
    }
    plans = rules.plan_rows(
        vehicle_id=vehicle_id, template_id=version.template_id, rows=definitions, effective_at=at
    )
    rows = {str(d.pk): d for d in definitions}
    for row_id, plan in sorted(plans.items(), key=lambda p: labels.get(p[1].item_id, "")):
        label = labels.get(plan.item_id, plan.item_id)
        here = place_label(vehicle_id, plan.place, at)
        if plan.rule == rules.REFUSED:
            if plan.users:
                changes = ", ".join(_field_changes(plan.version, rows[row_id]))
                impact.blocking.append(
                    f"{label} at {here}: {', '.join(plan.users)} inspect it there with other "
                    f"requirements ({changes}). Make the draft match, or change the item "
                    "requirement itself on the vehicle's item requirements page"
                )
            else:
                impact.blocking.append(f"{label} at {here}: {plan.reason}")
        elif plan.rule == rules.NEW_VERSION:
            changes = ", ".join(_field_changes(plan.version, rows[row_id]))
            impact.requirements.append(
                f"{label} at {here}: changes its item requirement ({changes})"
            )
        elif plan.rule == rules.MOVES:
            source = place_label(vehicle_id, rules._version_place(plan.version), at)
            impact.requirements.append(
                f"{label}: moves its item requirement from {source} to {here} (the same stock)"
            )
        elif plan.rule == rules.NEW_REQUIREMENT:
            impact.requirements.append(f"{label} at {here}: a new item requirement")


def minimum_publish_instant(template_id, now=None):
    """The earliest instant a new draft of the template could take effect: now, or its latest
    publication's when that is later (publish_template_version refuses anything before)."""
    now = now or timezone.now()
    latest = (
        ChecklistPublication.objects.filter(checklist_version__template_id=template_id)
        .order_by("-effective_at")
        .values_list("effective_at", flat=True)
        .first()
    )
    return max(now, latest) if latest else now


def refresh_specs(organization_id, template, version, specs):
    """Specs of a published base version, each row outside a kit taking its requirement's
    values and place in force at the draft's minimum publish instant (issue #256 C4).

    A published row keeps the values it recorded, while its requirement may have been edited
    since (CHK-5: reads follow the requirement). A new draft starting from it would otherwise
    revert that edit, or be refused. A saved draft is kept as saved; the preview calls out
    where it differs. Specs are returned unchanged before the item-requirement cutover.
    """
    from cya.checklists import item_requirements as rules
    from cya.checklists.models import ItemRequirementMap

    if template.vehicle_id is None or not rules.has_cutover(organization_id):
        return specs
    at = minimum_publish_instant(template.pk)
    mapped = {
        str(item): requirement
        for item, requirement in ItemRequirementMap.objects.filter(
            checklist_version=version, requirement__isnull=False
        ).values_list("item_def__item_id", "requirement_id")
    }
    refreshed = []
    for spec in specs:
        requirement = mapped.get(str(spec["item_id"])) if spec["kit_id"] is None else None
        current = rules.version_at(requirement, at) if requirement else None
        if current is not None:
            spec = {
                **spec,
                **{name: getattr(current, name) for name in rules.FIELDS},
                "allowed_outcomes": list(current.allowed_outcomes),
                "compartment_id": str(current.compartment_id) if current.compartment_id else None,
                "system_id": str(current.system_id) if current.system_id else None,
            }
        refreshed.append(spec)
    return refreshed


def _local(instant):
    return timezone.localtime(instant).strftime("%Y-%m-%d %H:%M")


def _slot_refs(definitions):
    """{(kind, referenced row id): (stable slot id, the referenced row's label)}."""
    refs = {}
    for kind, model, attr in (
        ("COMPARTMENT", Compartment, "compartment_id"),
        ("SYSTEM", VehicleSystem, "system_id"),
    ):
        ids = {getattr(d, attr) for d in definitions if getattr(d, attr)}
        for pk, slot, label in model.objects.filter(pk__in=ids).values_list(
            "pk", "stable_slot_id", "label"
        ):
            refs[(kind, str(pk))] = (str(slot), label)
    return refs


def _missing(refs, vehicle_id, at):
    """Labels of the placed positions the vehicle has no effective row for at `at`."""
    missing = []
    for kind, label_kind in (("COMPARTMENT", "compartment"), ("SYSTEM", "system")):
        wanted = {ref: value for (k, ref), value in refs.items() if k == kind}
        slots = vehicle_slots(label_kind, vehicle_id, list(wanted), at)
        missing += [wanted[str(ref)][1] for ref, row in slots.items() if row is None]
    return sorted(set(missing))


def _retired(refs, vehicle_id):
    labels = []
    for kind in ("COMPARTMENT", "SYSTEM"):
        slots = {slot: label for (k, _ref), (slot, label) in refs.items() if k == kind}
        labels += [slots[s] for s in position_rules.retired_slots(kind, vehicle_id, set(slots))]
    return sorted(set(labels))


def _owner_impact(impact, version, refs, at):
    """A vehicle checklist applies to its own vehicle only (SPEC 5.0.0 CHK-8, CFG-9)."""
    vehicle = version.template.vehicle
    entry = VehicleImpact(vehicle=vehicle)
    if missing := _missing(refs, str(vehicle.pk), at):
        entry.blocking.append(f"it has no position matching {', '.join(missing)}")
    if retired := _retired(refs, str(vehicle.pk)):
        entry.blocking.append(f"these positions are retired on it: {', '.join(retired)}")
    impact.vehicles.append(entry)


def _vehicle_impacts(impact, org, version, definitions, at):
    refs = _slot_refs(definitions)
    if version.template.vehicle_id is not None:
        _owner_impact(impact, version, refs, at)
        return
    # SPEC NFR-6: every read here is bounded. The candidates are at most VEHICLE_LIMIT vehicles
    # with a binding of this template and VEHICLE_LIMIT active vehicles; the page says when
    # there are more. The publication itself checks every vehicle under its locks.
    from cya.obligations.models import RequirementBinding

    with_binding = list(
        RequirementBinding.objects.filter(organization_id=org, template_id=version.template_id)
        .order_by("vehicle_id")
        .values_list("vehicle_id", flat=True)
        .distinct()[: VEHICLE_LIMIT + 1]
    )
    active_ids = list(
        Vehicle.objects.filter(organization_id=org, active=True)
        .order_by("pk")
        .values_list("pk", flat=True)[: VEHICLE_LIMIT + 1]
    )
    bound = {
        str(v)
        for v in position_rules.vehicles_bound_now_or_later(
            org, version.template_id, vehicle_ids=with_binding[:VEHICLE_LIMIT]
        )
    }
    active = {str(v) for v in active_ids[:VEHICLE_LIMIT]}
    vehicles = list(
        Vehicle.objects.filter(organization_id=org, pk__in=bound | active).order_by(
            "display_name", "pk"
        )[: VEHICLE_LIMIT + 1]
    )
    impact.more_vehicles = (
        len(vehicles) > VEHICLE_LIMIT
        or len(with_binding) > VEHICLE_LIMIT
        or len(active_ids) > VEHICLE_LIMIT
    )
    for vehicle in vehicles[:VEHICLE_LIMIT]:
        vehicle_id = str(vehicle.pk)
        entry = VehicleImpact(vehicle=vehicle)
        if vehicle_id in active and (missing := _missing(refs, vehicle_id, at)):
            # The command requires every active vehicle to have each placed position.
            entry.blocking.append(f"it has no position matching {', '.join(missing)}")
        if vehicle_id in bound:
            if retired := _retired(refs, vehicle_id):
                entry.blocking.append(f"these positions are retired on it: {', '.join(retired)}")
            overrides, more_overrides = _overrides_from(org, vehicle_id, version.template_id, at)
            for override in overrides:
                scheduled = override.effective_at > at
                entry.notes.append(
                    f"its override (revision {override.override_version.revision}"
                    + (f", taking effect {_local(override.effective_at)}" if scheduled else "")
                    + ") overlays an earlier version; its checklist cannot be resolved from "
                    + ("then" if scheduled else "this publication")
                    + " until an override over this version is published"
                )
            if more_overrides:
                entry.notes.append(
                    f"more overrides are scheduled after the first {LATER_OVERRIDES} listed; "
                    "each also overlays an earlier version"
                )
        if vehicle_id in bound or entry.blocking:
            impact.vehicles.append(entry)
