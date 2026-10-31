"""Audited commands for supervisor completion of Unable-to-Inspect obligations (issue 82 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/82))."""

import hashlib
import json
from uuid import UUID

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import load_state
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend
from cya.authorization.policy import Role, permitted, unable_self_completion_allowed
from cya.checks.models import (
    CheckEvent,
    CheckObligationLink,
    CheckRecord,
    DeterminationCheck,
    ItemLock,
)
from cya.checks.state import CheckDecision, CheckSnapshot, effective_checks
from cya.conditions.evaluator import latest_obligation_determination
from cya.conditions.models import ObligationDetermination
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.obligations.models import Obligation
from cya.obligations.queries import effective_selection

MAX_CHAIN_ROWS = 1000


class UnableCompletionError(ValueError):
    def __init__(self, code, path=None):
        super().__init__(code)
        self.code, self.path = code, path


class UnableOperationReused(RuntimeError):
    """The actor reused a command ID for a different completion request."""


def _uuid(value, field):
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise UnableCompletionError("INVALID_REQUEST", field) from None
    if str(parsed) != value:
        raise UnableCompletionError("INVALID_REQUEST", field)
    return value


def _current_roles(org_id, user_id):
    with locked_identities(org_id, [user_id], lock_organization=False) as (_, users):
        user = users[user_id]
        if not user.active:
            raise CommandDenied(Actor(user=user))
        state = load_state(DatabaseRoleBackend(), user)
        roles = frozenset(Role.CREW if r == Role.EMT else r for r in state.grants)
        actor = Actor(user=user, roles=tuple(sorted(r.value for r in roles)))
        return user, roles, actor


def _item_lock(org_id, check):
    # Every check writer locks this pre-provisioned row; absence means configuration is invalid.
    lock = (
        ItemLock.objects.select_for_update()
        .filter(organization_id=org_id, vehicle_id=check.vehicle_id, item_id=check.item_id)
        .first()
    )
    if lock is None:
        raise UnableCompletionError("ITEM_LOCK_UNAVAILABLE")
    return lock


def _effective_tip(org_id, check):
    rows = list(
        CheckRecord.objects.filter(
            organization_id=org_id, root_check_id=check.root_check_id
        ).order_by("recorded_at", "id")[: MAX_CHAIN_ROWS + 1]
    )
    events = list(
        CheckEvent.objects.filter(
            organization_id=org_id, root_check_id=check.root_check_id
        ).order_by("recorded_at", "id")[: MAX_CHAIN_ROWS + 1]
    )
    if len(rows) > MAX_CHAIN_ROWS or len(events) > MAX_CHAIN_ROWS:
        raise UnableCompletionError("CONFIGURATION_UNAVAILABLE")
    result = effective_checks(
        (
            CheckSnapshot(
                str(row.pk),
                str(row.root_check_id),
                str(row.predecessor_check_id) if row.predecessor_check_id else None,
                row.recorded_at,
                row.outcome,
            )
            for row in rows
        ),
        (
            CheckDecision(
                str(row.root_check_id),
                str(row.selected_check_id),
                row.event_type,
                row.recorded_at,
                str(row.replacement_check_id) if row.replacement_check_id else None,
            )
            for row in events
        ),
        as_of=timezone.now(),
    )
    return next((row for row in result if row.root_id == str(check.root_check_id)), None)


def complete_unable_obligation(
    *,
    organization_id,
    actor_id,
    obligation_id,
    check_id,
    explanation,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Append completion and supporting check link atomically under the item lock."""
    _uuid(organization_id, "organization_id")
    _uuid(actor_id, "actor_id")
    _uuid(obligation_id, "obligation_id")
    _uuid(check_id, "check_id")
    _uuid(operation_id, "operation_id")
    if UUID(operation_id).version != 4:
        raise UnableCompletionError("INVALID_REQUEST", "operation_id")
    if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 4000:
        raise UnableCompletionError("INVALID_REQUEST", "explanation")
    org_id = str(organization_id)
    det_id = new_id()
    digest = hashlib.sha256(
        json.dumps(
            {"obligation": obligation_id, "check": check_id, "explanation": explanation},
            sort_keys=True,
        ).encode()
    ).hexdigest()

    def authorize(org):
        user, roles, actor = _current_roles(org_id, actor_id)
        if not org.active or not permitted(
            "complete_unable_inspect", str(user.pk), roles, active=user.active
        ):
            raise CommandDenied(actor)
        return actor

    def prepare(actor):
        obligation = (
            Obligation.objects.select_related("requirement_version")
            .filter(organization_id=org_id, pk=obligation_id)
            .first()
        )
        check = CheckRecord.objects.filter(organization_id=org_id, pk=check_id).first()
        if obligation is None or check is None:
            raise UnableCompletionError("NOT_FOUND")
        _item_lock(org_id, check)
        # Re-read after acquiring the shared serialization lock.
        check = CheckRecord.objects.select_for_update().get(organization_id=org_id, pk=check_id)
        if (
            check.vehicle_id != obligation.vehicle_id
            or check.item_id != obligation.item_id
            or check.outcome != "UNABLE_TO_INSPECT"
            or not CheckObligationLink.objects.filter(
                organization_id=org_id, check_record=check, obligation=obligation
            ).exists()
        ):
            raise UnableCompletionError("INELIGIBLE_CHECK", "check_id")
        effective = _effective_tip(org_id, check)
        if effective is None or effective.invalidated or effective.check_id != str(check.pk):
            raise UnableCompletionError("INELIGIBLE_CHECK", "check_id")
        if not obligation.early_at <= check.recorded_at:
            raise UnableCompletionError("INELIGIBLE_CHECK", "check_id")
        selected = effective_selection(
            org_id,
            obligation.vehicle_id,
            obligation.item_id,
            obligation.due_at,
            kit_instance_id=obligation.kit_instance_id,
        )
        if selected is None or str(selected.selected_obligation_id) != str(obligation.pk):
            raise UnableCompletionError("INELIGIBLE_OBLIGATION", "obligation_id")
        if check.actor_user_id and str(check.actor_user_id) == str(actor.user.pk):
            if not unable_self_completion_allowed(actor.user.pk, check.actor_user_id):
                raise UnableCompletionError("SELF_COMPLETION_NOT_ALLOWED", "check_id")
        prior = latest_obligation_determination(org_id, obligation_id, for_update=True)
        if prior is not None and not (
            prior.determination_type in ("INVALIDATED", "SUPERSEDED")
            and prior.prior_determination
            and prior.prior_determination.determination_type == "SUPERVISOR_COMPLETED"
        ):
            raise UnableCompletionError("OBLIGATION_ALREADY_DETERMINED")
        for link in CheckObligationLink.objects.filter(
            organization_id=org_id, obligation=obligation
        ).select_related("check_record"):
            evidence = _effective_tip(org_id, link.check_record)
            if (
                evidence
                and not evidence.invalidated
                and evidence.outcome
                in (
                    "PASSED",
                    "FAILED",
                    "NOT_APPLICABLE",
                )
            ):
                raise UnableCompletionError("OBLIGATION_ALREADY_SATISFIED")

        def apply(receipt, current):
            determination = ObligationDetermination.objects.create(
                id=det_id,
                organization_id=org_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                obligation_id=obligation_id,
                vehicle_id=obligation.vehicle_id,
                due_at=obligation.due_at,
                determination_type="SUPERVISOR_COMPLETED",
                explanation=explanation,
                evaluated_inputs={"check_id": check_id},
                prior_determination=prior,
            )
            DeterminationCheck.objects.create(
                organization_id=org_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                determination=determination,
                check_record=check,
            )

        return Plan(
            response_body={"determination_id": det_id, "check_id": check_id},
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=org_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "determination_id": det_id,
                    "obligation_id": obligation_id,
                    "check_id": check_id,
                },
            },
        )

    result = run_command(
        organization_id=org_id,
        operation_id=operation_id,
        command_type="SUPERVISOR_COMPLETE_UNABLE",
        request_digest=digest,
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=prepare,
    )
    if result.operation_reused:
        raise UnableOperationReused()
    if result.replayed:
        return ObligationDetermination.objects.get(
            organization_id=org_id, command__operation_id=operation_id
        )
    return ObligationDetermination.objects.get(pk=det_id)


def invalidate_supervisor_completion(
    *,
    organization_id,
    actor_id,
    determination_id,
    explanation,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Append an administrator-only INVALIDATED child determination."""
    _uuid(determination_id, "determination_id")
    _uuid(operation_id, "operation_id")
    if UUID(operation_id).version != 4:
        raise UnableCompletionError("INVALID_REQUEST", "operation_id")
    if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 4000:
        raise UnableCompletionError("INVALID_REQUEST", "explanation")
    org_id, child_id = str(organization_id), new_id()
    digest = hashlib.sha256(
        json.dumps(
            {"determination": determination_id, "explanation": explanation}, sort_keys=True
        ).encode()
    ).hexdigest()

    def authorize(org):
        user, roles, actor = _current_roles(org_id, actor_id)
        if not org.active or not permitted(
            "invalidate_unable_completion", str(user.pk), roles, active=user.active
        ):
            raise CommandDenied(actor)
        return actor

    def prepare(actor):
        prior = ObligationDetermination.objects.filter(
            organization_id=org_id, pk=determination_id
        ).first()
        if prior is None:
            raise UnableCompletionError("NOT_FOUND")
        if prior.determination_type != "SUPERVISOR_COMPLETED":
            raise UnableCompletionError("NOT_CURRENT_DETERMINATION")
        supporting = (
            DeterminationCheck.objects.filter(organization_id=org_id, determination=prior)
            .select_related("check_record")
            .first()
        )
        if supporting is None:
            raise UnableCompletionError("CONFIGURATION_UNAVAILABLE")
        _item_lock(org_id, supporting.check_record)
        prior = (
            ObligationDetermination.objects.select_for_update()
            .filter(
                organization_id=org_id,
                pk=determination_id,
                determination_type="SUPERVISOR_COMPLETED",
            )
            .first()
        )
        if (
            prior is None
            or latest_obligation_determination(org_id, prior.obligation_id, for_update=True)
            != prior
        ):
            raise UnableCompletionError("NOT_CURRENT_DETERMINATION")

        def apply(receipt, current):
            ObligationDetermination.objects.create(
                id=child_id,
                organization_id=org_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                obligation_id=prior.obligation_id,
                vehicle_id=prior.vehicle_id,
                due_at=prior.due_at,
                determination_type="INVALIDATED",
                prior_determination=prior,
                explanation=explanation,
                evaluated_inputs={"invalidated_determination_id": determination_id},
            )

        return Plan(
            response_body={"determination_id": child_id, "invalidated_id": determination_id},
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "determination_id": child_id,
                    "invalidated_determination_id": determination_id,
                },
            },
        )

    result = run_command(
        organization_id=org_id,
        operation_id=operation_id,
        command_type="SUPERVISOR_COMPLETION_INVALIDATION",
        request_digest=digest,
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=prepare,
    )
    if result.operation_reused:
        raise UnableOperationReused()
    return ObligationDetermination.objects.get(pk=result.body["determination_id"])
