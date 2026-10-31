"""Server-side validation of checklist definition specs (SPEC 4.4, CHK-5).

A spec is a plain dict from a trusted handler. Unknown fields, wrong types and
inconsistent kind/quantity/unit combinations are rejected, never coerced or
truncated. validate_definition() also confirms every referenced row exists in
the organization; the database repeats the structural rules in CHECK
constraints and organization-pair foreign keys.
"""

from cya.authorization.backend import canonical_id
from cya.checklists.models import (
    KINDS,
    OUTCOMES,
    Compartment,
    Item,
    KitInstance,
    VehicleSystem,
)

FIELDS = frozenset(
    {
        "item_id",
        "display_order",
        "kind",
        "instructions",
        "expected_quantity",
        "unit",
        "allowed_outcomes",
        "compartment_id",
        "system_id",
        "kit_id",
    }
)
REQUIRED = frozenset({"item_id", "display_order", "kind", "allowed_outcomes"})
MAX_ITEMS = 500
MAX_INSTRUCTIONS = 4000
MAX_UNIT = 40


class ChecklistValidationError(ValueError):
    """A command was rejected before any write; the message names the problem."""


def is_count(value):
    return type(value) is int and value >= 0


def reference(spec, field, model, organization_id):
    value = spec.get(field)
    if value is None:
        return None
    try:
        canonical_id(value)
    except ValueError:
        raise ChecklistValidationError(f"{field} must be a canonical UUID") from None
    if not model.objects.filter(organization_id=organization_id, pk=value).exists():
        raise ChecklistValidationError(f"{field} {value} does not exist in the organization")
    return value


def _require_kit_in_compartment(kit_id, compartment_id):
    """A kit reference must be an active kit placed in the definition's compartment slot.

    The kit is a template reference: resolution maps it to each vehicle's own kit in that
    slot (overlay.vehicle_kits), so a kit in some other compartment is never acceptable.
    """
    kit = KitInstance.objects.select_related("compartment").get(pk=kit_id)
    if not kit.active:
        raise ChecklistValidationError(f"kit {kit_id} is inactive")
    if kit.compartment is None:
        raise ChecklistValidationError(f"kit {kit_id} is not placed in a compartment")
    wanted = Compartment.objects.get(pk=compartment_id)
    if kit.compartment.stable_slot_id != wanted.stable_slot_id:
        raise ChecklistValidationError(
            f"kit {kit_id} is not in the compartment slot of compartment {compartment_id}"
        )


def _outcomes(spec):
    outcomes = spec["allowed_outcomes"]
    if (
        not isinstance(outcomes, list)
        or not 1 <= len(outcomes) <= len(OUTCOMES)
        or len(set(outcomes)) != len(outcomes)
        or any(o not in OUTCOMES for o in outcomes)
        or "PASSED" not in outcomes
    ):
        raise ChecklistValidationError(
            f"allowed_outcomes must be distinct values from {list(OUTCOMES)} including PASSED"
        )
    return list(outcomes)


def _quantity_and_unit(spec):
    kind = spec["kind"]
    quantity, unit = spec.get("expected_quantity"), spec.get("unit")
    if kind == "QUANTITY":
        if not is_count(quantity):
            raise ChecklistValidationError("QUANTITY items need a nonnegative integer quantity")
    elif quantity is not None:
        raise ChecklistValidationError(f"{kind} items must not carry an expected quantity")
    if kind in ("MEASUREMENT", "VEHICLE_READING"):
        if not isinstance(unit, str) or not unit.strip() or len(unit) > MAX_UNIT:
            raise ChecklistValidationError(f"{kind} items need a unit of 1-{MAX_UNIT} characters")
    elif unit is not None:
        raise ChecklistValidationError(f"{kind} items must not carry a unit")
    return quantity, unit


def validate_definition(organization_id, spec):
    """Return model keyword arguments for one definition, or raise."""
    if not isinstance(spec, dict):
        raise ChecklistValidationError("a definition must be an object")
    unknown = set(spec) - FIELDS
    missing = REQUIRED - set(spec)
    if unknown or missing:
        raise ChecklistValidationError(
            f"definition fields rejected: unknown={sorted(unknown)} missing={sorted(missing)}"
        )
    if spec["kind"] not in KINDS:
        raise ChecklistValidationError(f"kind must be one of {list(KINDS)}")
    if not is_count(spec["display_order"]):
        raise ChecklistValidationError("display_order must be a nonnegative integer")
    instructions = spec.get("instructions", "")
    if not isinstance(instructions, str) or len(instructions) > MAX_INSTRUCTIONS:
        raise ChecklistValidationError(f"instructions must be text of at most {MAX_INSTRUCTIONS}")
    quantity, unit = _quantity_and_unit(spec)
    item_id = reference(spec, "item_id", Item, organization_id)
    if not Item.objects.get(pk=item_id).active:
        raise ChecklistValidationError(f"item {item_id} is inactive")
    compartment = reference(spec, "compartment_id", Compartment, organization_id)
    system = reference(spec, "system_id", VehicleSystem, organization_id)
    kit = reference(spec, "kit_id", KitInstance, organization_id)
    if (compartment is None) == (system is None) or (kit is not None and compartment is None):
        raise ChecklistValidationError(
            "give exactly one of compartment_id or system_id; a kit_id needs a compartment_id"
        )
    if kit is not None:
        _require_kit_in_compartment(kit, compartment)
    return {
        "item_id": item_id,
        "display_order": spec["display_order"],
        "kind": spec["kind"],
        "instructions": instructions,
        "expected_quantity": quantity,
        "unit": unit,
        "allowed_outcomes": _outcomes(spec),
        "compartment_id": compartment,
        "system_id": system,
        "kit_id": kit,
        "bulk_eligible": False,
    }


def validate_definitions(organization_id, specs):
    """Validate a full item set: bounded, nonempty, one definition per item."""
    if not isinstance(specs, list) or not 1 <= len(specs) <= MAX_ITEMS:
        raise ChecklistValidationError(f"provide 1-{MAX_ITEMS} item definitions")
    validated = [validate_definition(organization_id, spec) for spec in specs]
    item_ids = [definition["item_id"] for definition in validated]
    if len(set(item_ids)) != len(item_ids):
        raise ChecklistValidationError("an item may appear only once in a version")
    return validated
