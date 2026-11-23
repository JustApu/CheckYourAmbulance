"""Place of a loose-item deficiency before the item-requirement migration (SPEC 6.0.0 18.3 item 2).

Issue #256 part C3. A deficiency that names an item outside a kit (item set, kit and compartment
null) says which item is wrong but not which stock of it. While the vehicle keeps the item in one
place that is enough; where it keeps it in more than one, the migration to vehicle item requirements
cannot decide which requirement the deficiency concerns. It never attaches the deficiency to every
place: it refuses, listing the deficiency, until a Supervisor or Administrator records the place
with assign_deficiency_place.

The place is recorded as a position (compartment or system by stable slot, or Not placed), as on a
requirement version, because requirements do not exist before the migration. The position must be
one where a published checklist of the deficiency's vehicle keeps the item outside a kit; the
migration maps it to the requirement in force at that place when the deficiency was reported (or,
failing that, the latest requirement kept there). One place per deficiency, recorded once.

The command runs under the policy action expand_verification (Supervisor and Administrator): the
place decides which stock the deficiency's correction, verification and suspensions concern. It
takes the vehicle lock first, which the migration also holds for its whole run, so the migration
plans either before or after an assignment and never between.
"""

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.item_requirements import has_cutover
from cya.checklists.models import (
    PLACE_KINDS,
    ChecklistItemDef,
    ChecklistPublication,
    Compartment,
    Vehicle,
    VehicleSystem,
)
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies import service
from cya.deficiencies.commands import _authorizer, _operation_id
from cya.deficiencies.models import Deficiency, DeficiencyPlace
from cya.deficiencies.service import DeficiencyError
from cya.operations.models import MAX_TEXT

PLACE_ACTION = "expand_verification"
PLACE_COMMAND = "DEFICIENCY_PLACE_ASSIGN"


def place_of_row(row):
    """(place_kind, stable slot id or None) of an item row or a place record."""
    if row.compartment_id:
        return ("COMPARTMENT", str(row.compartment.stable_slot_id))
    if row.system_id:
        return ("SYSTEM", str(row.system.stable_slot_id))
    return ("NOT_PLACED", None)


def kept_places(organization_id, vehicle_id, item_id):
    """Every place where a published checklist of the vehicle keeps the item outside a kit."""
    published = ChecklistPublication.objects.filter(
        organization_id=organization_id, checklist_version__isnull=False
    ).values("checklist_version_id")
    rows = ChecklistItemDef.objects.filter(
        organization_id=organization_id,
        checklist_version__template__vehicle_id=vehicle_id,
        checklist_version_id__in=published,
        item_id=item_id,
        kit__isnull=True,
        override_version__isnull=True,
    ).select_related("compartment", "system")
    return {place_of_row(row) for row in rows}


def _position(organization_id, vehicle_id, place_kind, position_id):
    """The compartment or system revision row named, which must belong to the vehicle."""
    if place_kind == "NOT_PLACED":
        return None
    model = Compartment if place_kind == "COMPARTMENT" else VehicleSystem
    row = model.objects.filter(
        organization_id=organization_id, vehicle_id=vehicle_id, pk=position_id
    ).first()
    if row is None:
        raise DeficiencyError(
            "INVALID_PLACE",
            "the position is not a compartment or system of the deficiency's vehicle",
            path="position_id",
        )
    return row


def _parse_place(place_kind, position_id):
    if place_kind not in PLACE_KINDS:
        raise service.malformed(
            "place_kind", "place_kind must be COMPARTMENT, SYSTEM or NOT_PLACED"
        )
    if place_kind == "NOT_PLACED":
        if position_id is not None:
            raise service.malformed("position_id", "a Not placed place names no position")
        return None
    return service.canonical(position_id, "position_id")


def assign_deficiency_place(
    *,
    deficiency_id,
    place_kind,
    explanation,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    position_id=None,
):
    """Record which place of its item a loose-item deficiency concerns (SPEC 18.3 item 2).

    place_kind is COMPARTMENT or SYSTEM with position_id a compartment or system row of the
    deficiency's vehicle, or NOT_PLACED without one; explanation is required (at most 4,000
    characters). The deficiency must name an item outside a kit (422 NOT_A_LOOSE_ITEM), have no
    place yet (422 PLACE_ALREADY_ASSIGNED), and a published checklist of its vehicle must keep the
    item there outside a kit (422 PLACE_NOT_KEPT). After the organization's item-requirement
    cutover nothing is assigned any more (422 CUTOVER_DONE). Supervisors and Administrators only. A replay
    with the same operation_id returns the stored answer.
    """
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    position_id = _parse_place(place_kind, position_id)
    explanation = service.text(explanation, "explanation", MAX_TEXT)

    def build(_actor):
        vehicle_id = (
            Deficiency.objects.filter(organization_id=organization_id, pk=deficiency_id)
            .values_list("vehicle_id", flat=True)
            .first()
        )
        if vehicle_id is not None:
            # The configuration lock the migration holds: it plans before or after this.
            Vehicle.objects.select_for_update().filter(
                organization_id=organization_id, pk=vehicle_id
            ).first()
        service.lock_deficiencies(organization_id, [deficiency_id])
        deficiency = Deficiency.objects.get(organization_id=organization_id, pk=deficiency_id)
        if deficiency.item_id is None or deficiency.kit_id or deficiency.compartment_id:
            raise DeficiencyError(
                "NOT_A_LOOSE_ITEM",
                "only a deficiency that names an item outside a kit has a place to assign",
                path="deficiency_id",
            )
        if has_cutover(organization_id):
            # The cutover maps recorded places once (deficiency_requirement_map); after it, a
            # deficiency on an item outside a kit names its item requirement when reported.
            raise DeficiencyError(
                "CUTOVER_DONE",
                "the organization already uses item requirements; places are no longer assigned",
                path="deficiency_id",
            )
        if DeficiencyPlace.objects.filter(deficiency_id=deficiency_id).exists():
            raise DeficiencyError(
                "PLACE_ALREADY_ASSIGNED",
                "the deficiency's place is already recorded",
                path="deficiency_id",
            )
        vehicle_id = str(deficiency.vehicle_id)
        position = _position(organization_id, vehicle_id, place_kind, position_id)
        place = (place_kind, None if position is None else str(position.stable_slot_id))
        if place not in kept_places(organization_id, vehicle_id, deficiency.item_id):
            raise DeficiencyError(
                "PLACE_NOT_KEPT",
                "no published checklist of the vehicle keeps the item at that place",
                path="position_id" if position_id else "place_kind",
            )
        place_id = new_id()
        now = timezone.now()

        def apply(receipt, current):
            DeficiencyPlace.objects.create(
                id=place_id,
                organization_id=organization_id,
                deficiency_id=deficiency_id,
                vehicle_id=vehicle_id,
                item_id=deficiency.item_id,
                place_kind=place[0],
                place_slot_id=place[1],
                compartment_id=position_id if place_kind == "COMPARTMENT" else None,
                system_id=position_id if place_kind == "SYSTEM" else None,
                explanation=explanation,
                **service.stamp(receipt, current),
            )

        values = {
            "deficiency_place_id": place_id,
            "deficiency_id": deficiency_id,
            "place_kind": place[0],
            "place_slot_id": place[1],
        }
        return Plan(
            response_body={"code": "DEFICIENCY_PLACE_ASSIGNED", **values},
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={"schema_version": 1, "values": values},
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=PLACE_COMMAND,
        request_digest=digest(
            {
                "deficiency_id": deficiency_id,
                "place_kind": place_kind,
                "position_id": position_id,
                "explanation": explanation,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, PLACE_ACTION),
        prepare=build,
    )
