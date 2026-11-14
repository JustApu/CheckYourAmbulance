"""Kit presence items (SPEC 2.0.0 4.6 KDEF-12, issue #129).

Each kit definition has a presence item: an organization item created with the definition and
named after it, which a kit presence check counts. It is never kit content (the definition
commands and the kit-library importer refuse it), and its label follows the definition's.
"""

import uuid

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
