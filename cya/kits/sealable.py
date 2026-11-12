"""Sealable designations for compartments and kit instances (SPEC 10.1 SEAL-1, 10.2 CAB-1).

Two audited commands, both under the policy action designate_sealable (Supervisors and
Administrators; SPEC 3.3):

- designate_compartment_sealable appends a new revision of a vehicle's compartment slot with
  the requested sealable value, effective now or at a later instant, never earlier than the
  slot's latest revision. Compartment rows are immutable (checklists migration 0002), so the
  old value stays in history.
- designate_kit_sealable appends a kit_seal_designation row, effective when recorded.

Each runs through cya.core.audit_commands.run_command (one transaction, an operation-id
receipt that replays the original result, a success or failure audit); do not wrap them in a
transaction. Designation changes nothing about obligations: substituting a seal for checking
contents is a later slice of issue #131, built with the sealed-kit workflow (OQ-66).
"""

from datetime import datetime

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.checklists.models import Compartment, KitInstance, Vehicle
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.kits.models import KitSealDesignation

ACTION = "designate_sealable"
COMPARTMENT_COMMAND = "COMPARTMENT_SEALABLE_DESIGNATE"
KIT_COMMAND = "KIT_SEAL_DESIGNATE"
LOOKUP_LIMIT = 2000  # kit ids answered by one kit_sealable call


class SealableError(ValueError):
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


def _run(context, command_type, request, build):
    canonical_id(context["actor_id"])
    return run_command(
        organization_id=context["organization_id"],
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(context["organization_id"], context["actor_id"]),
        prepare=build,
    )


def _plan(organization_id, body, apply, prior, new):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=organization_id,
        prior_snapshot=None if prior is None else {"schema_version": 1, "values": prior},
        new_snapshot={"schema_version": 1, "values": new},
    )


def designate_compartment_sealable(
    *, vehicle_id, compartment_id, sealable, effective_at=None, **context
):
    """Append a revision of a compartment slot that is (or is not) sealable.

    compartment_id names any revision row of the slot on that vehicle. effective_at None takes
    effect when recorded; a later instant schedules the change. A value equal to the slot's
    latest revision is refused, as is an instant earlier than that revision or than now.
    """
    org = context["organization_id"]
    canonical_id(vehicle_id)
    canonical_id(compartment_id)
    if type(sealable) is not bool:
        raise SealableError("sealable must be true or false")
    if effective_at is not None and (
        not isinstance(effective_at, datetime) or effective_at.utcoffset() is None
    ):
        raise SealableError("effective_at must be a timezone-aware datetime")

    def build(actor):
        vehicle = (
            Vehicle.objects.select_for_update().filter(organization_id=org, pk=vehicle_id).first()
        )
        if vehicle is None or not vehicle.active:
            raise SealableError("vehicle was not found in the organization or is inactive")
        named = Compartment.objects.filter(
            organization_id=org, pk=compartment_id, vehicle_id=vehicle_id
        ).first()
        if named is None:
            raise SealableError("compartment is not a compartment of the vehicle")
        tail = (
            Compartment.objects.filter(
                organization_id=org, vehicle_id=vehicle_id, stable_slot_id=named.stable_slot_id
            )
            .order_by("-revision")
            .first()
        )
        if tail.sealable is sealable:
            raise SealableError("the compartment's latest revision already has that designation")
        row_id = new_id()
        revision = tail.revision + 1

        def apply(receipt, _current):
            starts = effective_at or receipt.recorded_at
            if starts < receipt.recorded_at:
                raise SealableError("a designation cannot take effect before it is recorded")
            if starts < tail.effective_at:
                raise SealableError("a later revision of this compartment is already scheduled")
            Compartment.objects.create(
                id=row_id,
                organization_id=org,
                vehicle_id=vehicle_id,
                stable_slot_id=tail.stable_slot_id,
                revision=revision,
                label=tail.label,
                effective_at=starts,
                sealable=sealable,
            )

        values = {
            "vehicle_id": vehicle_id,
            "stable_slot_id": str(tail.stable_slot_id),
            "compartment_id": row_id,
            "revision": revision,
            "sealable": sealable,
        }
        body = {"code": "COMPARTMENT_SEALABLE_DESIGNATED", **values}
        prior = {**values, "compartment_id": str(tail.pk), "revision": tail.revision}
        prior["sealable"] = tail.sealable
        return _plan(org, body, apply, prior, values)

    request = {
        "vehicle_id": vehicle_id,
        "compartment_id": compartment_id,
        "sealable": sealable,
        "effective_at": effective_at.isoformat() if effective_at else None,
    }
    return _run(context, COMPARTMENT_COMMAND, request, build)


def designate_kit_sealable(*, kit_id, sealable, **context):
    """Record that a kit instance is (or is not) sealed, effective now."""
    org = context["organization_id"]
    canonical_id(kit_id)
    if type(sealable) is not bool:
        raise SealableError("sealable must be true or false")

    def build(actor):
        kit = KitInstance.objects.select_for_update().filter(organization_id=org, pk=kit_id).first()
        if kit is None:
            raise SealableError("kit instance was not found in the organization")
        prior = (
            KitSealDesignation.objects.select_for_update()
            .filter(organization_id=org, kit_id=kit_id)
            .order_by("-sequence")
            .first()
        )
        currently = prior.sealable if prior else False
        if currently is sealable:
            raise SealableError("the kit instance already has that designation")
        row_id = new_id()
        sequence = prior.sequence + 1 if prior else 1

        def apply(receipt, current):
            KitSealDesignation.objects.create(
                id=row_id,
                organization_id=org,
                kit=kit,
                sequence=sequence,
                prior_designation=prior,
                sealable=sealable,
                effective_at=receipt.recorded_at,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )

        values = {"kit_id": kit_id, "designation_id": row_id, "sequence": sequence}
        body = {"code": "KIT_SEAL_DESIGNATED", **values, "sealable": sealable}
        prior_values = None
        if prior is not None:
            prior_values = {
                "kit_id": kit_id,
                "designation_id": str(prior.pk),
                "sequence": prior.sequence,
                "sealable": prior.sealable,
            }
        return _plan(org, body, apply, prior_values, {**values, "sealable": sealable})

    request = {"kit_id": kit_id, "sealable": sealable}
    return _run(context, KIT_COMMAND, request, build)


def kit_sealable(organization_id, kit_ids, at):
    """{kit id: True} for each named kit designated sealed at instant at; others are absent.

    Reads only kit_seal_designation, so a past answer cannot change.
    """
    ids = sorted({str(k) for k in kit_ids})
    if len(ids) > LOOKUP_LIMIT:
        raise ValueError(f"at most {LOOKUP_LIMIT} kit instances per lookup")
    if not ids:
        return {}
    latest = {}
    for row in (
        KitSealDesignation.objects.filter(
            organization_id=organization_id, kit_id__in=ids, recorded_at__lte=at
        )
        .order_by("kit_id", "-sequence")
        .values("kit_id", "sealable")
    ):
        latest.setdefault(str(row["kit_id"]), row["sealable"])
    return {kit_id: True for kit_id, sealed in latest.items() if sealed}
