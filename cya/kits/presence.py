"""Kit presence items (SPEC 2.0.0 4.6 KDEF-12, issue #129).

Each kit definition has a presence item: an organization item created with the definition and
named after it, which a kit presence check counts. It is never kit content (the definition
commands and the kit-library importer refuse it), and its label follows the definition's.
"""

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
