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
from cya.kits import keys as key_rules
from cya.kits import positions as position_rules
from cya.kits.models import KitDefinition, KitDefinitionPublication
from cya.kits.placements import placements_of
from cya.operations.vehicles import MAX_NAME, VehicleError, _plan, _run

ACTION = "draft_edit_template"
VEHICLE_LIMIT = 500  # vehicles the impact preview checks (SPEC NFR-6)
CREATE_COMMAND = "TEMPLATE_CREATE"
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

    @property
    def refused(self):
        return bool(self.blocking) or any(v.blocking for v in self.vehicles)


def _place(definition, slot_labels, kit_labels):
    if definition.compartment_id:
        where = slot_labels.get(("COMPARTMENT", str(definition.compartment_id)), "a compartment")
        if definition.kit_id:
            return f"{where}, kit {kit_labels.get(str(definition.kit_id), 'not found')}"
        return where
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
    return ("SYSTEM", definition.system.stable_slot_id)


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
    later = list(publications.filter(effective_at__gt=at).order_by("effective_at"))
    return ([current] if current else []) + later


def publication_impact(organization_id, version_id, effective_at=None):
    """What publishing a draft version at `effective_at` (None: now) would do, read only."""
    canonical_id(version_id)
    org = str(organization_id)
    version = (
        ChecklistVersion.objects.filter(organization_id=org, pk=version_id)
        .select_related("template")
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
    return impact


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


def _vehicle_impacts(impact, org, version, definitions, at):
    refs = _slot_refs(definitions)
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
            for override in _overrides_from(org, vehicle_id, version.template_id, at):
                scheduled = override.effective_at > at
                entry.notes.append(
                    f"its override (revision {override.override_version.revision}"
                    + (f", taking effect {_local(override.effective_at)}" if scheduled else "")
                    + ") overlays an earlier version; its checklist cannot be resolved from "
                    + ("then" if scheduled else "this publication")
                    + " until an override over this version is published"
                )
        if vehicle_id in bound or entry.blocking:
            impact.vehicles.append(entry)
