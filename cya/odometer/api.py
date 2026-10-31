"""POST /api/v1/odometer-entries over authenticated same-origin JSON (contracts section 4).

Same shape as POST /api/v1/checks (cya.checks.api): the handler parses and bounds the body,
maps outcomes to the contract's error shape and decides nothing itself; actor, organization and
roles come from the signed-in user, never the body (SPEC 3.2). Rules live in
cya.odometer.commands.
"""

import json
import logging
import uuid
from datetime import UTC

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.backend import AuthorizationUnavailable
from cya.checks.api import Rejected, _read_body, _reply
from cya.checks.commands import CheckError, SelfInvalidationForbidden
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.odometer.commands import record_odometer
from cya.odometer.corrections import MODELS, correct_reading, invalidate_reading
from cya.odometer.models import OdometerEntry, ReadingEvent

logger = logging.getLogger(__name__)

REQUIRED = ("operation_id", "vehicle_id", "value", "unit")
OPTIONAL = ("engine_hours", "confirm_anomaly", "explanation", "session_id")
ALLOWED = frozenset(REQUIRED + OPTIONAL)


def _parse(request):
    """The request fields for record_odometer, or Rejected; unknown fields are refused."""
    if request.content_type != "application/json":
        raise Rejected(400, "MALFORMED_REQUEST")
    try:
        body = json.loads(_read_body(request).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Rejected(400, "MALFORMED_REQUEST") from None
    if not isinstance(body, dict):
        raise Rejected(400, "MALFORMED_REQUEST")
    problems = [(name, "REQUIRED") for name in REQUIRED if name not in body]
    if set(body) - ALLOWED:
        problems.append(("$", "UNKNOWN_FIELD"))
    if problems:
        raise Rejected(400, "MALFORMED_REQUEST", problems)
    return body


def _acknowledgement(result, user):
    """The stored committed result plus the server instant read back from the row."""
    row = (
        OdometerEntry.objects.filter(
            organization_id=user.organization_id, pk=result.body["entry_id"]
        )
        .values("recorded_at")
        .first()
    )
    if row is None:
        raise DatabaseError("committed odometer entry could not be read back")
    recorded = row["recorded_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "entry_id": result.body["entry_id"],
        "engine_hours_entry_id": result.body["engine_hours_entry_id"],
        "server_recorded_at": recorded,
        "anomaly_confirmed": result.body["anomaly_confirmed"],
        "obligation_links": result.body["obligation_links"],
    }


@never_cache
@require_http_methods(["POST"])
def odometer_entries(request):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        fields = _parse(request)
        result = record_odometer(
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
            **fields,
        )
        if result.operation_reused:
            return _reply(409, "OPERATION_REUSED", correlation_id, [("operation_id", "REUSED")])
        return JsonResponse(
            {
                "status": "COMMITTED",
                "operation_id": fields["operation_id"],
                "correlation_id": result.correlation_id,
                "result": _acknowledgement(result, user),
            },
            status=result.status,
        )
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except CheckError as error:
        errors = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, errors)
    except CommandDenied:
        return _reply(403, "FORBIDDEN", correlation_id)
    except OperationConflict:
        return _reply(409, "OPERATION_OWNED_BY_ANOTHER_ACTOR", correlation_id)
    except AuditPersistenceFailure:
        logger.error("odometer entry not confirmed: audit unavailable")
        return _reply(503, "AUDIT_UNAVAILABLE", correlation_id)
    except AuthorizationUnavailable:
        logger.error("odometer entry not confirmed: authorization unavailable")
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)
    except (RetryLater, OutcomeUnknown, DatabaseError):
        logger.error("odometer entry not confirmed: retry with the same operation id")
        return _reply(503, "RETRY_LATER", correlation_id)


def _decision_body(request, required, optional=()):
    if request.content_type != "application/json":
        raise Rejected(400, "MALFORMED_REQUEST")
    try:
        body = json.loads(_read_body(request).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Rejected(400, "MALFORMED_REQUEST") from None
    if (
        not isinstance(body, dict)
        or not set(required) <= set(body)
        or set(body) - set(required + optional)
    ):
        raise Rejected(400, "MALFORMED_REQUEST", [("$", "INVALID_FIELDS")])
    return body


def _decision_response(result, user, operation_id, kind):
    model = MODELS[kind]
    if "entry_id" in result.body:
        row = (
            model.objects.filter(organization_id=user.organization_id, pk=result.body["entry_id"])
            .values("recorded_at")
            .first()
        )
    else:
        row = (
            ReadingEvent.objects.filter(
                organization_id=user.organization_id,
                pk=result.body["invalidation_event_id"],
            )
            .values("recorded_at")
            .first()
        )
    if row is None:
        raise DatabaseError("committed reading decision could not be read back")
    return JsonResponse(
        {
            "status": "COMMITTED",
            "operation_id": operation_id,
            "correlation_id": result.correlation_id,
            "result": {
                **result.body,
                "server_recorded_at": row["recorded_at"]
                .astimezone(UTC)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            },
        },
        status=result.status,
    )


def _decision(request, kind, reading_id, *, correction):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        if kind not in MODELS:
            raise Rejected(404, "NOT_FOUND")
        if correction:
            body = _decision_body(
                request,
                ("operation_id", "expected_tip_entry_id", "value"),
                ("confirm_anomaly", "explanation"),
            )
            result = correct_reading(
                kind=kind,
                reading_id=reading_id,
                expected_tip_entry_id=body["expected_tip_entry_id"],
                value=body["value"],
                confirm_anomaly=body.get("confirm_anomaly", False),
                explanation=body.get("explanation"),
                operation_id=body["operation_id"],
                organization_id=str(user.organization_id),
                actor_id=str(user.pk),
                correlation_id=correlation_id,
                request_attempt_id=str(uuid.uuid4()),
            )
        else:
            body = _decision_body(request, ("operation_id", "explanation"))
            result = invalidate_reading(
                kind=kind,
                reading_id=reading_id,
                explanation=body["explanation"],
                operation_id=body["operation_id"],
                organization_id=str(user.organization_id),
                actor_id=str(user.pk),
                correlation_id=correlation_id,
                request_attempt_id=str(uuid.uuid4()),
            )
        if result.operation_reused:
            return _reply(409, "OPERATION_REUSED", correlation_id, [("operation_id", "REUSED")])
        return _decision_response(result, user, body["operation_id"], kind)
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except CheckError as error:
        return _reply(
            error.status,
            error.code,
            correlation_id,
            [(error.path, error.code)] if error.path else [],
        )
    except SelfInvalidationForbidden:
        return _reply(403, "SELF_INVALIDATION_FORBIDDEN", correlation_id)
    except CommandDenied:
        return _reply(403, "FORBIDDEN", correlation_id)
    except OperationConflict:
        return _reply(409, "OPERATION_OWNED_BY_ANOTHER_ACTOR", correlation_id)
    except AuditPersistenceFailure:
        logger.error("reading decision not confirmed: audit unavailable")
        return _reply(503, "AUDIT_UNAVAILABLE", correlation_id)
    except AuthorizationUnavailable:
        logger.error("reading decision not confirmed: authorization unavailable")
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)
    except (RetryLater, OutcomeUnknown, DatabaseError):
        logger.error("reading decision not confirmed: retry with the same operation id")
        return _reply(503, "RETRY_LATER", correlation_id)


@never_cache
@require_http_methods(["POST"])
def reading_corrections(request, kind, reading_id):
    return _decision(request, kind, reading_id, correction=True)


@never_cache
@require_http_methods(["POST"])
def reading_invalidations(request, kind, reading_id):
    return _decision(request, kind, reading_id, correction=False)
