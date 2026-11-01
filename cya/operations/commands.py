"""Audited operational-status commands (SPEC section 9, OPS-1, OPS-3, OPS-4, OPS-6).

Each function is the server boundary for a future UI handler. It takes IDs and intent,
never client role snapshots, and runs through cya.core.audit_commands.run_command, which owns
the outermost transaction, the operation-id receipt (replays return the original result)
and the success/failure audit. Do not wrap them in a transaction.

Permission is the policy action set_vehicle_status: crew may only request (intent
"request"); supervisors and administrators may also set status and decide requests. Status is
manual only (OPS-1, OPS-2): nothing here reacts to deficiencies or checks. Each command
schedules an identifier-only live hint inside its transaction (status.changed for a new status
event, status.request for a request or decision row); it is delivered only after commit.

The vehicle row is locked FOR UPDATE in prepare, so concurrent writers on one vehicle are
serialized; the next sequence number and the prior event are then read with a locking read.
recorded_at is assigned after prepare, so it never runs backwards against the sequence.
"""

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import ResourceContext, Role
from cya.checklists.models import Vehicle
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.notifications.recipients import notify
from cya.operations.models import MAX_TEXT, STATUSES, StatusRequest, VehicleStatusEvent

ACTION = "set_vehicle_status"
DECISIONS = ("APPROVE", "DENY")


class StatusError(ValueError):
    """A status rule was violated; the command rolls back and is audited as failed.

    code is the stable wire code, status the HTTP status, path a safe schema path.
    """

    def __init__(self, message, *, code="INVALID_STATUS_CHANGE", status=422, path=None):
        super().__init__(message)
        self.code, self.status, self.path = code, status, path


def _hint(org, vehicle_id, record_id, kind):
    """Identifier-only live hint, delivered only if the command commits (docs/live-updates.md)."""
    from cya.live_updates.publish import publish_vehicle_update

    publish_vehicle_update(
        organization_id=org, vehicle_id=vehicle_id, event_id=record_id, kind=kind
    )


def _snapshot(**values):
    return {"schema_version": 1, "values": values}


def _text(value, field, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise StatusError(
            f"{field} must be nonblank text of at most {MAX_TEXT} characters",
            code="MALFORMED_REQUEST",
            status=400,
            path=field,
        )
    return value


def _status(value, field="status"):
    if value not in STATUSES:
        raise StatusError(
            f"{field} must be one of {list(STATUSES)}",
            code="MALFORMED_REQUEST",
            status=400,
            path=field,
        )
    return value


def _restrictions(status, restrictions):
    """Limited service needs free-text restrictions (OPS-3); other statuses carry none."""
    if status == "LIMITED_SERVICE":
        return _text(restrictions, "restrictions")
    if restrictions is not None:
        raise StatusError(
            "restrictions are only valid for LIMITED_SERVICE",
            code="MALFORMED_REQUEST",
            status=400,
            path="restrictions",
        )
    return None


def _locked_vehicle(organization_id, vehicle_id):
    try:
        vehicle = Vehicle.objects.select_for_update().get(
            organization_id=organization_id, pk=vehicle_id
        )
    except Vehicle.DoesNotExist:
        raise StatusError(
            f"vehicle {vehicle_id} does not exist in the organization",
            code="NOT_FOUND",
            status=404,
        ) from None
    if not vehicle.active:
        raise StatusError("vehicle is inactive", code="VEHICLE_INACTIVE", status=409)
    return vehicle


def _latest(organization_id, vehicle_id):
    """Locking read of the newest event; the caller already holds the vehicle row lock."""
    return (
        VehicleStatusEvent.objects.select_for_update()
        .filter(organization_id=organization_id, vehicle_id=vehicle_id)
        .order_by("-sequence")
        .first()
    )


def _require_change(latest, status, restrictions):
    if latest is not None and (latest.status, latest.restrictions) == (status, restrictions):
        raise StatusError(
            f"vehicle is already {status} with these restrictions", code="NO_CHANGE", status=409
        )


def _execute(
    *,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    intent,
    command_type,
    request,
    build,
):
    for value in (organization_id, actor_id):
        canonical_id(value)
    backend = DatabaseRoleBackend()

    def authorize(org):
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
                require_permission(
                    principal, organization_id, ACTION, context=ResourceContext(intent=intent)
                )
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=build,
    )


def _evidence(receipt, actor):
    return {
        "command": receipt,
        "recorded_at": receipt.recorded_at,
        "actor_user": actor.user,
        "actor_roles_at_time": list(actor.roles),
    }


def _status_link(vehicle_id):
    return f"/status/vehicles/{vehicle_id}/"


def _append_event(receipt, actor, *, event_id, org, vehicle_id, latest, status, why, restrictions):
    event = VehicleStatusEvent.objects.create(
        id=event_id,
        organization_id=org,
        vehicle_id=vehicle_id,
        sequence=1 if latest is None else latest.sequence + 1,
        prior_event=latest,
        status=status,
        explanation=why,
        restrictions=restrictions,
        **_evidence(receipt, actor),
    )
    # SPEC 9 OPS-1, NOT-3: a status change notifies (issue 14); set directly or by approval.
    notify(
        receipt,
        event_type="vehicle.status_changed",
        source_event_type="vehicle_status_event",
        source_event_id=event_id,
        deep_link=_status_link(vehicle_id),
        payload={"vehicle_id": str(vehicle_id), "status": status},
        actor_user_id=receipt.actor_user_id,
    )
    return event


def _event_snapshots(latest, vehicle_id, status, restrictions, sequence):
    prior = (
        _snapshot(
            vehicle_id=vehicle_id,
            status=latest.status,
            restrictions=latest.restrictions,
            sequence=latest.sequence,
        )
        if latest
        else None
    )
    new = _snapshot(
        vehicle_id=vehicle_id, status=status, restrictions=restrictions, sequence=sequence
    )
    return prior, new


def _plan(org, body, apply, prior, new):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=org,
        prior_snapshot=prior,
        new_snapshot=new,
    )


# --- commands ------------------------------------------------------------------------------


def set_vehicle_status(*, vehicle_id, status, explanation, restrictions=None, **context):
    """Supervisor/administrator sets status manually with a required explanation (OPS-1)."""
    canonical_id(vehicle_id)
    org = context["organization_id"]

    def build(actor):
        _locked_vehicle(org, vehicle_id)
        new_status, why = _status(status), _text(explanation, "explanation")
        limits = _restrictions(new_status, restrictions)
        latest = _latest(org, vehicle_id)
        _require_change(latest, new_status, limits)
        event_id = new_id()
        sequence = 1 if latest is None else latest.sequence + 1

        def apply(receipt, current):
            _append_event(
                receipt,
                current,
                event_id=event_id,
                org=org,
                vehicle_id=vehicle_id,
                latest=latest,
                status=new_status,
                why=why,
                restrictions=limits,
            )
            _hint(org, vehicle_id, event_id, "status.changed")

        prior, new = _event_snapshots(latest, vehicle_id, new_status, limits, sequence)
        body = {"code": "STATUS_SET", "event_id": event_id, "sequence": sequence}
        return _plan(org, body, apply, prior, new)

    return _execute(
        intent="mutate",
        command_type="VEHICLE_STATUS_SET",
        request={
            "vehicle_id": vehicle_id,
            "status": status,
            "explanation": explanation,
            "restrictions": restrictions,
        },
        build=build,
        **context,
    )


def request_status_change(*, vehicle_id, requested_status, reason, **context):
    """Any role the policy lets request (crew included) asks for a status change (OPS-1, OPS-4)."""
    canonical_id(vehicle_id)
    org = context["organization_id"]

    def build(actor):
        _locked_vehicle(org, vehicle_id)
        target, why = _status(requested_status, "requested_status"), _text(reason, "reason")
        latest = _latest(org, vehicle_id)
        # A limited-service request may still reshape the restrictions, so only others are no-ops.
        if latest is not None and latest.status == target and target != "LIMITED_SERVICE":
            raise StatusError(f"vehicle is already {target}", code="NO_CHANGE", status=409)
        request_id = new_id()

        def apply(receipt, current):
            StatusRequest.objects.create(
                id=request_id,
                organization_id=org,
                vehicle_id=vehicle_id,
                requested_status=target,
                reason=why,
                action="REQUEST",
                **_evidence(receipt, current),
            )
            notify(
                receipt,
                event_type="vehicle.status_change_requested",
                source_event_type="status_request",
                source_event_id=request_id,
                deep_link=_status_link(vehicle_id),
                payload={"vehicle_id": str(vehicle_id), "requested_status": target},
                actor_user_id=receipt.actor_user_id,
            )
            _hint(org, vehicle_id, request_id, "status.request")

        new = _snapshot(vehicle_id=vehicle_id, requested_status=target, request_id=request_id)
        return _plan(org, {"code": "REQUESTED", "request_id": request_id}, apply, None, new)

    return _execute(
        intent="request",
        command_type="STATUS_REQUEST",
        request={"vehicle_id": vehicle_id, "requested_status": requested_status, "reason": reason},
        build=build,
        **context,
    )


def decide_status_request(*, request_id, decision, explanation, restrictions=None, **context):
    """Supervisor/administrator approves (sets the requested status) or denies (explanation)."""
    canonical_id(request_id)
    org = context["organization_id"]

    def build(actor):
        try:
            asked = StatusRequest.objects.get(organization_id=org, pk=request_id, action="REQUEST")
        except StatusRequest.DoesNotExist:
            raise StatusError(
                f"status request {request_id} does not exist", code="NOT_FOUND", status=404
            ) from None
        vehicle_id = asked.vehicle_id
        _locked_vehicle(org, vehicle_id)
        if decision not in DECISIONS:
            raise StatusError(
                f"decision must be one of {list(DECISIONS)}",
                code="MALFORMED_REQUEST",
                status=400,
                path="decision",
            )
        if StatusRequest.objects.select_for_update().filter(request=asked).exists():
            raise StatusError(
                "status request is already decided", code="ALREADY_DECIDED", status=409
            )
        why = _text(explanation, "explanation")
        decision_id = new_id()
        latest = _latest(org, vehicle_id)
        event_id, sequence, limits = None, None, None
        if decision == "APPROVE":
            limits = _restrictions(asked.requested_status, restrictions)
            _require_change(latest, asked.requested_status, limits)
            event_id, sequence = new_id(), 1 if latest is None else latest.sequence + 1
        elif restrictions is not None:
            raise StatusError(
                "restrictions are only valid when approving",
                code="MALFORMED_REQUEST",
                status=400,
                path="restrictions",
            )

        def apply(receipt, current):
            if decision == "APPROVE":
                _append_event(
                    receipt,
                    current,
                    event_id=event_id,
                    org=org,
                    vehicle_id=vehicle_id,
                    latest=latest,
                    status=asked.requested_status,
                    why=why,
                    restrictions=limits,
                )
            StatusRequest.objects.create(
                id=decision_id,
                organization_id=org,
                vehicle_id=vehicle_id,
                requested_status=asked.requested_status,
                reason=asked.reason,
                request=asked,
                action=decision,
                decision_explanation=why,
                **_evidence(receipt, current),
            )
            notify(
                receipt,
                event_type="vehicle.status_request_decided",
                source_event_type="status_request",
                source_event_id=decision_id,
                deep_link=_status_link(vehicle_id),
                payload={
                    "vehicle_id": str(vehicle_id),
                    "requested_status": asked.requested_status,
                    "decision": decision,
                },
                actor_user_id=receipt.actor_user_id,
                requester_id=asked.actor_user_id,
            )
            if decision == "APPROVE":
                _hint(org, vehicle_id, event_id, "status.changed")
            _hint(org, vehicle_id, decision_id, "status.request")

        prior, new = (
            _event_snapshots(latest, vehicle_id, asked.requested_status, limits, sequence)
            if decision == "APPROVE"
            else (None, _snapshot(decision="DENY"))
        )
        new["values"].update(vehicle_id=vehicle_id, request_id=request_id, decision_id=decision_id)
        body = {"code": f"REQUEST_{decision}", "decision_id": decision_id, "event_id": event_id}
        return _plan(org, body, apply, prior, new)

    return _execute(
        intent="mutate",
        command_type="STATUS_REQUEST_DECIDE",
        request={
            "request_id": request_id,
            "decision": decision,
            "explanation": explanation,
            "restrictions": restrictions,
        },
        build=build,
        **context,
    )
