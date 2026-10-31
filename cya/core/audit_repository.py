"""Typed append-only writes. Trusted handlers supply sanitized, validated facts.

These functions require the caller's transaction; they neither authorize users
nor infer worker privileges. No raw HTTP payload or exception belongs here.
"""

import json
import re
from collections.abc import Mapping
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.transaction import TransactionManagementError
from django.utils import timezone

from cya.accounts.models import Organization, User
from cya.core.models import AuditEvent, CommandReceipt
from tools.command_identity import canonical_uuid, receipt_identity, worker_operation_id

CANONICAL_ROLES = frozenset({"CREW", "SUPERVISOR", "SUPPLY_OFFICER", "ADMINISTRATOR"})
LEGACY_ROLES = frozenset({"EMT"})
ROLES = CANONICAL_ROLES | LEGACY_ROLES
OUTCOMES = frozenset({"SUCCEEDED", "DENIED", "FAILED", "OUTCOME_UNKNOWN"})
MAX_JSON_BYTES = 262144


def _uuid(value):
    try:
        canonical_uuid(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError("invalid canonical UUID") from exc


def _code(value, limit=100):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]*", value)
        or len(value) > limit
    ):
        raise ValidationError("invalid bounded action/reason code")
    return value


def _json(value, *, snapshot=False):
    if not isinstance(value, Mapping):
        raise ValidationError("JSON must be an object")
    value = dict(value)
    if snapshot and (
        set(value) != {"schema_version", "values"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["values"], dict)
    ):
        raise ValidationError("unsupported snapshot schema")
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("invalid JSON object") from exc
    if len(encoded.encode("ascii")) > MAX_JSON_BYTES:
        raise ValidationError("JSON object exceeds limit")
    # Detach mutable input and normalize exactly what will be stored.
    return json.loads(encoded)


def _transaction():
    if not connection.in_atomic_block:
        raise TransactionManagementError("Audit writes require an explicit transaction")


def append_receipt(
    *,
    organization,
    operation_id,
    command_type,
    request_digest,
    response_status,
    response_body,
    correlation_id,
    recorded_at=None,
    actor_user=None,
    service_identity=None,
    job_key=None,
    result_schema_version=1,
):
    """INSERT the complete acknowledgement before effects in the same transaction.

    No upsert/reservation commit. Workers must pass the exact deterministic key;
    the command handler must separately authorize the named service action.
    """
    _transaction()
    _uuid(str(organization.pk))
    _uuid(correlation_id)
    _code(command_type)
    if recorded_at is not None and (
        not isinstance(recorded_at, datetime)
        or recorded_at.tzinfo is None
        or recorded_at.utcoffset() is None
    ):
        raise ValidationError("receipt recorded_at must be a timezone-aware instant")
    try:
        kind = receipt_identity(
            operation_id=operation_id,
            actor_user_id=str(actor_user.pk) if actor_user else None,
            service_identity=service_identity,
            job_key=job_key,
        )
        if kind == "worker":
            if (
                not isinstance(service_identity, str)
                or len(service_identity) > 200
                or len(job_key) > 200
            ):
                raise ValueError("invalid worker identity")
            expected = worker_operation_id(
                str(organization.pk), service_identity, command_type, job_key
            )
            if str(expected) != operation_id:
                raise ValueError("non-deterministic worker operation")
    except (ValueError, TypeError) as exc:
        raise ValidationError("invalid receipt identity") from exc
    if actor_user is not None and actor_user.organization_id != organization.pk:
        raise ValidationError("actor belongs to another organization")
    if not isinstance(request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", request_digest):
        raise ValidationError("invalid SHA-256 digest")
    if type(result_schema_version) is not int or result_schema_version != 1:
        raise ValidationError("unsupported result schema")
    if type(response_status) is not int or not 200 <= response_status < 300:
        raise ValidationError("only committed success acknowledgements have receipts")
    return CommandReceipt.objects.create(
        organization=organization,
        operation_id=operation_id,
        actor_user=actor_user,
        service_identity=service_identity,
        job_key=job_key,
        command_type=command_type,
        request_digest=request_digest,
        response_status=response_status,
        response_body=_json(response_body),
        result_schema_version=result_schema_version,
        recorded_at=timezone.now() if recorded_at is None else recorded_at,
        correlation_id=correlation_id,
    )


def append_audit(
    *,
    organization,
    request_attempt_id,
    action,
    outcome,
    correlation_id,
    roles_at_time,
    actor_user=None,
    command=None,
    reason_code=None,
    target_type=None,
    target_id=None,
    prior_snapshot=None,
    new_snapshot=None,
    service_identity=None,
    job_key=None,
):
    """Append safe typed evidence; snapshots use the bounded version-1 envelope.

    Callers select the approved snapshot fields; this is validation, not an
    automatic secret scrubber. Missing actors stay anonymous, never fabricated.
    """
    _transaction()
    for value in (request_attempt_id, correlation_id):
        _uuid(value)
    _code(action)
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise ValidationError("unknown audit outcome")
    if outcome != "SUCCEEDED" and reason_code is None:
        raise ValidationError("unsuccessful audit requires a reason code")
    if reason_code is not None:
        _code(reason_code)
    if actor_user is not None and actor_user.organization_id != organization.pk:
        raise ValidationError("actor belongs to another organization")
    if command is not None:
        if command.organization_id != organization.pk or command.actor_user_id != (
            str(actor_user.pk) if actor_user else None
        ):
            raise ValidationError("receipt attribution does not match audit")
        if command.correlation_id != correlation_id:
            raise ValidationError("receipt correlation does not match audit")
        if (command.service_identity, command.job_key) != (service_identity, job_key):
            raise ValidationError("receipt service attribution does not match audit")
    if service_identity is not None or job_key is not None:
        if actor_user is not None or not all(
            isinstance(v, str) and v.strip() and len(v) <= 200 for v in (service_identity, job_key)
        ):
            raise ValidationError("invalid service audit attribution")
    if (
        not isinstance(roles_at_time, list)
        or not all(isinstance(r, str) and r in ROLES for r in roles_at_time)
        or len(set(roles_at_time)) != len(roles_at_time)
    ):
        raise ValidationError("invalid role snapshot")
    if actor_user is None and roles_at_time:
        raise ValidationError("anonymous/service actors have no human roles")
    if (target_type is None) != (target_id is None):
        raise ValidationError("target type and ID must be supplied together")
    if target_type is not None:
        _code(target_type)
        _uuid(target_id)
        targets = {"USER": User, "ORGANIZATION": Organization, "COMMAND_RECEIPT": CommandReceipt}
        if target_type not in targets:
            raise ValidationError("unregistered audit target type")
        if target_type == "ORGANIZATION":
            valid_target = target_id == str(organization.pk)
        else:
            valid_target = (
                targets[target_type]
                .objects.filter(pk=target_id, organization=organization)
                .exists()
            )
        if not valid_target:
            raise ValidationError("audit target belongs to another organization or is missing")
    return AuditEvent.objects.create(
        organization=organization,
        command=command,
        request_attempt_id=request_attempt_id,
        action=action,
        outcome=outcome,
        correlation_id=correlation_id,
        roles_at_time=list(roles_at_time),
        actor_user=actor_user,
        reason_code=reason_code,
        target_type=target_type,
        target_id=target_id,
        service_identity=service_identity,
        job_key=job_key,
        prior_snapshot=_json(prior_snapshot, snapshot=True) if prior_snapshot is not None else None,
        new_snapshot=_json(new_snapshot, snapshot=True) if new_snapshot is not None else None,
        recorded_at=command.recorded_at if command is not None else timezone.now(),
    )
