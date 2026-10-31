"""HTTP adapters for audited Unable-to-Inspect determination commands."""

import json
import logging
import uuid

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from cya.authorization.backend import AuthorizationUnavailable
from cya.conditions.uti import (
    UnableCompletionError,
    UnableOperationReused,
    complete_unable_obligation,
    invalidate_supervisor_completion,
)
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure

logger = logging.getLogger(__name__)

_ERROR_STATUS = {
    "INVALID_REQUEST": 400,
    "NOT_FOUND": 404,
    "INELIGIBLE_CHECK": 422,
    "INELIGIBLE_OBLIGATION": 422,
    "SELF_COMPLETION_NOT_ALLOWED": 422,
    "OBLIGATION_ALREADY_DETERMINED": 409,
    "OBLIGATION_ALREADY_SATISFIED": 409,
    "NOT_CURRENT_DETERMINATION": 409,
    "ITEM_LOCK_UNAVAILABLE": 503,
    "CONFIGURATION_UNAVAILABLE": 503,
}


def _rejected(status, code, correlation, field=None, field_code="INVALID"):
    errors = [{"path": field, "code": field_code}] if field else []
    return JsonResponse(
        {"status": "REJECTED", "code": code, "correlation_id": correlation, "field_errors": errors},
        status=status,
    )


def _body(request):
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise UnableCompletionError("INVALID_REQUEST") from None
    if request.content_type != "application/json" or declared > 16384:
        raise UnableCompletionError("INVALID_REQUEST")
    try:
        raw = request.read(16385)
        if len(raw) > 16384:
            raise UnableCompletionError("INVALID_REQUEST")
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise UnableCompletionError("INVALID_REQUEST") from None
    if not isinstance(value, dict):
        raise UnableCompletionError("INVALID_REQUEST")
    return value


def _command(request, body, invoke):
    correlation = str(uuid.uuid4())
    try:
        if request.cya_user is None:
            return _rejected(401, "AUTHENTICATION_REQUIRED", correlation)
        result = invoke(body, correlation, request.cya_user)
        return JsonResponse(
            {
                "status": "COMMITTED",
                "correlation_id": result.command.correlation_id,
                "result": {"determination_id": str(result.pk)},
            },
            status=201,
        )
    except UnableCompletionError as error:
        status = _ERROR_STATUS.get(error.code)
        if status is None:
            logger.error("determination refused with an unmapped error code")
            return _rejected(503, "CONFIGURATION_UNAVAILABLE", correlation)
        if status == 503:
            logger.error("determination not confirmed: %s", error.code)
        return _rejected(status, error.code, correlation, error.path)
    except UnableOperationReused:
        return _rejected(409, "OPERATION_REUSED", correlation, "operation_id", "REUSED")
    except CommandDenied:
        return _rejected(403, "FORBIDDEN", correlation)
    except OperationConflict:
        return _rejected(409, "OPERATION_OWNED_BY_ANOTHER_ACTOR", correlation)
    except AuditPersistenceFailure:
        logger.error("determination not confirmed: audit unavailable")
        return _rejected(503, "AUDIT_UNAVAILABLE", correlation)
    except AuthorizationUnavailable:
        logger.error("determination not confirmed: authorization unavailable")
        return _rejected(503, "CONFIGURATION_UNAVAILABLE", correlation)
    except (RetryLater, OutcomeUnknown):
        logger.error("determination not confirmed: retry with the same operation id")
        return _rejected(503, "RETRY_LATER", correlation)
    except DatabaseError:
        logger.error("determination not confirmed: retry with the same operation id")
        return _rejected(503, "RETRY_LATER", correlation)


@never_cache
@require_POST
def complete_unable(request, obligation_id):
    if request.cya_user is None:
        return _rejected(401, "AUTHENTICATION_REQUIRED", str(uuid.uuid4()))
    try:
        body = _body(request)
        if set(body) != {"operation_id", "check_id", "explanation"}:
            raise UnableCompletionError("INVALID_REQUEST")
    except UnableCompletionError as error:
        return _rejected(400, error.code, str(uuid.uuid4()), error.path)
    return _command(
        request,
        body,
        lambda data, correlation, user: complete_unable_obligation(
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            obligation_id=obligation_id,
            check_id=data["check_id"],
            explanation=data["explanation"],
            operation_id=data["operation_id"],
            correlation_id=correlation,
            request_attempt_id=str(uuid.uuid4()),
        ),
    )


@never_cache
@require_POST
def invalidate_completion(request, determination_id):
    if request.cya_user is None:
        return _rejected(401, "AUTHENTICATION_REQUIRED", str(uuid.uuid4()))
    try:
        body = _body(request)
        if set(body) != {"operation_id", "explanation"}:
            raise UnableCompletionError("INVALID_REQUEST")
    except UnableCompletionError as error:
        return _rejected(400, error.code, str(uuid.uuid4()), error.path)
    return _command(
        request,
        body,
        lambda data, correlation, user: invalidate_supervisor_completion(
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            determination_id=determination_id,
            explanation=data["explanation"],
            operation_id=data["operation_id"],
            correlation_id=correlation,
            request_attempt_id=str(uuid.uuid4()),
        ),
    )
