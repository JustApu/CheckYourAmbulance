"""Kit definitions a checklist version inspects (SPEC 4.6 KDEF-4, issue #129 part 4a).

A placement names a kit definition only: where and how many instances each vehicle should carry
come from the vehicle's kit list (cya.kits.kit_lists, KDEF-9). validate_placements checks the
placements a template draft carries; check_publishable checks them again when the version is
published; placements_of reads a version's rows. Obligations do not use placements yet (KDEF-8:
slot-placed kit items keep working until a later part of issue #129).
"""

from cya.authorization.backend import canonical_id
from cya.checklists.validation import ChecklistValidationError, is_count
from cya.kits.models import ChecklistKitPlacement, KitDefinition, KitDefinitionPublication

FIELDS = frozenset({"definition_id", "display_order"})


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
    if not isinstance(placements, list):
        raise ChecklistValidationError("placements must be a list")
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
        if definition.pk in seen:
            raise ChecklistValidationError(f"{where} places kit {definition.key!r} twice")
        seen.add(definition.pk)
        validated.append(
            {"definition_id": str(definition.pk), "display_order": spec["display_order"]}
        )
    return validated


def placements_of(checklist_version_id):
    return list(
        ChecklistKitPlacement.objects.filter(checklist_version_id=checklist_version_id).order_by(
            "display_order", "id"
        )
    )


def check_publishable(placements):
    """Every placed definition must be active and have a published version (KDEF-2, KDEF-4).

    Locks the placed definitions in id order until the publishing command commits, so a
    concurrent set_kit_definition_active (which locks the same row) cannot deactivate one
    between this check and the publication.
    """
    locked = {
        str(d.pk): d
        for d in KitDefinition.objects.select_for_update()
        .filter(pk__in={p.definition_id for p in placements})
        .order_by("pk")
    }
    for placement in placements:
        definition = locked[str(placement.definition_id)]
        if not definition.active:
            raise ChecklistValidationError(f"placed kit definition {definition.key!r} is inactive")
        if not KitDefinitionPublication.objects.filter(version__definition=definition).exists():
            raise ChecklistValidationError(
                f"placed kit definition {definition.key!r} has no published version"
            )


KIT_DEFINITION_SOURCE = "KIT_DEFINITION"


def definition_version_at(organization_id, definition_id, at, recorded_before=None):
    """The version id of a kit definition in force at `at`, as the records stood, or None."""
    publications = KitDefinitionPublication.objects.filter(
        organization_id=organization_id, version__definition_id=definition_id, effective_at__lte=at
    )
    if recorded_before is not None:
        publications = publications.filter(recorded_at__lt=recorded_before)
    latest = publications.order_by("-effective_at").values_list("version_id", flat=True).first()
    return str(latest) if latest else None


def placed_contents(organization_id, resolved, item_id, at, recorded_before=None):
    """The item as content of each kit the resolved checklist places (KDEF-4, KDEF-7).

    One ResolvedItem per placement whose definition, in the version in force at `at`, lists the
    item. Its requirement's definition_id is the kit_definition_item id, its source is
    KIT_DEFINITION, its kit_id the placement id (so the item counts as inside a kit), and its
    kits the vehicle's own kits of that definition in the placed slot.
    """
    # Imported late: overlay is imported by the checklists app, which the kits app depends on.
    from cya.checklists.overlay import Requirement, ResolvedItem
    from cya.kits.models import KitDefinitionItem

    found = []
    for placement in resolved.placements:
        version_id = definition_version_at(
            organization_id, placement.definition_id, at, recorded_before
        )
        if version_id is None:
            continue
        content = KitDefinitionItem.objects.filter(version_id=version_id, item_id=item_id).first()
        if content is None:
            continue
        requirement = Requirement(
            item_id=str(content.item_id),
            definition_id=str(content.pk),
            source=KIT_DEFINITION_SOURCE,
            display_order=content.display_order,
            kind=content.kind,
            instructions=content.instructions,
            expected_quantity=content.expected_quantity,
            unit=content.unit,
            allowed_outcomes=tuple(content.allowed_outcomes),
            compartment_id=placement.compartment_id,
            system_id=None,
            kit_id=placement.placement_id,
        )
        found.append(
            ResolvedItem(
                requirement=requirement,
                compartment_id=placement.compartment_id,
                compartment_label=placement.compartment_label,
                system_id=None,
                system_label=None,
                effective_quantity=content.expected_quantity,
                deviation_id=None,
                kits=placement.kits,
            )
        )
    return found


def placed_content_item_ids(placements):
    """Every item any published version of the placed definitions lists (for item locks)."""
    from cya.kits.models import KitDefinitionItem

    return list(
        KitDefinitionItem.objects.filter(
            version__definition_id__in=[p.definition_id for p in placements],
            version__in=KitDefinitionPublication.objects.values("version_id"),
        )
        .values_list("item_id", flat=True)
        .distinct()
    )


def placed_items(organization_id, resolved, at, recorded_before=None):
    """Every content entry of every kit the resolved checklist places, as ResolvedItems."""
    from cya.kits.models import KitDefinitionItem

    items = []
    for placement in resolved.placements:
        version_id = definition_version_at(
            organization_id, placement.definition_id, at, recorded_before
        )
        if version_id is None:
            continue
        for content in KitDefinitionItem.objects.filter(version_id=version_id).order_by(
            "display_order", "id"
        ):
            items.extend(
                placed_contents(
                    organization_id, resolved, str(content.item_id), at, recorded_before
                )
            )
    unique = {}
    for item in items:
        unique[(item.requirement.definition_id, item.requirement.kit_id)] = item
    return list(unique.values())


def is_kit_content(definition_id):
    """True when the id names a placed kit definition's content entry, not a checklist item."""
    from cya.kits.models import KitDefinitionItem

    return KitDefinitionItem.objects.filter(pk=definition_id).exists()


def content_columns(definition_id):
    """The {definition_id, kit_definition_item_id} pair for a scope row from one content id."""
    if is_kit_content(definition_id):
        return {"definition_id": None, "kit_definition_item_id": str(definition_id)}
    return {"definition_id": str(definition_id), "kit_definition_item_id": None}
