"""Kits placed by definition in a checklist version (SPEC 4.6 KDEF-4, issue #129 part 4a).

validate_placements checks the placements a template draft carries; check_publishable checks
them again when the version is published; placements_of reads a version's rows. Obligations do
not use placements yet (KDEF-8: slot-placed kit items keep working until a later part of issue
#129 makes requirements come from definitions). Vehicle-override placements arrive with that
part, which also defines how an override adds or removes a template's placements.
"""

from cya.authorization.backend import canonical_id
from cya.checklists.models import Compartment
from cya.checklists.validation import ChecklistValidationError, is_count
from cya.kits.models import ChecklistKitPlacement, KitDefinition, KitDefinitionPublication

MAX_PLACEMENTS = 200
FIELDS = frozenset({"definition_id", "compartment_id", "display_order"})


def _reference(value, model, organization_id, where):
    try:
        canonical_id(value)
    except (TypeError, ValueError):
        raise ChecklistValidationError(f"{where} must be a canonical UUID") from None
    row = model.objects.filter(organization_id=organization_id, pk=value).first()
    if row is None:
        raise ChecklistValidationError(f"{where} {value} does not exist in the organization")
    return row


def validate_placements(organization_id, placements):
    """Model fields for each placement of a template draft, or raise naming its position."""
    if not isinstance(placements, list) or len(placements) > MAX_PLACEMENTS:
        raise ChecklistValidationError(f"placements must be a list of at most {MAX_PLACEMENTS}")
    validated, seen = [], set()
    for index, spec in enumerate(placements):
        where = f"placements[{index}]"
        if not isinstance(spec, dict) or set(spec) != FIELDS:
            raise ChecklistValidationError(f"{where} needs exactly {sorted(FIELDS)}")
        if not is_count(spec["display_order"]):
            raise ChecklistValidationError(f"{where} display_order must be a nonnegative integer")
        definition = _reference(
            spec["definition_id"], KitDefinition, organization_id, f"{where} definition_id"
        )
        if not definition.active:
            raise ChecklistValidationError(f"{where} kit definition {definition.key!r} is inactive")
        compartment = _reference(
            spec["compartment_id"], Compartment, organization_id, f"{where} compartment_id"
        )
        key = (compartment.stable_slot_id, definition.pk)
        if key in seen:
            raise ChecklistValidationError(
                f"{where} places kit {definition.key!r} in that slot twice"
            )
        seen.add(key)
        validated.append(
            {
                "definition_id": str(definition.pk),
                "compartment_id": str(compartment.pk),
                "display_order": spec["display_order"],
            }
        )
    return validated


def placements_of(checklist_version_id):
    return list(
        ChecklistKitPlacement.objects.filter(checklist_version_id=checklist_version_id).order_by(
            "display_order", "id"
        )
    )


def check_publishable(placements):
    """Every placed definition must be active and have a published version (KDEF-2, KDEF-4)."""
    for placement in placements:
        definition = KitDefinition.objects.get(pk=placement.definition_id)
        if not definition.active:
            raise ChecklistValidationError(f"placed kit definition {definition.key!r} is inactive")
        if not KitDefinitionPublication.objects.filter(version__definition=definition).exists():
            raise ChecklistValidationError(
                f"placed kit definition {definition.key!r} has no published version"
            )
