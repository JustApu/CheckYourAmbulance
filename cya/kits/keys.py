"""Audited commands that set stable keys once (SPEC 2.1.0 IMP-6, issue #129).

A vehicle's key is its VIN, a checklist template and every compartment or vehicle-system position
have a key, and each is set once and never changed: a remount is a new vehicle. Vehicle files
(IMP-7) match records by these keys. Records created before 2.1.0 get theirs through these
commands; a vehicle file sets them for what it creates.

All commands need the policy action set_configuration_keys (Administrators, SPEC 3.3) and run
through cya.core.audit_commands.run_command; do not wrap them in a transaction. Each takes the
organization lock first, as the uniqueness rules span the organization, so two concurrent
settings cannot both pass.
"""

import re

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import (
    KEY_PATTERN,
    MAX_KEY,
    VIN_PATTERN,
    ChecklistTemplate,
    Compartment,
    Vehicle,
    VehicleSystem,
)
from cya.core.audit_commands import Plan, run_command
from cya.kits import definitions
from cya.kits.models import POSITION_KINDS, PositionKey

ACTION = "set_configuration_keys"
VIN_COMMAND = "VEHICLE_VIN_SET"
TEMPLATE_COMMAND = "TEMPLATE_KEY_SET"
POSITION_COMMAND = "POSITION_KEY_SET"
SLOT_MODELS = {"COMPARTMENT": Compartment, "SYSTEM": VehicleSystem}


class KeyRuleError(ValueError):
    """A rule was violated; the command rolls back and is audited as failed."""


def check_vin(value):
    """A VIN in its stored form: trimmed, uppercase, 17 characters without I, O or Q."""
    if not isinstance(value, str):
        raise KeyRuleError("vin must be a string")
    vin = value.strip().upper()
    if not re.fullmatch(VIN_PATTERN, vin):
        raise KeyRuleError("vin must be 17 letters and digits, without I, O or Q")
    return vin


def check_key(value, what="key"):
    """A position or template key: the kit definition key format (IMP-1, IMP-6)."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_KEY:
        raise KeyRuleError(f"{what} must be 1 to {MAX_KEY} characters")
    if not re.fullmatch(KEY_PATTERN, value):
        raise KeyRuleError(
            f"{what} must start with a lowercase letter or digit, then use a-z 0-9 . _ -"
        )
    return value


def _run(context, command_type, request, build, *, lock_organization=False):
    canonical_id(context["actor_id"])
    return run_command(
        organization_id=context["organization_id"],
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=definitions.authorizer(
            context["organization_id"],
            context["actor_id"],
            ACTION,
            lock_organization=lock_organization,
        ),
        prepare=build,
    )


def _plan(organization_id, body, apply):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=organization_id,
        new_snapshot={"schema_version": 1, "values": body},
    )


def set_vehicle_vin(*, vehicle_id, vin, **context):
    """Set a vehicle's VIN once (IMP-6); a VIN is never changed."""
    canonical_id(vehicle_id)
    org = context["organization_id"]
    vin = check_vin(vin)

    def build(actor):
        vehicle = (
            Vehicle.objects.select_for_update().filter(organization_id=org, pk=vehicle_id).first()
        )
        if vehicle is None:
            raise KeyRuleError(f"vehicle {vehicle_id} does not exist in the organization")
        if vehicle.key is not None:
            raise KeyRuleError("the vehicle's VIN is already set and never changes")
        if Vehicle.objects.filter(organization_id=org, key=vin).exists():
            raise KeyRuleError("another vehicle has that VIN")

        def apply(receipt, _current):
            Vehicle.objects.filter(pk=vehicle.pk).update(key=vin)

        return _plan(org, {"code": "VEHICLE_VIN_SET", "vehicle_id": vehicle_id, "vin": vin}, apply)

    return _run(
        context, VIN_COMMAND, {"vehicle_id": vehicle_id, "vin": vin}, build, lock_organization=True
    )


def set_template_key(*, template_id, key, **context):
    """Set a checklist template's key once (IMP-6)."""
    canonical_id(template_id)
    org = context["organization_id"]
    key = check_key(key, "template key")

    def build(actor):
        template = (
            ChecklistTemplate.objects.select_for_update()
            .filter(organization_id=org, pk=template_id)
            .first()
        )
        if template is None:
            raise KeyRuleError(f"template {template_id} does not exist in the organization")
        if template.vehicle_id is not None:
            raise KeyRuleError("a vehicle checklist has no key (SPEC 5.0.0 IMP-6)")
        from cya.checklists.ownership import require_legacy_writable

        require_legacy_writable(org, KeyRuleError)
        if template.key is not None:
            raise KeyRuleError("the template's key is already set and never changes")
        if ChecklistTemplate.objects.filter(organization_id=org, key=key).exists():
            raise KeyRuleError("another template has that key")

        def apply(receipt, _current):
            ChecklistTemplate.objects.filter(pk=template.pk).update(key=key)

        body = {"code": "TEMPLATE_KEY_SET", "template_id": template_id, "key": key}
        return _plan(org, body, apply)

    return _run(
        context,
        TEMPLATE_COMMAND,
        {"template_id": template_id, "key": key},
        build,
        lock_organization=True,
    )


def set_position_key(*, kind, stable_slot_id, key, **context):
    """Set the organization-wide key of a compartment or system position once (IMP-6)."""
    if kind not in POSITION_KINDS:
        raise KeyRuleError(f"kind must be one of {list(POSITION_KINDS)}")
    canonical_id(stable_slot_id)
    org = context["organization_id"]
    key = check_key(key, "position key")

    def build(actor):
        if (
            not SLOT_MODELS[kind]
            .objects.filter(organization_id=org, stable_slot_id=stable_slot_id)
            .exists()
        ):
            raise KeyRuleError("no compartment or system has that position in the organization")
        existing = PositionKey.objects.filter(organization_id=org, kind=kind)
        if existing.filter(stable_slot_id=stable_slot_id).exists():
            raise KeyRuleError("the position's key is already set and never changes")
        if existing.filter(key=key).exists():
            raise KeyRuleError("another position of that kind has that key")
        key_id = new_id()

        def apply(receipt, current):
            PositionKey.objects.create(
                id=key_id,
                organization_id=org,
                kind=kind,
                stable_slot_id=stable_slot_id,
                key=key,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )

        body = {
            "code": "POSITION_KEY_SET",
            "kind": kind,
            "stable_slot_id": stable_slot_id,
            "key": key,
        }
        return _plan(org, body, apply)

    request = {"kind": kind, "stable_slot_id": stable_slot_id, "key": key}
    # Both uniqueness rules span positions, so settings serialize on the organization lock,
    # taken first, before the actor's (cya.authorization.locking).
    return _run(context, POSITION_COMMAND, request, build, lock_organization=True)


def position_keys(organization_id, kind):
    """{stable slot id: key} of the organization's positions of one kind."""
    return {
        str(slot): key
        for slot, key in PositionKey.objects.filter(
            organization_id=organization_id, kind=kind
        ).values_list("stable_slot_id", "key")
    }
