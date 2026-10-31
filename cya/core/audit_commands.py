"""Audited command infrastructure; trusted feature handlers own policy and locks.

No endpoint or role policy is installed here. authorize must re-read and lock
current identity/roles; prepare preallocates the immutable result; apply writes
only database effects under the documented resource locks. Callbacks must not
perform network/external effects. Register notification intent transactionally
in the future outbox; publish hints with transaction.on_commit.
"""

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, InterfaceError, OperationalError, connection, transaction

from cya.accounts.models import Organization
from cya.core.audit_repository import ROLES, _code, _uuid, append_audit, append_receipt
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.core.models import CommandReceipt
from tools.command_identity import receipt_identity, worker_operation_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Actor:
    user: object = None
    roles: tuple[str, ...] = ()
    service_identity: str | None = None
    job_key: str | None = None


@dataclass(frozen=True)
class Plan:
    response_body: dict
    apply: object
    response_status: int = 200
    target_type: str | None = None
    target_id: str | None = None
    prior_snapshot: dict | None = None
    new_snapshot: dict | None = None
    recorded_at: datetime | None = None


@dataclass(frozen=True)
class Result:
    status: int
    body: dict
    correlation_id: str
    replayed: bool = False
    operation_reused: bool = False


class CommandDenied(PermissionError):
    """authorize may carry the trusted current actor, never request claims."""

    def __init__(self, actor=None):
        super().__init__("FORBIDDEN")
        self.actor = actor


class OperationConflict(ValueError):
    """Do not reveal another principal's receipt."""


class RetryLater(RuntimeError):
    """The fully rolled-back retry budget was exhausted."""


class OutcomeUnknown(RuntimeError):
    """Connection lost; no authorised committed receipt could be resolved."""


def _identity(organization, operation_id, command_type, actor):
    if not isinstance(actor, Actor):
        raise TypeError("authorize must return an Actor")
    if not isinstance(actor.roles, tuple) or any(role not in ROLES for role in actor.roles):
        raise ValueError("invalid current roles")
    receipt_identity(
        operation_id=operation_id,
        actor_user_id=str(actor.user.pk) if actor.user else None,
        service_identity=actor.service_identity,
        job_key=actor.job_key,
    )
    if actor.user is not None:
        if actor.user.organization_id != organization.pk or actor.user.active is not True:
            raise CommandDenied()
    elif (
        str(
            worker_operation_id(
                str(organization.pk), actor.service_identity, command_type, actor.job_key
            )
        )
        != operation_id
    ):
        raise ValueError("invalid worker key")


def _replay(receipt, actor, command_type, digest):
    identity = (str(actor.user.pk) if actor.user else None, actor.service_identity, actor.job_key)
    if (receipt.actor_user_id, receipt.service_identity, receipt.job_key) != identity:
        logger.error("audit operation identity conflict")
        raise OperationConflict("OPERATION_CONFLICT")
    if actor.user is None and receipt.command_type != command_type:
        logger.error("audit worker command identity conflict")
        raise OperationConflict("OPERATION_CONFLICT")
    return Result(
        receipt.response_status,
        receipt.response_body,
        receipt.correlation_id,
        replayed=True,
        operation_reused=(receipt.command_type != command_type or receipt.request_digest != digest),
    )


def _lookup(organization, operation_id):
    return CommandReceipt.objects.filter(
        organization=organization, operation_id=operation_id
    ).first()


def _duplicate_receipt(error):
    # Only the confirmed unique operation key can arbitrate a replay. A business
    # constraint or the receipt's unrelated primary key is an ordinary failure.
    return (
        isinstance(error, IntegrityError)
        and len(error.args) > 1
        and error.args[0] == 1062
        and re.search(
            r"for key ['`](?:[A-Za-z0-9_]+\.)?receipt_org_operation['`]", str(error.args[1])
        )
        is not None
    )


def _failure_audit(organization_id, actor, action, correlation_id, attempt_id, outcome, reason):
    try:
        with transaction.atomic(durable=True):
            org = Organization.objects.get(pk=organization_id)
            append_audit(
                organization=org,
                request_attempt_id=attempt_id,
                correlation_id=correlation_id,
                action=action,
                outcome=outcome,
                reason_code=reason,
                actor_user=actor.user if actor else None,
                roles_at_time=list(actor.roles) if actor else [],
                service_identity=actor.service_identity if actor else None,
                job_key=actor.job_key if actor else None,
            )
    except Exception as error:
        # Fixed operational alert survives without the failed receipt/outbox.
        logger.error("mandatory audit persistence unavailable")
        raise AuditPersistenceFailure("AUDIT_UNAVAILABLE") from error


@dataclass(frozen=True)
class _Invocation:
    organization_id: str
    operation_id: str
    command_type: str
    request_digest: str
    correlation_id: str
    request_attempt_id: str
    authorize: object
    prepare: object


@dataclass
class _Attempt:
    actor: Actor | None = None
    result: Result | None = None
    committed: bool = False


def _authorize(command):
    org = Organization.objects.get(pk=command.organization_id)
    if org.active is not True:
        raise CommandDenied()
    actor = command.authorize(org)
    _identity(org, command.operation_id, command.command_type, actor)
    return org, actor


def _write_effects(command, org, actor):
    plan = command.prepare(actor)
    if not isinstance(plan, Plan) or not callable(plan.apply):
        raise TypeError("prepare must return an executable Plan")
    receipt = append_receipt(
        organization=org,
        operation_id=command.operation_id,
        actor_user=actor.user,
        service_identity=actor.service_identity,
        job_key=actor.job_key,
        command_type=command.command_type,
        request_digest=command.request_digest,
        response_status=plan.response_status,
        response_body=plan.response_body,
        correlation_id=command.correlation_id,
        recorded_at=plan.recorded_at,
    )
    plan.apply(receipt, actor)
    append_audit(
        organization=org,
        command=receipt,
        request_attempt_id=command.request_attempt_id,
        correlation_id=command.correlation_id,
        action=command.command_type,
        outcome="SUCCEEDED",
        actor_user=actor.user,
        roles_at_time=list(actor.roles),
        service_identity=actor.service_identity,
        job_key=actor.job_key,
        target_type=plan.target_type,
        target_id=plan.target_id,
        prior_snapshot=plan.prior_snapshot,
        new_snapshot=plan.new_snapshot,
    )
    # The immutable stored acknowledgement wins over accidental callback-side
    # mutation of the receipt's in-memory response dictionary.
    stored = CommandReceipt.objects.get(pk=receipt.pk)
    return Result(stored.response_status, stored.response_body, stored.correlation_id)


def _execute_attempt(command, state):
    with transaction.atomic(durable=True):
        # Register before feature callbacks so a post-commit hint failure cannot
        # be mistaken for a rolled-back command. Lost COMMIT responses still
        # require connection discard and authoritative receipt resolution.
        transaction.on_commit(lambda: setattr(state, "committed", True), robust=True)
        org, state.actor = _authorize(command)
        existing = _lookup(org, command.operation_id)
        if existing is not None:
            state.result = _replay(
                existing, state.actor, command.command_type, command.request_digest
            )
        else:
            state.result = _write_effects(command, org, state.actor)
    return state.result


def _resolve(command):
    with transaction.atomic(durable=True):
        org, actor = _authorize(command)
        receipt = _lookup(org, command.operation_id)
        result = (
            _replay(receipt, actor, command.command_type, command.request_digest)
            if receipt
            else None
        )
    return result, actor


def _failure_kind(error, unknown, code):
    if unknown:
        return "OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"
    if isinstance(error, CommandDenied):
        return "DENIED", "FORBIDDEN"
    if isinstance(error, OperationConflict):
        return "FAILED", "OPERATION_CONFLICT"
    return "FAILED", "RETRY_LATER" if code == 1213 else "COMMAND_FAILED"


def run_command(
    *,
    organization_id,
    operation_id,
    command_type,
    request_digest,
    correlation_id,
    request_attempt_id,
    authorize,
    prepare,
):
    """Commit once, replay under current access, retry only known deadlocks (3).

    authorize(organization) returns a freshly locked Actor, or CommandDenied.
    prepare(actor) returns a Plan; its apply(receipt, actor) writes DB effects.
    The automatic success audit captures the planned target/snapshots; further
    per-effect audit rows may be appended by apply in the same transaction.
    Errors propagate for the feature's sanitized HTTP mapping. No transport
    failure is blindly retried, and no raw exception is persisted in audit.
    """
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Audited commands require an outermost autocommit boundary")
    for value in (organization_id, operation_id, correlation_id, request_attempt_id):
        _uuid(value)
    _code(command_type)
    if not isinstance(request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", request_digest):
        raise ValueError("invalid command digest")
    command = _Invocation(
        organization_id,
        operation_id,
        command_type,
        request_digest,
        correlation_id,
        request_attempt_id,
        authorize,
        prepare,
    )
    for attempt in range(3):
        state = _Attempt()
        try:
            return _execute_attempt(command, state)
        except Exception as error:
            if state.committed and state.result is not None:
                logger.error("post-commit callback failed; command remains committed")
                return state.result
            if isinstance(error, CommandDenied):
                state.actor = error.actor
            code = error.args[0] if isinstance(error, OperationalError) and error.args else None
            if code == 1213 and attempt < 2:
                time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
                continue
            unknown = code in (2006, 2013, 2055) or isinstance(error, InterfaceError)
            if _duplicate_receipt(error) or unknown:
                if unknown:
                    connection.close()
                try:
                    result, state.actor = _resolve(command)
                    if result is not None:
                        return result
                except Exception as resolution_error:
                    if isinstance(resolution_error, CommandDenied):
                        state.actor = resolution_error.actor
                    if not unknown:
                        error = resolution_error
                if unknown:
                    error = OutcomeUnknown("OUTCOME_UNKNOWN")
            outcome, reason = _failure_kind(error, unknown, code)
            _failure_audit(
                organization_id,
                state.actor,
                command_type,
                correlation_id,
                request_attempt_id,
                outcome,
                reason,
            )
            if code == 1213:
                raise RetryLater("RETRY_LATER") from error
            raise error
    raise AssertionError("bounded retry loop must return or raise")
