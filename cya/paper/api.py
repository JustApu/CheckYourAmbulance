"""Paper-entry HTTP endpoints (SPEC 8.0.0 LATE-2, LATE-3, REC-5; issue #9 Part B1).

- POST /api/v1/paper-entry-batches saves a batch header (cya.paper.commands).
- GET /api/v1/paper-entry-performers?q= finds users to attribute paper work to.

Both use the /api/v1/checks error shape: {status: "REJECTED", code, correlation_id,
field_errors: [{path, code}]}, with schema paths that never echo user content. Actor,
organization and roles come from the signed-in user, never the body (SPEC 3.2).
"""

import json
import logging
import re
import uuid
from datetime import date, time

from django.db import DatabaseError
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.accounts.models import User
from cya.authorization.backend import AuthorizationUnavailable
from cya.authorization.persistence import current_role_codes
from cya.authorization.policy import permitted
from cya.checks.api import Rejected, _read_body, _reply
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.paper.commands import OTHERS_ACTION, PaperEntryError, record_paper_entry_batch

logger = logging.getLogger(__name__)

REQUIRED = (
    "operation_id",
    "vehicle_id",
    "checklist_version_id",
    "performed_on",
    "performed_time",
    "performer_ids",
)
OPTIONAL = ("kit_definition_version_ids",)
ALLOWED = frozenset(REQUIRED + OPTIONAL)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME = re.compile(r"\d{2}:\d{2}")
SEARCH_MIN, SEARCH_MAX, SEARCH_LIMIT = 2, 100, 20


def _parse_date(value):
    if not isinstance(value, str) or not DATE.fullmatch(value):
        raise Rejected(400, "MALFORMED_REQUEST", [("performed_on", "INVALID")])
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise Rejected(400, "MALFORMED_REQUEST", [("performed_on", "INVALID")]) from None


def _parse_time(value):
    if not isinstance(value, str) or not TIME.fullmatch(value):
        raise Rejected(400, "MALFORMED_REQUEST", [("performed_time", "INVALID")])
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise Rejected(400, "MALFORMED_REQUEST", [("performed_time", "INVALID")]) from None


def _parse(request):
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
    body["performed_on"] = _parse_date(body["performed_on"])
    body["performed_time"] = _parse_time(body["performed_time"])
    return body


@never_cache
@require_http_methods(["POST"])
def batches(request):
    """POST /api/v1/paper-entry-batches: save one paper-entry batch header."""
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        body = _parse(request)
        operation_id = body.pop("operation_id")
        result = record_paper_entry_batch(
            **body,
            operation_id=operation_id,
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
        )
        if result.operation_reused:
            return _reply(409, "OPERATION_REUSED", correlation_id, [("operation_id", "REUSED")])
        body = dict(result.body)
        body.pop("code", None)
        return JsonResponse(
            {
                "status": "COMMITTED",
                "operation_id": operation_id,
                "correlation_id": result.correlation_id,
                "result": body,
            },
            status=result.status,
        )
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except PaperEntryError as error:
        paths = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, paths)
    except CommandDenied:
        return _reply(403, "FORBIDDEN", correlation_id)
    except OperationConflict:
        return _reply(409, "OPERATION_OWNED_BY_ANOTHER_ACTOR", correlation_id)
    except AuditPersistenceFailure:
        logger.error("paper entry batch not confirmed: audit unavailable")
        return _reply(503, "AUDIT_UNAVAILABLE", correlation_id)
    except AuthorizationUnavailable:
        logger.error("paper entry batch not confirmed: authorization unavailable")
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)
    except (RetryLater, OutcomeUnknown, DatabaseError):
        logger.error("paper entry batch not confirmed: retry with the same operation id")
        return _reply(503, "RETRY_LATER", correlation_id)


@never_cache
@require_http_methods(["GET"])
def performers(request):
    """GET /api/v1/paper-entry-performers?q=: users to attribute paper work to (LATE-3).

    Only a Supervisor or Administrator attributes paper work to others, so only they may search.
    Unlike the Roles page, inactive users are included: the person who did the paper check
    may have left since (LATE-3). Results are bounded; the caller refines a long list.
    """
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        roles = current_role_codes(user)
    except DatabaseError:
        return _reply(503, "RETRY_LATER", correlation_id)
    if not permitted(OTHERS_ACTION, str(user.pk), roles):
        return _reply(403, "FORBIDDEN", correlation_id)
    query = request.GET.get("q", "").strip()
    if not SEARCH_MIN <= len(query) <= SEARCH_MAX or set(request.GET) - {"q"}:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("q", "INVALID")])
    try:
        found = list(
            User.objects.filter(organization_id=user.organization_id)
            .filter(Q(display_name__icontains=query) | Q(email__icontains=query))
            .order_by("display_name", "email", "id")
            .values("id", "display_name", "email", "active")[: SEARCH_LIMIT + 1]
        )
    except DatabaseError:
        return _reply(503, "RETRY_LATER", correlation_id)
    return JsonResponse(
        {
            "status": "OK",
            "users": [
                {
                    "user_id": str(row["id"]),
                    "display_name": row["display_name"],
                    "email": row["email"],
                    "active": row["active"],
                }
                for row in found[:SEARCH_LIMIT]
            ],
            "truncated": len(found) > SEARCH_LIMIT,
        }
    )
