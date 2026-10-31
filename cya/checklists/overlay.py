"""Effective checklist resolution: template version (+) override version as of instant T (SPEC CHK-6).

apply_overlay() is the pure ADD / REMOVE / REPLACE merge. resolve_checklist()
reads published rows, picks each side as of T, applies the overlay, maps
template slots to the vehicle's own slot rows and applies temporary stocking
deviations (SPEC 4.5). Both fail closed: an inconsistent overlay, an override
bound to a template version other than the governing one, or a slot the vehicle
lacks raises instead of silently dropping or inventing a requirement. Nothing
here writes; unpublished (draft) versions are never resolved.
"""

from dataclasses import dataclass
from datetime import datetime

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    Compartment,
    KitInstance,
    StockingDeviation,
    Vehicle,
    VehicleOverrideItem,
    VehicleSystem,
)


class ResolutionError(ValueError):
    """The effective checklist cannot be determined; the caller must not guess."""


class OverlayError(ResolutionError):
    """An override entry does not fit the template version it overlays."""


class NoEffectiveVersionError(ResolutionError):
    """No published template version is effective at the instant."""


class StaleOverrideError(ResolutionError):
    """The effective override overlays a template version other than the governing one."""


class MissingSlotError(ResolutionError):
    """A definition names a slot the vehicle has no effective row for."""


@dataclass(frozen=True)
class Requirement:
    """One item requirement as defined; placement ids are the definition's own references."""

    item_id: str
    definition_id: str
    source: str  # "TEMPLATE" or "OVERRIDE"
    display_order: int
    kind: str
    instructions: str
    expected_quantity: int | None
    unit: str | None
    allowed_outcomes: tuple[str, ...]
    compartment_id: str | None
    system_id: str | None
    kit_id: str | None


@dataclass(frozen=True)
class OverrideEntry:
    item_id: str
    action: str
    definition: Requirement | None


@dataclass(frozen=True)
class ResolvedItem:
    """A requirement resolved for one vehicle at one instant."""

    requirement: Requirement
    compartment_id: str | None  # the vehicle's own effective compartment row
    compartment_label: str | None
    system_id: str | None
    system_label: str | None
    effective_quantity: int | None  # alternate during a valid deviation, else expected
    deviation_id: str | None
    # The vehicle's own active kits in the item's kit slot as (id, label): zero, one or several.
    # Never the template's kit instance; the check records which one was present (SPEC 4.2).
    kits: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ResolvedChecklist:
    vehicle_id: str
    as_of: datetime
    template_version_id: str
    override_version_id: str | None
    items: tuple[ResolvedItem, ...]


def apply_overlay(template_items, entries):
    """Return the template requirements with ADD / REMOVE / REPLACE entries applied."""
    resolved = {requirement.item_id: requirement for requirement in template_items}
    seen = set()
    for entry in entries:
        if entry.item_id in seen:
            raise OverlayError(f"item {entry.item_id} has more than one override entry")
        seen.add(entry.item_id)
        if entry.action == "REMOVE":
            if entry.definition is not None:
                raise OverlayError(f"REMOVE of item {entry.item_id} must not carry a definition")
            if resolved.pop(entry.item_id, None) is None:
                raise OverlayError(f"REMOVE of item {entry.item_id} that the template lacks")
        elif entry.action in ("ADD", "REPLACE"):
            definition = entry.definition
            if definition is None or definition.item_id != entry.item_id:
                raise OverlayError(f"{entry.action} of item {entry.item_id} needs its definition")
            if (entry.action == "ADD") == (entry.item_id in resolved):
                raise OverlayError(
                    f"{entry.action} of item {entry.item_id} conflicts with the template"
                )
            resolved[entry.item_id] = definition
        else:
            raise OverlayError(f"unknown override action {entry.action!r}")
    return sorted(resolved.values(), key=lambda r: (r.display_order, r.item_id))


def requirement_from_fields(fields, source, definition_id=""):
    """Build a Requirement from validated definition fields (see validation.py)."""
    return Requirement(
        item_id=fields["item_id"],
        definition_id=definition_id,
        source=source,
        display_order=fields["display_order"],
        kind=fields["kind"],
        instructions=fields["instructions"],
        expected_quantity=fields["expected_quantity"],
        unit=fields["unit"],
        allowed_outcomes=tuple(fields["allowed_outcomes"]),
        compartment_id=fields["compartment_id"],
        system_id=fields["system_id"],
        kit_id=fields["kit_id"],
    )


def requirement_from_definition(definition, source):
    return requirement_from_fields(
        {
            "item_id": definition.item_id,
            "display_order": definition.display_order,
            "kind": definition.kind,
            "instructions": definition.instructions,
            "expected_quantity": definition.expected_quantity,
            "unit": definition.unit,
            "allowed_outcomes": definition.allowed_outcomes,
            "compartment_id": definition.compartment_id,
            "system_id": definition.system_id,
            "kit_id": definition.kit_id,
        },
        source,
        definition.pk,
    )


def _latest_publication(queryset, as_of, revision_field, recorded_before=None):
    queryset = queryset.filter(effective_at__lte=as_of)
    if recorded_before is not None:
        queryset = queryset.filter(recorded_at__lt=recorded_before)
    return (
        queryset.order_by("-effective_at", f"-{revision_field}")
        .select_related("checklist_version", "override_version")
        .first()
    )


def governing_versions(organization_id, vehicle_id, template_id, as_of, recorded_before=None):
    """Return (template version, override version or None) effective at as_of.

    With recorded_before, only publications recorded strictly before that instant count: what
    would have governed as the records stood then (used to judge a change against a window).
    """
    publications = ChecklistPublication.objects.filter(organization_id=organization_id)
    template_publication = _latest_publication(
        publications.filter(checklist_version__template_id=template_id),
        as_of,
        "checklist_version__revision",
        recorded_before,
    )
    if template_publication is None:
        raise NoEffectiveVersionError(f"template {template_id} has no version effective at {as_of}")
    override_publication = _latest_publication(
        publications.filter(
            override_version__vehicle_id=vehicle_id,
            override_version__template_version__template_id=template_id,
        ),
        as_of,
        "override_version__revision",
        recorded_before,
    )
    version = template_publication.checklist_version
    override = override_publication.override_version if override_publication else None
    if override is not None and override.template_version_id != version.pk:
        raise StaleOverrideError(
            f"override {override.pk} overlays template version {override.template_version_id}, "
            f"but {version.pk} governs at {as_of}; publish a matching override version"
        )
    return version, override


def stale_override_vehicle_ids(organization_id, template_id, as_of):
    """Vehicles whose effective override overlays a different template version than governs.

    Resolution fails closed for these vehicles (StaleOverrideError). Operators run this after
    publishing a template version with a future effective instant, then publish matching
    override versions before that instant.
    """
    stale = []
    for vehicle_id in Vehicle.objects.filter(
        organization_id=organization_id, active=True
    ).values_list("pk", flat=True):
        try:
            governing_versions(organization_id, vehicle_id, template_id, as_of)
        except StaleOverrideError:
            stale.append(vehicle_id)
    return sorted(stale)


def _requirements(version, override):
    template_items = [
        requirement_from_definition(definition, "TEMPLATE")
        for definition in ChecklistItemDef.objects.filter(checklist_version=version)
    ]
    entries = []
    if override is not None:
        defs = {
            definition.pk: definition
            for definition in ChecklistItemDef.objects.filter(override_version=override)
        }
        for entry in VehicleOverrideItem.objects.filter(override_version=override):
            definition = defs.get(entry.definition_id)
            entries.append(
                OverrideEntry(
                    entry.item_id,
                    entry.action,
                    requirement_from_definition(definition, "OVERRIDE") if definition else None,
                )
            )
    return apply_overlay(template_items, entries)


def vehicle_slots(kind, vehicle_id, reference_ids, as_of):
    """Map a referenced slot row id to the vehicle's own latest effective row of that slot.

    kind is "compartment" or "system". A reference the vehicle has no effective row for maps
    to None; callers decide whether that is an error.
    """
    model = {"compartment": Compartment, "system": VehicleSystem}[kind]
    if not reference_ids:
        return {}
    slot_of = dict(model.objects.filter(pk__in=reference_ids).values_list("pk", "stable_slot_id"))
    rows = model.objects.filter(
        vehicle_id=vehicle_id, stable_slot_id__in=set(slot_of.values()), effective_at__lte=as_of
    ).order_by("stable_slot_id", "-revision")
    latest = {}
    for row in rows:
        latest.setdefault(row.stable_slot_id, row)
    return {ref: latest.get(slot) for ref, slot in slot_of.items()}


def vehicle_kits(vehicle_id, reference_ids):
    """Map a referenced kit id to the vehicle's own active kits in the same compartment slot.

    A kit is a physical object bound to one vehicle (SPEC 4.2), so a definition's kit is a
    template reference naming a slot, never an instance to propagate. Vehicles carry a
    variable number of a kit (none, one or several): the result is every active kit the
    vehicle keeps in that slot, possibly empty, ordered by label. Which one was present is
    recorded on each check.
    """
    if not reference_ids:
        return {}
    own = {}
    for kit in KitInstance.objects.filter(
        vehicle_id=vehicle_id, active=True, compartment__isnull=False
    ).select_related("compartment"):
        own.setdefault(kit.compartment.stable_slot_id, []).append((kit.label, kit.pk))
    result = {}
    from cya.kits.history import slot_of_reference_kit  # imported late: kits depends on checklists

    for ref in KitInstance.objects.filter(pk__in=reference_ids):
        slot = slot_of_reference_kit(ref.pk)  # the slot it was first placed in, even if it moved
        found = sorted(own.get(slot, [])) if slot else []
        result[ref.pk] = tuple((pk, label) for label, pk in found)
    return result


def _active_deviations(organization_id, vehicle_id, item_ids, as_of):
    """Tail revision recorded by as_of per item, kept only while its window is open."""
    tails = {}
    for deviation in StockingDeviation.objects.filter(
        organization_id=organization_id,
        vehicle_id=vehicle_id,
        item_id__in=item_ids,
        recorded_at__lte=as_of,
    ).order_by("item_id", "-revision"):
        tails.setdefault(deviation.item_id, deviation)
    return {
        item_id: deviation
        for item_id, deviation in tails.items()
        if deviation.valid_from <= as_of < deviation.valid_until
    }


def resolve_checklist(*, organization_id, vehicle_id, template_id, as_of, recorded_before=None):
    """Effective checklist for one vehicle at as_of (a timezone-aware instant).

    recorded_before (optional, timezone-aware) ignores publications recorded at or after it.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    if not Vehicle.objects.filter(organization_id=organization_id, pk=vehicle_id).exists():
        raise ResolutionError(f"vehicle {vehicle_id} is not in organization {organization_id}")
    version, override = governing_versions(
        organization_id, vehicle_id, template_id, as_of, recorded_before
    )
    requirements = _requirements(version, override)
    compartments = vehicle_slots(
        "compartment",
        vehicle_id,
        {r.compartment_id for r in requirements if r.compartment_id},
        as_of,
    )
    systems = vehicle_slots(
        "system", vehicle_id, {r.system_id for r in requirements if r.system_id}, as_of
    )
    kits = vehicle_kits(vehicle_id, {r.kit_id for r in requirements if r.kit_id})
    deviations = _active_deviations(
        organization_id,
        vehicle_id,
        [r.item_id for r in requirements if r.kind == "QUANTITY"],
        as_of,
    )
    items = []
    for requirement in requirements:
        compartment = compartments.get(requirement.compartment_id)
        system = systems.get(requirement.system_id)
        if (requirement.compartment_id and compartment is None) or (
            requirement.system_id and system is None
        ):
            raise MissingSlotError(
                f"vehicle {vehicle_id} has no effective slot for item {requirement.item_id} "
                f"at {as_of}"
            )
        deviation = deviations.get(requirement.item_id)
        items.append(
            ResolvedItem(
                requirement=requirement,
                compartment_id=compartment.pk if compartment else None,
                compartment_label=compartment.label if compartment else None,
                system_id=system.pk if system else None,
                system_label=system.label if system else None,
                effective_quantity=(
                    deviation.alternate_quantity if deviation else requirement.expected_quantity
                ),
                deviation_id=deviation.pk if deviation else None,
                kits=kits.get(requirement.kit_id, ()),
            )
        )
    return ResolvedChecklist(
        vehicle_id=vehicle_id,
        as_of=as_of,
        template_version_id=version.pk,
        override_version_id=override.pk if override else None,
        items=tuple(items),
    )


__all__ = [
    "MissingSlotError",
    "NoEffectiveVersionError",
    "OverlayError",
    "OverrideEntry",
    "Requirement",
    "ResolutionError",
    "ResolvedChecklist",
    "ResolvedItem",
    "StaleOverrideError",
    "apply_overlay",
    "governing_versions",
    "requirement_from_definition",
    "requirement_from_fields",
    "resolve_checklist",
    "stale_override_vehicle_ids",
    "vehicle_slots",
]
