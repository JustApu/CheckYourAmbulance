"""Audited seal event commands for cabinets and physical kits."""

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import Compartment, KitInstance, Vehicle
from cya.core.audit_commands import Plan, run_command
from cya.kits.definitions import authorizer
from cya.kits.sealable import kit_sealable
from cya.seals.models import SealEvent

MAX_EVENTS = 10000
MAX_REASON = 4000


class SealError(ValueError):
    """Seal state or request is invalid."""


class SealConflict(SealError):
    """The seal stream changed since the caller read it."""


def _subject(organization_id, vehicle_id, kit_id, slot_id, *, require_sealable):
    vehicle = (
        Vehicle.objects.select_for_update()
        .filter(organization_id=organization_id, pk=vehicle_id, active=True)
        .first()
    )
    if vehicle is None:
        raise SealError("vehicle is unavailable")
    if kit_id is not None:
        kit = (
            KitInstance.objects.select_for_update()
            .filter(organization_id=organization_id, pk=kit_id, vehicle_id=vehicle_id, active=True)
            .first()
        )
        now = timezone.now()
        if kit is None or (require_sealable and not kit_sealable(organization_id, [kit_id], now)):
            raise SealError("kit is unavailable or not designated sealable")
    else:
        now = timezone.now()
        compartment = (
            Compartment.objects.filter(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                stable_slot_id=slot_id,
                effective_at__lte=now,
            )
            .order_by("-effective_at", "-revision")
            .first()
        )
        if compartment is None or (require_sealable and not compartment.sealable):
            raise SealError("compartment is unavailable or not designated sealable")
    return now


def _events(organization_id, vehicle_id, kit_id, slot_id):
    rows = SealEvent.objects.filter(organization_id=organization_id)
    if kit_id is not None:
        rows = rows.filter(kit_id=kit_id)
    else:
        rows = rows.filter(vehicle_id=vehicle_id, compartment_slot_id=slot_id, kit__isnull=True)
    events = list(rows.order_by("sequence")[: MAX_EVENTS + 1])
    if len(events) > MAX_EVENTS:
        raise SealError("seal event history exceeds its limit")
    return events


def _event_state(events):
    seal = None
    audit_pending = False
    for event in events:
        if event.event_type == "RESEAL":
            seal = event
        elif event.event_type == "BREAK":
            seal = None
        elif event.event_type == "AUDIT_REQUEST":
            audit_pending = True
        elif event.event_type == "AUDIT_FULFILLED":
            audit_pending = False
    return seal, audit_pending


def _append_event(
    *,
    event_type,
    vehicle_id,
    kit_id=None,
    slot_id=None,
    expected_tip_id=None,
    reason=None,
    **context,
):
    organization_id = context["organization_id"]
    for value in (organization_id, context["actor_id"], vehicle_id):
        canonical_id(value)
    if (kit_id is None) == (slot_id is None):
        raise SealError("exactly one kit or compartment slot is required")
    canonical_id(kit_id if kit_id is not None else slot_id)
    if expected_tip_id is not None:
        canonical_id(expected_tip_id)
    if event_type == "BREAK":
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
            raise SealError("a break requires a reason")
        reason = reason.strip()
        command_type, action = "SEAL_BREAK", "verify_break_reseal_kit_seal"
    elif event_type == "AUDIT_REQUEST":
        command_type, action = "SEAL_AUDIT_REQUEST", "request_kit_audit"
    else:
        raise SealError("unsupported seal event")

    request = {
        "vehicle_id": vehicle_id,
        "kit_id": kit_id,
        "slot_id": slot_id,
        "expected_tip_id": expected_tip_id,
        "reason": reason,
    }

    def build(actor):
        now = _subject(
            organization_id, vehicle_id, kit_id, slot_id, require_sealable=event_type != "BREAK"
        )
        events = _events(organization_id, vehicle_id, kit_id, slot_id)
        prior = events[-1] if events else None
        if (str(prior.pk) if prior else None) != expected_tip_id:
            raise SealConflict("seal changed; reload before continuing")
        seal, pending = _event_state(events)
        if event_type == "BREAK" and seal is None:
            raise SealError("there is no intact seal to break")
        if event_type == "AUDIT_REQUEST" and pending:
            raise SealError("a full audit is already pending")
        event_id = new_id()

        def apply(receipt, current):
            SealEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                kit_id=kit_id,
                compartment_slot_id=slot_id,
                sequence=prior.sequence + 1 if prior else 1,
                prior_event=prior,
                event_type=event_type,
                marking=seal.marking if event_type == "BREAK" else None,
                reason=reason,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )

        body = {"code": "SEAL_" + event_type, "event_id": event_id}
        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=organization_id,
            recorded_at=now,
            new_snapshot={"schema_version": 1, "values": body},
        )

    return run_command(
        organization_id=organization_id,
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=authorizer(organization_id, context["actor_id"], action=action),
        prepare=build,
    )


def break_seal(*, reason, **kwargs):
    return _append_event(event_type="BREAK", reason=reason, **kwargs)


def request_seal_audit(**kwargs):
    return _append_event(event_type="AUDIT_REQUEST", **kwargs)
