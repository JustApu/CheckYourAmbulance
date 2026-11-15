"""Kit presence items (SPEC 2.0.0 4.6 KDEF-12, issue #129).

Each kit definition has a presence item: an organization item created with the definition and
named after it, which a kit presence check counts. It is never kit content (the definition
commands and the kit-library importer refuse it), and its label follows the definition's.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

from cya.accounts.fields import new_id
from cya.checklists.models import Item

SUFFIX = " (present)"


def presence_label(definition_label):
    return f"{definition_label}{SUFFIX}"


def create_presence_item(organization_id, definition_label):
    """Create and return the presence item for a new definition; call inside its command."""
    return Item.objects.create(
        id=new_id(),
        organization_id=organization_id,
        label=presence_label(definition_label),
        active=True,
    )


def presence_item_ids(organization_id):
    """Ids of every presence item in the organization."""
    from cya.kits.models import KitDefinition

    return {
        str(pk)
        for pk in KitDefinition.objects.filter(organization_id=organization_id).values_list(
            "presence_item_id", flat=True
        )
    }


# Fixed namespace for presence unit keys; changing it would change every presence stream.
UNIT_NAMESPACE = uuid.UUID("6f3c1b7e-2d4a-5e8f-9a1b-3c5d7e9f1a2b")


def line_unit_key(vehicle_id, definition_id, stable_slot_id):
    """The obligation-unit key of a kit list line (KDEF-12): vehicle, definition and slot.

    Never the count or the list revision, so a count change replaces the requirement on the
    same stream while two compartments of one definition stay separate streams. It is an
    internal comparison key only: the obligation's kit_instance_id stays NULL.
    """
    name = f"{vehicle_id}:{definition_id}:{stable_slot_id}"
    return str(uuid.uuid5(UNIT_NAMESPACE, name))


def line_unit_keys(kit_requirement_ids):
    """{kit list line id: its unit key} for the given lines."""
    from cya.kits.models import VehicleKitRequirement

    return {
        str(pk): line_unit_key(vehicle, definition, slot)
        for pk, vehicle, definition, slot in VehicleKitRequirement.objects.filter(
            pk__in=list(kit_requirement_ids)
        ).values_list("pk", "version__vehicle_id", "definition_id", "compartment__stable_slot_id")
    }


PRESENCE_SOURCE = "KIT_PRESENCE"  # a requirement whose definition_id is a kit list line


def presence_resolved_item(organization_id, resolved, kit_requirement_id, item_id, as_of):
    """The presence check of one kit list line as a Quantity item (KDEF-10, KDEF-12), or None.

    The line must be on a published kit list of the resolved vehicle, its definition placed by
    the resolved checklist version, and item_id that definition's presence item. The target is
    the line's exact required count; the item is located in the vehicle's own compartment row of
    the line's slot at as_of (or the line's row when the slot has no later one).
    """
    from cya.checklists.models import Compartment
    from cya.checklists.overlay import Requirement, ResolvedItem
    from cya.kits.models import VehicleKitListPublication, VehicleKitRequirement

    line = (
        VehicleKitRequirement.objects.filter(
            organization_id=organization_id,
            pk=kit_requirement_id,
            version__vehicle_id=resolved.vehicle_id,
        )
        .select_related("definition", "compartment")
        .first()
    )
    if line is None or str(line.definition.presence_item_id) != str(item_id):
        return None
    if not VehicleKitListPublication.objects.filter(version_id=line.version_id).exists():
        return None
    placement = next(
        (p for p in resolved.placements if p.definition_id == str(line.definition_id)), None
    )
    if placement is None:
        return None
    row = (
        Compartment.objects.filter(
            vehicle_id=resolved.vehicle_id,
            stable_slot_id=line.compartment.stable_slot_id,
            effective_at__lte=as_of,
        )
        .order_by("-revision")
        .first()
    ) or line.compartment
    requirement = Requirement(
        item_id=str(item_id),
        definition_id=str(line.pk),
        source=PRESENCE_SOURCE,
        display_order=line.display_order,
        kind="QUANTITY",
        instructions="",
        expected_quantity=line.required_count,
        unit=None,
        allowed_outcomes=("PASSED", "FAILED", "UNABLE_TO_INSPECT"),
        compartment_id=str(row.pk),
        system_id=None,
        kit_id=None,
    )
    return ResolvedItem(
        requirement=requirement,
        compartment_id=str(row.pk),
        compartment_label=row.label,
        system_id=None,
        system_label=None,
        effective_quantity=line.required_count,
        deviation_id=None,
        kits=(),
    )


def standalone_presence_item(organization_id, vehicle_id, kit_requirement_id, item_id, as_of):
    """Resolve a current line only if no bound checklist inspects its definition."""
    from cya.checklists.overlay import resolve_checklist
    from cya.kits.kit_lists import kit_list_effective_at
    from cya.kits.models import VehicleKitRequirement
    from cya.obligations.requirements import bound_template_ids

    line = (
        VehicleKitRequirement.objects.filter(
            organization_id=organization_id, pk=kit_requirement_id, version__vehicle_id=vehicle_id
        )
        .select_related("definition")
        .first()
    )
    if line is None or str(line.definition.presence_item_id) != str(item_id):
        return None
    current = kit_list_effective_at(
        organization_id, vehicle_id, as_of, as_of + timedelta(microseconds=1)
    )
    if current is None or str(current.pk) != str(line.version_id):
        return None
    templates = bound_template_ids(organization_id, vehicle_id, as_of)
    if len(templates) > 2000:
        raise ValueError("too many bound checklists to resolve kit presence")
    for template_id in templates:
        resolved = resolve_checklist(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            template_id=template_id,
            as_of=as_of,
            recorded_before=as_of + timedelta(microseconds=1),
        )
        if any(
            placement.definition_id == str(line.definition_id) for placement in resolved.placements
        ):
            return None
    # Reuse the same line validation and quantity construction as the checklist-linked path;
    # the synthetic placement is not persisted and is allowed only after the bound-template check.
    resolved = SimpleNamespace(
        vehicle_id=vehicle_id,
        placements=(SimpleNamespace(definition_id=str(line.definition_id)),),
    )
    return presence_resolved_item(organization_id, resolved, kit_requirement_id, item_id, as_of)
