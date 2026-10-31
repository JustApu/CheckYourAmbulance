"""Author corrections and administrator invalidation of immutable reading chains (ODO-5)."""

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import ResourceContext, Role
from cya.checklists.models import Vehicle
from cya.checks.commands import (
    CheckError,
    SelfInvalidationForbidden,
    _explanation,
    _id,
    _operation_id,
    _self_invalidation_forbidden,
)
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.odometer.commands import _reading, anomaly, next_reading, previous_reading, threshold_for
from cya.odometer.models import EngineHoursEntry, OdometerEntry, ReadingEvent

CORRECTION_COMMAND = "READING_CORRECTION"
INVALIDATION_COMMAND = "READING_INVALIDATION"
CORRECTION_ACTION = "correct_check"
INVALIDATION_ACTION = "invalidate_others_check"
MODELS = {"odometer": OdometerEntry, "engine-hours": EngineHoursEntry}


def _kind(kind):
    if kind not in MODELS:
        raise CheckError("MALFORMED_REQUEST", "unknown reading kind", status=400, path="kind")
    return MODELS[kind]


def _target(model, organization_id, reading_id):
    row = model.objects.filter(organization_id=organization_id, pk=reading_id).first()
    if row is None:
        raise CheckError("NOT_FOUND", "reading was not found", status=404, path="reading_id")
    return row


def _vehicle_lock(organization_id, vehicle_id):
    vehicle = (
        Vehicle.objects.select_for_update()
        .filter(organization_id=organization_id, pk=vehicle_id)
        .first()
    )
    if vehicle is None:
        raise CheckError("CONFIGURATION_UNAVAILABLE", "vehicle lock is unavailable", status=503)


def _tip(model, root):
    predecessors = model.objects.filter(root_entry=root, predecessor_entry__isnull=False).values(
        "predecessor_entry_id"
    )
    tips = list(model.objects.filter(root_entry=root).exclude(pk__in=predecessors)[:2])
    if len(tips) != 1:
        raise CheckError("CONFIGURATION_UNAVAILABLE", "reading chain is unavailable", status=503)
    return tips[0]


def _event_fields(kind, root, selected, replacement=None):
    prefix = "odometer" if kind == "odometer" else "hours"
    return {
        f"root_{prefix}": root,
        f"selected_{prefix}": selected,
        f"replacement_{prefix}": replacement,
    }


def _invalidated(kind, root):
    field = "root_odometer" if kind == "odometer" else "root_hours"
    return ReadingEvent.objects.filter(**{field: root}, event_type="INVALIDATION").exists()


def correct_reading(
    *,
    kind,
    reading_id,
    expected_tip_entry_id,
    value,
    operation_id,
    organization_id,
    actor_id,
    correlation_id,
    request_attempt_id,
    confirm_anomaly=False,
    explanation=None,
):
    """Append a correction by the original author, with a compare-and-swap chain tip."""
    for identifier in (organization_id, actor_id):
        canonical_id(identifier)
    model = _kind(kind)
    reading_id = _id(reading_id, "reading_id")
    expected_tip_entry_id = _id(expected_tip_entry_id, "expected_tip_entry_id")
    _operation_id(operation_id)
    value = _reading(value, "value")
    if type(confirm_anomaly) is not bool:
        raise CheckError(
            "MALFORMED_REQUEST",
            "confirm_anomaly must be boolean",
            status=400,
            path="confirm_anomaly",
        )
    explanation = _explanation(explanation)

    def build(actor):
        target = _target(model, organization_id, reading_id)
        _vehicle_lock(organization_id, target.vehicle_id)
        root = _target(model, organization_id, target.root_entry_id)
        if _invalidated(kind, root):
            raise CheckError("CHAIN_INVALIDATED", "the reading chain is invalidated", status=409)
        tip = _tip(model, root)
        if str(tip.pk) != expected_tip_entry_id:
            raise CheckError("CHAIN_CHANGED", "the reading chain changed", status=409)
        prior = previous_reading(model, organization_id, root.vehicle_id, before_root=root)
        if prior is not None and prior.unit != root.unit:
            raise CheckError("UNIT_MISMATCH", "vehicle reading unit changed", path="value")
        next_read = next_reading(model, organization_id, root.vehicle_id, after_root=root)
        if next_read is not None and next_read.unit != root.unit:
            raise CheckError("UNIT_MISMATCH", "vehicle reading unit changed", path="value")
        limit = threshold_for(organization_id, root.unit) if kind == "odometer" else None
        anomaly_kind = anomaly(value, prior, limit)
        downstream_anomaly = next_read is not None and value > next_read.value
        is_anomalous = bool(anomaly_kind) or downstream_anomaly
        if is_anomalous and not confirm_anomaly:
            raise CheckError(
                "ANOMALY_CONFIRMATION_REQUIRED",
                "correct or confirm this reading",
                status=409,
                path="value",
            )
        replacement_id, event_id = new_id(), new_id()

        def apply(receipt, current):
            replacement = model.objects.create(
                id=replacement_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                operation_id=operation_id,
                vehicle_id=root.vehicle_id,
                value=value,
                unit=root.unit,
                prior_entry=prior,
                root_entry=root,
                predecessor_entry=tip,
                anomaly_confirmed=is_anomalous,
                confirmation_explanation=explanation if is_anomalous else None,
            )
            ReadingEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                event_type="CORRECTION",
                explanation=explanation,
                **_event_fields(kind, root, tip, replacement),
            )
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=root.vehicle_id,
                event_id=event_id,
                kind="odometer.corrected",
            )

        return Plan(
            response_body={
                "code": "READING_CORRECTED",
                "entry_id": replacement_id,
                "root_entry_id": root.pk,
                "supersedes_entry_id": tip.pk,
                "event_id": event_id,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "kind": kind,
                    "root_entry_id": root.pk,
                    "replacement_entry_id": replacement_id,
                    "event_id": event_id,
                    "value": str(value),
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=CORRECTION_COMMAND,
        request_digest=digest(
            {
                "kind": kind,
                "reading_id": reading_id,
                "expected_tip_entry_id": expected_tip_entry_id,
                "value": str(value),
                "confirm_anomaly": confirm_anomaly,
                "explanation": explanation,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_correction_authorizer(organization_id, actor_id, model, reading_id),
        prepare=build,
    )


def _correction_authorizer(organization_id, actor_id, model, reading_id):
    backend = DatabaseRoleBackend()

    def authorize(_org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if role == Role.EMT else role for role in load_state(backend, user).grants
            )
            actor = Actor(user=user, roles=tuple(sorted(role.value for role in roles)))
            principal = Principal(
                organization_id, actor_id, roles, user.active and current_org.active
            )
            author_id = (
                model.objects.filter(organization_id=organization_id, pk=reading_id)
                .values_list("actor_user_id", flat=True)
                .first()
            )
            try:
                require_permission(
                    principal,
                    organization_id,
                    CORRECTION_ACTION,
                    context=ResourceContext(
                        author_id=str(canonical_id(author_id)) if author_id is not None else None
                    ),
                )
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def _invalidation_authorizer(organization_id, actor_id, model, reading_id):
    backend = DatabaseRoleBackend()

    def authorize(_org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if role == Role.EMT else role for role in load_state(backend, user).grants
            )
            actor = Actor(user=user, roles=tuple(sorted(role.value for role in roles)))
            principal = Principal(
                organization_id, actor_id, roles, user.active and current_org.active
            )
            author_id = (
                model.objects.filter(organization_id=organization_id, pk=reading_id)
                .values_list("actor_user_id", flat=True)
                .first()
            )
            if (
                Role.ADMINISTRATOR in roles
                and author_id is not None
                and _self_invalidation_forbidden(
                    actor_id=actor_id, author_id=str(canonical_id(author_id))
                )
            ):
                raise SelfInvalidationForbidden(actor)
            try:
                require_permission(
                    principal,
                    organization_id,
                    INVALIDATION_ACTION,
                    context=ResourceContext(author_id=str(author_id or "")),
                )
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def invalidate_reading(
    *,
    kind,
    reading_id,
    explanation,
    operation_id,
    organization_id,
    actor_id,
    correlation_id,
    request_attempt_id,
):
    """Invalidate an entire reading chain; no predecessor value is revived."""
    for identifier in (organization_id, actor_id):
        canonical_id(identifier)
    model = _kind(kind)
    reading_id = _id(reading_id, "reading_id")
    _operation_id(operation_id)
    if not isinstance(explanation, str) or not explanation.strip():
        raise CheckError(
            "EXPLANATION_REQUIRED", "invalidation needs an explanation", path="explanation"
        )
    explanation = _explanation(explanation)

    def build(actor):
        target = _target(model, organization_id, reading_id)
        _vehicle_lock(organization_id, target.vehicle_id)
        root = _target(model, organization_id, target.root_entry_id)
        if _invalidated(kind, root):
            raise CheckError(
                "CHAIN_INVALIDATED", "the reading chain is already invalidated", status=409
            )
        tip = _tip(model, root)
        event_id = new_id()
        affected = []
        if kind == "odometer":
            from cya.odometer.models import OdometerObligationLink

            affected = sorted(
                str(pk)
                for pk in OdometerObligationLink.objects.filter(entry=root).values_list(
                    "obligation_id", flat=True
                )
            )

        def apply(receipt, current):
            ReadingEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                event_type="INVALIDATION",
                explanation=explanation,
                **_event_fields(kind, root, tip),
            )
            from cya.live_updates.publish import publish_vehicle_update

            publish_vehicle_update(
                organization_id=organization_id,
                vehicle_id=root.vehicle_id,
                event_id=event_id,
                kind="odometer.invalidated",
            )

        return Plan(
            response_body={
                "code": "READING_CHAIN_INVALIDATED",
                "root_entry_id": root.pk,
                "invalidation_event_id": event_id,
                "affected_obligation_ids": affected,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "kind": kind,
                    "root_entry_id": root.pk,
                    "selected_entry_id": tip.pk,
                    "event_id": event_id,
                    "affected_obligation_ids": affected,
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=INVALIDATION_COMMAND,
        request_digest=digest({"kind": kind, "reading_id": reading_id, "explanation": explanation}),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_invalidation_authorizer(organization_id, actor_id, model, reading_id),
        prepare=build,
    )
