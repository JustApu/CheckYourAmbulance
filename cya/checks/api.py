"""POST /api/v1/checks over authenticated same-origin JSON (contracts section 4).

The handler parses and bounds the body, maps outcomes to the contract's error shape and adds
nothing of its own to the decision: actor, organization and roles come from the signed-in
user, never the body (SPEC 3.2). Business rules live in cya.checks.commands.

Errors are {status: "REJECTED", code, correlation_id, field_errors: [{path, code}]}. Paths
name schema fields, never echo user content. CSRF protection stays on; csrf_failure gives
/api/ callers a JSON refusal and leaves HTML pages on Django's default.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.csrf import csrf_failure as django_csrf_failure
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.backend import AuthorizationUnavailable
from cya.checks.commands import (
    CheckError,
    SelfInvalidationForbidden,
    correct_check,
    invalidate_chain,
    record_check,
)
from cya.checks.conflicts import CheckConflict
from cya.checks.models import CheckEvent, CheckRecord
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.deficiencies.service import DeficiencyError

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
REQUIRED = ("operation_id", "vehicle_id", "item_id", "checklist_version_id", "purpose")
OPTIONAL = (
    "outcome",
    "session_id",
    "basis_check_id",
    "acknowledges_check_id",
    "quantity",
    "measurement",
    "explanation",
    "client_observed_at",
    "kit_id",
    "verification_for_deficiency_id",
    "deficiency",
)
ALLOWED = frozenset(REQUIRED + OPTIONAL)


class Rejected(Exception):
    def __init__(self, status, code, field_errors=()):
        super().__init__(code)
        self.status, self.code, self.field_errors = status, code, list(field_errors)


def _reply(status, code, correlation_id, field_errors=()):
    return JsonResponse(
        {
            "status": "REJECTED",
            "code": code,
            "correlation_id": correlation_id,
            "field_errors": [{"path": path, "code": c} for path, c in field_errors],
        },
        status=status,
    )


def _conflict(conflict, operation_id, correlation_id):
    """The dedicated warning shape; it carries only fields every signed-in user may see."""
    newest = dict(conflict.newest)
    newest["server_recorded_at"] = (
        newest["server_recorded_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    return JsonResponse(
        {
            "status": "CONFLICT",
            "code": "CHECK_CONFLICT",
            "operation_id": operation_id,
            "correlation_id": correlation_id,
            "newest_check": newest,
            "obligation_id": conflict.obligation_id,
        },
        status=409,
    )


def _read_body(request):
    """At most MAX_BODY_BYTES, whatever the declared length claims."""
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise Rejected(400, "MALFORMED_REQUEST") from None
    if declared > MAX_BODY_BYTES:
        raise Rejected(413, "REQUEST_TOO_LARGE")
    raw = request.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise Rejected(413, "REQUEST_TOO_LARGE")
    return raw


def _observed_at(value):
    if not isinstance(value, str):
        raise Rejected(400, "MALFORMED_REQUEST", [("client_observed_at", "INVALID")])
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise Rejected(400, "MALFORMED_REQUEST", [("client_observed_at", "INVALID")]) from None


def _parse(request):
    """The request fields for record_check, or Rejected; unknown fields are refused."""
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
    if body.get("client_observed_at") is not None:
        body["client_observed_at"] = _observed_at(body["client_observed_at"])
    return body


def _acknowledgement(result, user):
    """The stored committed result plus the server instant read back from the row."""
    row = (
        CheckRecord.objects.filter(organization_id=user.organization_id, pk=result.body["check_id"])
        .values("recorded_at")
        .first()
    )
    if row is None:
        raise DatabaseError("committed check could not be read back")
    recorded = row["recorded_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "check_id": result.body["check_id"],
        "outcome": result.body["outcome"],
        "server_recorded_at": recorded,
        "obligation_links": result.body["obligation_links"],
        "deficiency_ids": result.body["deficiency_ids"],
        "event_ids": result.body["event_ids"],
    }


def _correction_acknowledgement(result, user):
    row = (
        CheckRecord.objects.filter(organization_id=user.organization_id, pk=result.body["check_id"])
        .values("recorded_at")
        .first()
    )
    if row is None:
        raise DatabaseError("committed correction could not be read back")
    return {
        "check_id": result.body["check_id"],
        "outcome": result.body["outcome"],
        "server_recorded_at": row["recorded_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "root_check_id": result.body["root_check_id"],
        "supersedes_check_id": result.body["supersedes_check_id"],
        "event_ids": result.body["event_ids"],
        # A receipt committed before issue 81 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/81) has no such key; replay it as "no effects".
        "deficiency_event_ids": result.body.get("deficiency_event_ids", []),
        "obligation_links": result.body["obligation_links"],
    }


@never_cache
@require_http_methods(["POST"])
def checks(request):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        fields = _parse(request)
        result = record_check(
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
            **fields,
        )
        if result.operation_reused:
            # Same actor and id but a different request: nothing was applied (REC-4).
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
    except CheckConflict as conflict:
        return _conflict(conflict, fields["operation_id"], correlation_id)
    except (CheckError, DeficiencyError) as error:
        errors = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, errors)
    except CommandDenied:
        return _reply(403, "FORBIDDEN", correlation_id)
    except OperationConflict:
        return _reply(409, "OPERATION_OWNED_BY_ANOTHER_ACTOR", correlation_id)
    except AuditPersistenceFailure:
        logger.error("check not confirmed: audit unavailable")
        return _reply(503, "AUDIT_UNAVAILABLE", correlation_id)
    except AuthorizationUnavailable:
        logger.error("check not confirmed: authorization unavailable")
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)
    except (RetryLater, OutcomeUnknown, DatabaseError):
        logger.error("check not confirmed: retry with the same operation id")
        return _reply(503, "RETRY_LATER", correlation_id)


@never_cache
@require_http_methods(["POST"])
def corrections(request, check_id):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        if request.content_type != "application/json":
            raise Rejected(400, "MALFORMED_REQUEST")
        try:
            body = json.loads(_read_body(request).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise Rejected(400, "MALFORMED_REQUEST") from None
        required = {"operation_id", "expected_tip_check_id", "outcome"}
        optional = {"quantity", "measurement", "explanation"}
        if (
            not isinstance(body, dict)
            or not required <= set(body)
            or set(body) - required - optional
        ):
            raise Rejected(400, "MALFORMED_REQUEST", [("$", "INVALID_FIELDS")])
        result = correct_check(
            check_id=check_id,
            expected_tip_check_id=body["expected_tip_check_id"],
            outcome=body["outcome"],
            operation_id=body["operation_id"],
            quantity=body.get("quantity"),
            measurement=body.get("measurement"),
            explanation=body.get("explanation"),
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
        )
        if result.operation_reused:
            return _reply(409, "OPERATION_REUSED", correlation_id, [("operation_id", "REUSED")])
        return JsonResponse(
            {
                "status": "COMMITTED",
                "operation_id": body["operation_id"],
                "correlation_id": result.correlation_id,
                "result": _correction_acknowledgement(result, user),
            },
            status=result.status,
        )
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except CheckError as error:
        return _reply(
            error.status,
            error.code,
            correlation_id,
            [(error.path, error.code)] if error.path else [],
        )
    except CommandDenied:
        return _reply(403, "FORBIDDEN", correlation_id)
    except OperationConflict:
        return _reply(409, "OPERATION_OWNED_BY_ANOTHER_ACTOR", correlation_id)
    except AuditPersistenceFailure:
        logger.error("check correction not confirmed: audit unavailable")
        return _reply(503, "AUDIT_UNAVAILABLE", correlation_id)
    except AuthorizationUnavailable:
        logger.error("check correction not confirmed: authorization unavailable")
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)
    except (RetryLater, OutcomeUnknown, DatabaseError):
        logger.error("check correction not confirmed: retry with the same operation id")
        return _reply(503, "RETRY_LATER", correlation_id)


def _invalidation_acknowledgement(result, user):
    row = (
        CheckEvent.objects.filter(
            organization_id=user.organization_id, pk=result.body["invalidation_event_id"]
        )
        .values("recorded_at")
        .first()
    )
    if row is None:
        raise DatabaseError("committed invalidation could not be read back")
    return {
        "root_check_id": result.body["root_check_id"],
        "invalidation_event_id": result.body["invalidation_event_id"],
        "server_recorded_at": row["recorded_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "affected_obligation_ids": result.body["affected_obligation_ids"],
        "reopened_deficiency_ids": result.body["reopened_deficiency_ids"],
        "voided_determination_ids": result.body["voided_determination_ids"],
    }


@never_cache
@require_http_methods(["POST"])
def invalidations(request, check_id):
    """POST /api/v1/checks/{check_id}/invalidations: administrators only (contracts)."""
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        if request.content_type != "application/json":
            raise Rejected(400, "MALFORMED_REQUEST")
        try:
            body = json.loads(_read_body(request).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise Rejected(400, "MALFORMED_REQUEST") from None
        if not isinstance(body, dict) or set(body) != {"operation_id", "explanation"}:
            raise Rejected(400, "MALFORMED_REQUEST", [("$", "INVALID_FIELDS")])
        result = invalidate_chain(
            check_id=check_id,
            explanation=body["explanation"],
            operation_id=body["operation_id"],
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
        )
        if result.operation_reused:
            return _reply(409, "OPERATION_REUSED", correlation_id, [("operation_id", "REUSED")])
        return JsonResponse(
            {
                "status": "COMMITTED",
                "operation_id": body["operation_id"],
                "correlation_id": result.correlation_id,
                "result": _invalidation_acknowledgement(result, user),
            },
            status=result.status,
        )
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
        return _reply(403, "ADMINISTRATOR_REQUIRED", correlation_id)
    except OperationConflict:
        return _reply(409, "OPERATION_OWNED_BY_ANOTHER_ACTOR", correlation_id)
    except AuditPersistenceFailure:
        logger.error("check invalidation not confirmed: audit unavailable")
        return _reply(503, "AUDIT_UNAVAILABLE", correlation_id)
    except AuthorizationUnavailable:
        logger.error("check invalidation not confirmed: authorization unavailable")
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)
    except (RetryLater, OutcomeUnknown, DatabaseError):
        logger.error("check invalidation not confirmed: retry with the same operation id")
        return _reply(503, "RETRY_LATER", correlation_id)


def csrf_failure(request, reason=""):
    if request.path.startswith("/api/"):
        return _reply(403, "CSRF_REJECTED", str(uuid.uuid4()))
    return django_csrf_failure(request, reason)
