"""Audited kit assignment commands (SPEC 4.2, TIME-1a; OQ-54, issue 92 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/92)).

add_kit_instance and record_kit_assignment are the server boundary for adding, moving and
deactivating a kit instance. Each runs through cya.core.audit_commands.run_command (one
outermost transaction, an operation-id receipt that replays the original result, a success or
failure audit); do not wrap them in a transaction.

Permission is the policy action manage_kit_assignment: Supervisors, Supply Officers and
Administrators, not Crew (SPEC 3.3). An assignment takes effect at the instant it is recorded
and is never backdated (migration 0002 refuses otherwise). The command appends the event and
updates kit_instance's vehicle, compartment and active flag in the same transaction, so that
projection and history cannot disagree.

Locking: the vehicles involved (the one the kit leaves and the one it joins) are locked first,
in id order, then the kit row, then its latest event. Obligation generation locks the vehicle
row first as well, so a unit and a kit change on one vehicle serialize. After the command
commits, those vehicles' upcoming obligations are reconciled; a failure there is logged and
left to the next generator run, as for requirement bindings.
"""

import logging

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.checklists.models import Compartment, KitInstance, Vehicle
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.kits.models import KitAssignmentEvent
from cya.obligations.generation import reconcile_vehicle

logger = logging.getLogger(__name__)

ACTION = "manage_kit_assignment"
ADD_COMMAND = "KIT_ADD"
ASSIGN_COMMAND = "KIT_ASSIGNMENT_RECORD"
MAX_LABEL = 200


class KitError(ValueError):
    """A rule was violated; the command rolls back and is audited as failed."""


def _authorize(organization_id, actor_id):
    backend = DatabaseRoleBackend()

    def authorize(_org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if r == Role.EMT else r for r in load_state(backend, user).grants
            )
            actor = Actor(user=user, roles=tuple(sorted(r.value for r in roles)))
            principal = Principal(
                organization_id, actor_id, roles, user.active and current_org.active
            )
            try:
                require_permission(principal, organization_id, ACTION)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def _lock_vehicles(organization_id, vehicle_ids):
    """Lock the named vehicles in id order; every one must exist, be active and be ours."""
    wanted = sorted({v for v in vehicle_ids if v is not None})
    found = list(
        Vehicle.objects.select_for_update()
        .filter(organization_id=organization_id, pk__in=wanted)
        .order_by("pk")
    )
    if len(found) != len(wanted):
        raise KitError("vehicle was not found in the organization")
    return {str(v.pk): v for v in found}


def _placement(organization_id, vehicle, compartment_id):
    compartment = Compartment.objects.filter(
        organization_id=organization_id, pk=compartment_id, vehicle_id=vehicle.pk
    ).first()
    if compartment is None:
        raise KitError("compartment is not a compartment of the vehicle")
    if not vehicle.active:
        raise KitError("vehicle is inactive")
    return compartment


def _snapshot(kit_id, event_id, sequence, vehicle_id, compartment_id, active):
    return {
        "schema_version": 1,
        "values": {
            "kit_id": kit_id,
            "event_id": event_id,
            "sequence": sequence,
            "vehicle_id": vehicle_id,
            "compartment_id": compartment_id,
            "active": active,
        },
    }


def _append(receipt, actor, *, event_id, org, kit, prior, vehicle_id, compartment_id, active):
    KitAssignmentEvent.objects.create(
        id=event_id,
        organization_id=org,
        kit=kit,
        sequence=1 if prior is None else prior.sequence + 1,
        prior_event=prior,
        vehicle_id=vehicle_id,
        compartment_id=compartment_id,
        active=active,
        effective_at=receipt.recorded_at,
        command=receipt,
        recorded_at=receipt.recorded_at,
        actor_user=actor.user,
        actor_roles_at_time=list(actor.roles),
    )
    KitInstance.objects.filter(pk=kit.pk).update(
        vehicle_id=vehicle_id, compartment_id=compartment_id, active=active
    )


def _reconcile_after(organization_id, vehicle_ids):
    for vehicle_id in sorted({v for v in vehicle_ids if v is not None}):
        summary = reconcile_vehicle(organization_id, vehicle_id)
        for failure in summary.failures:
            logger.error(
                "obligation reconciliation failed after kit change: vehicle %s due %s: %s",
                failure.vehicle_id,
                failure.due_at.isoformat(),
                failure.error,
            )


def _execute(context, command_type, request, build, vehicles_of):
    result = run_command(
        organization_id=context["organization_id"],
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(context["organization_id"], context["actor_id"]),
        prepare=build,
    )
    if not result.replayed:
        _reconcile_after(context["organization_id"], vehicles_of(result.body))
    return result


def add_kit_instance(*, label, vehicle_id, compartment_id, **context):
    """Add a new kit instance in a compartment of a vehicle, active from now."""
    org = context["organization_id"]
    canonical_id(vehicle_id)
    canonical_id(compartment_id)
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL:
        raise KitError(f"label must be nonblank text of at most {MAX_LABEL} characters")

    def build(actor):
        vehicle = _lock_vehicles(org, [vehicle_id])[vehicle_id]
        _placement(org, vehicle, compartment_id)
        kit_id, event_id = new_id(), new_id()

        def apply(receipt, current):
            kit = KitInstance.objects.create(
                id=kit_id,
                organization_id=org,
                label=label,
                active=True,
                vehicle_id=vehicle_id,
                compartment_id=compartment_id,
            )
            _append(
                receipt,
                current,
                event_id=event_id,
                org=org,
                kit=kit,
                prior=None,
                vehicle_id=vehicle_id,
                compartment_id=compartment_id,
                active=True,
            )

        body = {
            "code": "KIT_ADDED",
            "kit_id": kit_id,
            "event_id": event_id,
            "sequence": 1,
            "vehicle_ids": [vehicle_id],
        }
        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            new_snapshot=_snapshot(kit_id, event_id, 1, vehicle_id, compartment_id, True),
        )

    request = {"label": label, "vehicle_id": vehicle_id, "compartment_id": compartment_id}
    return _execute(context, ADD_COMMAND, request, build, lambda body: body["vehicle_ids"])


def record_kit_assignment(*, kit_id, active, vehicle_id=None, compartment_id=None, **context):
    """Move a kit instance to a compartment of a vehicle (active), or deactivate it (not on any).

    Deactivating names no vehicle or compartment. Re-activating or moving names both. A move
    to another vehicle is a removal from the first and an addition to the second (TIME-1a).
    """
    org = context["organization_id"]
    canonical_id(kit_id)
    if type(active) is not bool:
        raise KitError("active must be true or false")
    if active:
        canonical_id(vehicle_id)
        canonical_id(compartment_id)
    elif vehicle_id is not None or compartment_id is not None:
        raise KitError("a deactivated kit is on no vehicle or compartment")

    def build(actor):
        kit = KitInstance.objects.filter(organization_id=org, pk=kit_id).first()
        if kit is None:
            raise KitError("kit instance was not found in the organization")
        previous = (
            KitAssignmentEvent.objects.filter(organization_id=org, kit_id=kit_id)
            .order_by("-sequence")
            .first()
        )
        if previous is None:
            raise KitError("kit instance has no assignment history; add it with add_kit_instance")
        involved = [str(previous.vehicle_id) if previous.vehicle_id else None, vehicle_id]
        vehicles = _lock_vehicles(org, involved)
        KitInstance.objects.select_for_update().get(pk=kit_id)
        prior = (
            KitAssignmentEvent.objects.select_for_update()
            .filter(organization_id=org, kit_id=kit_id)
            .order_by("-sequence")
            .first()
        )
        if prior.pk != previous.pk:
            raise KitError("kit instance changed while locking; retry")
        same = (prior.active, str(prior.vehicle_id or ""), str(prior.compartment_id or "")) == (
            active,
            vehicle_id or "",
            compartment_id or "",
        )
        if same:
            raise KitError("kit instance is already in that state")
        if active:
            _placement(org, vehicles[vehicle_id], compartment_id)
        event_id = new_id()
        sequence = prior.sequence + 1

        def apply(receipt, current):
            _append(
                receipt,
                current,
                event_id=event_id,
                org=org,
                kit=kit,
                prior=prior,
                vehicle_id=vehicle_id,
                compartment_id=compartment_id,
                active=active,
            )

        affected = sorted({v for v in involved if v is not None})
        body = {
            "code": "KIT_ASSIGNMENT_RECORDED",
            "kit_id": kit_id,
            "event_id": event_id,
            "sequence": sequence,
            "vehicle_ids": affected,
        }
        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            prior_snapshot=_snapshot(
                kit_id,
                str(prior.pk),
                prior.sequence,
                str(prior.vehicle_id) if prior.vehicle_id else None,
                str(prior.compartment_id) if prior.compartment_id else None,
                prior.active,
            ),
            new_snapshot=_snapshot(kit_id, event_id, sequence, vehicle_id, compartment_id, active),
        )

    request = {
        "kit_id": kit_id,
        "active": active,
        "vehicle_id": vehicle_id,
        "compartment_id": compartment_id,
    }
    return _execute(context, ASSIGN_COMMAND, request, build, lambda body: body["vehicle_ids"])
