"""Explicit, immutable human orders, outside routine reconciliation (SPEC ADH-1)."""

from datetime import UTC, datetime

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.attention.checklist import ChecklistQueryError, _resolved_items
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import ChecklistPublication
from cya.checklists.overlay import resolve_checklist
from cya.checks.models import ItemLock
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies.commands import _authorizer
from cya.kits.history import active_kits, slot_of_reference_kit
from cya.obligations.generation import UnitUnavailable, lock_unit_scope
from cya.obligations.models import Obligation, RequirementVersion
from cya.obligations.state import NO_KIT
from cya.operations.commands import StatusError, _text

ACTION = "request_ad_hoc_inspection"
COMMAND_TYPE = "AD_HOC_INSPECTION_REQUEST"
MAX_ITEMS = 200


def _items(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ITEMS:
        raise StatusError(
            "choose 1–200 item/kit pairs", code="MALFORMED_REQUEST", status=400, path="items"
        )
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"item_id", "kit_instance_id"}:
            raise StatusError(
                "each item needs item_id and kit_instance_id",
                code="MALFORMED_REQUEST",
                status=400,
                path="items",
            )
        try:
            item = canonical_id(row["item_id"])
            kit = None if row["kit_instance_id"] is None else canonical_id(row["kit_instance_id"])
        except (TypeError, ValueError):
            raise StatusError(
                "invalid item/kit id", code="MALFORMED_REQUEST", status=400, path="items"
            ) from None
        pair = (item, kit)
        if pair in result:
            raise StatusError(
                "duplicate item/kit pair", code="MALFORMED_REQUEST", status=400, path="items"
            )
        result.append(pair)
    return sorted(result, key=lambda pair: (pair[0], pair[1] or ""))


def _pins(org, vehicle, due_at, items):
    resolved = _resolved_items(org, vehicle, due_at)
    result = []
    version_pins = {}
    kit_slots = {}
    for item_id, kit_id in items:
        if item_id not in resolved:
            raise StatusError(
                "item is not in this vehicle's due-time checklist",
                code="INVALID_SCOPE",
                path="items",
            )
        _order, row, version_id = resolved[item_id]
        requirement = row.requirement
        if requirement.kind == "VEHICLE_READING":
            raise StatusError(
                "choose checklist items recorded through item checks",
                code="INVALID_SCOPE",
                path="items",
            )
        kit_event = None
        if requirement.kit_id:
            slot = slot_of_reference_kit(str(requirement.kit_id))
            if slot not in kit_slots:
                kit_slots[slot] = active_kits(org, vehicle, slot, due_at) if slot else ()
            kits = kit_slots[slot]
            kit = next((k for k in kits if k.kit_id == kit_id), None)
            if kit is None:
                raise StatusError(
                    "kit is not assigned to the item's vehicle slot",
                    code="INVALID_SCOPE",
                    path="items",
                )
            kit_event = kit.since_event_id
        elif kit_id is not None:
            raise StatusError("item is outside a kit", code="INVALID_SCOPE", path="items")
        if version_id not in version_pins:
            publication = ChecklistPublication.objects.select_related("checklist_version").get(
                organization_id=org, checklist_version_id=version_id
            )
            overlay = resolve_checklist(
                organization_id=org,
                vehicle_id=vehicle,
                template_id=publication.checklist_version.template_id,
                as_of=due_at,
            ).override_version_id
            version_pins[version_id] = (str(publication.pk), overlay)
        publication_id, override_id = version_pins[version_id]
        result.append(
            dict(
                item_id=item_id,
                item_def_id=str(requirement.definition_id),
                publication_id=publication_id,
                override_version_id=override_id,
                kit_instance_id=kit_id,
                kit_key=kit_id or NO_KIT,
                kit_event_id=kit_event,
            )
        )
    return result


def request_inspection(
    *,
    organization_id,
    actor_id,
    vehicle_id,
    items,
    due_at,
    explanation,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Create one obligation per explicit pair. Replays reauthorize and return the original ids."""
    for value in (organization_id, actor_id, vehicle_id):
        canonical_id(value)
    selected = _items(items)
    why = _text(explanation, "explanation")
    if not isinstance(due_at, datetime) or due_at.utcoffset() is None:
        raise StatusError(
            "due_at needs a timezone", code="MALFORMED_REQUEST", status=400, path="due_at"
        )
    due_at = due_at.astimezone(UTC)

    def prepare(actor):
        try:
            lock_unit_scope(organization_id, vehicle_id)
        except UnitUnavailable:
            raise StatusError(
                "vehicle not found or inactive", code="NOT_FOUND", status=404
            ) from None
        # Checks outside a session do not lock the vehicle. Serialize the order with their
        # item locks before assigning E, so a pre-order check can never satisfy the order.
        locked = list(
            ItemLock.objects.select_for_update()
            .filter(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                item_id__in={item for item, _kit in selected},
            )
            .order_by("item_id")
        )
        if len(locked) != len({item for item, _kit in selected}):
            raise StatusError(
                "item is not available on this vehicle", code="INVALID_SCOPE", path="items"
            )
        instant = timezone.now()
        if due_at <= instant:
            raise StatusError("due_at must be in the future", code="INVALID_DUE_AT", path="due_at")
        try:
            pins = _pins(organization_id, vehicle_id, due_at, selected)
        except ChecklistQueryError as error:
            raise StatusError(str(error), code=error.code, status=error.status) from None
        obligation_ids = [new_id() for _ in pins]

        def apply(receipt, current):
            evidence = dict(
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )
            for pin, oid in zip(pins, obligation_ids, strict=True):
                version = RequirementVersion.objects.create(
                    organization_id=organization_id, vehicle_id=vehicle_id, **pin, **evidence
                )
                Obligation.objects.create(
                    id=oid,
                    organization_id=organization_id,
                    vehicle_id=vehicle_id,
                    item_id=pin["item_id"],
                    kit_instance_id=pin["kit_instance_id"],
                    kit_key=pin["kit_key"],
                    requirement_version=version,
                    routine=False,
                    explanation=why,
                    early_at=receipt.recorded_at,
                    due_at=due_at,
                    grace_at=due_at,
                    basis_at=receipt.recorded_at,
                    **evidence,
                )
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                event_id=obligation_ids[0],
                kind="obligations.changed",
            )

        return Plan(
            recorded_at=instant,
            response_body={"code": "INSPECTION_REQUIRED", "obligation_ids": obligation_ids},
            apply=apply,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "vehicle_id": vehicle_id,
                    "items": items,
                    "due_at": due_at.isoformat(),
                    "explanation": why,
                    "obligation_ids": obligation_ids,
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=COMMAND_TYPE,
        request_digest=digest(
            {
                "vehicle_id": vehicle_id,
                "items": selected,
                "due_at": due_at.isoformat(),
                "explanation": why,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, ACTION),
        prepare=prepare,
    )
