"""Operational-status HTTP endpoints over authenticated same-origin JSON (SPEC section 9).

POST /api/v1/vehicles/{id}/status-changes sets the status (supervisor, administrator),
POST /api/v1/vehicles/{id}/status-requests asks for a change (crew and above) and
POST /api/v1/status-requests/{id}/decisions approves or denies a request (supervisor,
administrator). GET /api/v1/vehicles/{id}/status returns the current status, its history and the
vehicle's pending requests; GET /api/v1/status-requests lists pending requests, optionally for
one vehicle. Status changes only through these explicit human acts (OPS-1, OPS-2, AC-19).

Actor, organization and roles come from the signed-in user, never the body (SPEC 3.2); the
command's permission check is the authority and a hidden button is only a convenience. Reads need
a signed-in active user holding a built-in role (deny by default, AC-24). Errors use the
record-check shape and codes; business rules live in cya.operations.commands.
"""

import logging
import uuid

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.backend import canonical_id
from cya.checks.api import _reply
from cya.deficiencies.api import _json_body, _run, _stamp
from cya.deficiencies.service import DeficiencyError
from cya.operations import commands, service
from cya.operations.commands import StatusError
from cya.shell.navigation import roles_for
from tools.command_identity import human_operation_id

logger = logging.getLogger(__name__)

SET_FIELDS = frozenset({"operation_id", "status", "explanation"})
REQUEST_FIELDS = frozenset({"operation_id", "requested_status", "reason"})
DECIDE_FIELDS = frozenset({"operation_id", "decision", "explanation"})
RESTRICTIONS = frozenset({"restrictions"})


def _malformed(path):
    return StatusError(f"{path} is invalid", code="MALFORMED_REQUEST", status=400, path=path)


def _id(value, path):
    try:
        return canonical_id(value)
    except (TypeError, ValueError):
        raise _malformed(path) from None


def _operation(value):
    try:
        human_operation_id(value)
    except (TypeError, ValueError, AttributeError):
        raise _malformed("operation_id") from None
    return value


def _post(request, label, command, fields, optional, **fixed):
    """One status POST. StatusError is reported through the deficiency error shape and codes."""

    def call(user, correlation_id):
        try:
            body = _json_body(request, fields, optional)
            operation_id = _operation(body["operation_id"])
            extra = {k: body[k] for k in optional if k in body}
            ids = {name: _id(value, name) for name, value in fixed.items()}
            result = command(
                operation_id=operation_id,
                organization_id=str(user.organization_id),
                actor_id=str(user.pk),
                correlation_id=correlation_id,
                request_attempt_id=str(uuid.uuid4()),
                **{k: v for k, v in body.items() if k not in ("operation_id", *optional)},
                **extra,
                **ids,
            )
        except StatusError as error:
            raise DeficiencyError(
                error.code, str(error), status=error.status, path=error.path
            ) from None
        return operation_id, result, lambda stored: dict(stored.body)

    return _run(request, label, call)


@never_cache
@require_http_methods(["POST"])
def status_changes(request, vehicle_id):
    return _post(
        request,
        "vehicle status change",
        commands.set_vehicle_status,
        SET_FIELDS,
        RESTRICTIONS,
        vehicle_id=vehicle_id,
    )


@never_cache
@require_http_methods(["POST"])
def status_requests(request, vehicle_id):
    return _post(
        request,
        "vehicle status request",
        commands.request_status_change,
        REQUEST_FIELDS,
        frozenset(),
        vehicle_id=vehicle_id,
    )


@never_cache
@require_http_methods(["POST"])
def decisions(request, request_id):
    return _post(
        request,
        "vehicle status decision",
        commands.decide_status_request,
        DECIDE_FIELDS,
        RESTRICTIONS,
        request_id=request_id,
    )


def _reader(request, allowed):
    """(user, None, correlation id) for a permitted GET, else (None, the refusal, correlation id)."""
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return None, _reply(401, "AUTHENTICATION_REQUIRED", correlation_id), correlation_id
    if not roles_for(user):
        return None, _reply(403, "FORBIDDEN", correlation_id), correlation_id
    if set(request.GET) - allowed or any(len(request.GET.getlist(k)) > 1 for k in request.GET):
        refusal = _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
        return None, refusal, correlation_id
    return user, None, correlation_id


def _reply_error(error, correlation_id):
    errors = [(error.path, error.code)] if error.path else []
    return _reply(error.status, error.code, correlation_id, errors)


def _stamped_requests(view):
    for row in view["requests"]:
        row["recorded_at"] = _stamp(row["recorded_at"])
    return view


@never_cache
@require_http_methods(["GET"])
def vehicle_status(request, vehicle_id):
    """Current status, history (newest first) and the vehicle's pending requests (OPS-4, OPS-6)."""
    user, refusal, correlation_id = _reader(request, frozenset())
    if refusal is not None:
        return refusal
    try:
        vehicle_id = _id(vehicle_id, "vehicle_id")
        organization_id = str(user.organization_id)
        view = service.status_history(organization_id, vehicle_id)
        pending = _stamped_requests(service.pending_requests(organization_id, vehicle_id))
    except StatusError as error:
        return _reply_error(error, correlation_id)
    except DatabaseError:
        logger.error("vehicle status unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    for row in view["history"]:
        row["recorded_at"] = _stamp(row["recorded_at"])
    if view["current"] is not None:
        view["current"]["recorded_at"] = _stamp(view["current"]["recorded_at"])
    return JsonResponse(
        {
            "status": "OK",
            "correlation_id": correlation_id,
            "vehicle_id": vehicle_id,
            **view,
            "pending_requests": pending["requests"],
            "pending_truncated": pending["truncated"],
        }
    )


@never_cache
@require_http_methods(["GET"])
def pending_status_requests(request):
    """Requests nobody has decided yet, oldest first; vehicle_id narrows them to one vehicle."""
    user, refusal, correlation_id = _reader(request, frozenset({"vehicle_id"}))
    if refusal is not None:
        return refusal
    try:
        vehicle_id = request.GET.get("vehicle_id")
        if vehicle_id is not None:
            vehicle_id = _id(vehicle_id, "vehicle_id")
        view = _stamped_requests(service.pending_requests(str(user.organization_id), vehicle_id))
    except StatusError as error:
        return _reply_error(error, correlation_id)
    except DatabaseError:
        logger.error("status requests unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    return JsonResponse({"status": "OK", "correlation_id": correlation_id, **view})


@never_cache
@require_http_methods(["POST"])
def ad_hoc_inspections(request, vehicle_id):
    """An explicit supervisor order; returning to service does not call this endpoint."""
    from django.utils.dateparse import parse_datetime

    from cya.obligations.adhoc import request_inspection

    def command(**kwargs):
        value = kwargs["due_at"]
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.utcoffset() is None:
            raise _malformed("due_at")
        kwargs["due_at"] = parsed
        return request_inspection(**kwargs)

    return _post(
        request,
        "ad hoc inspection",
        command,
        frozenset({"operation_id", "items", "due_at", "explanation"}),
        frozenset(),
        vehicle_id=vehicle_id,
    )
