"""Notification HTTP endpoints over authenticated same-origin JSON (SPEC section 13, NOT-1).

GET  /api/v1/notifications                  the caller's own in-app inbox (any role holder)
POST /api/v1/notifications/{id}/reads       mark one of the caller's own items read
GET  /api/v1/notification-preferences       the caller's own optional switches
POST /api/v1/notification-preferences       switch one event/channel on or off for the caller
GET  /api/v1/notification-policy            mandatory event/channel pairs (administrator)
POST /api/v1/notification-policy            set a pair mandatory or optional (administrator)
GET  /api/v1/notification-failures          email intents that failed and are not yet sent
                                            (administrator; safe error codes only)

Actor, organization and roles come from the signed-in user, never the body or the query; a caller
cannot name another user. Reads need a built-in role (deny by default). Errors use the record-check
shape and codes; the rules live in cya.notifications.commands and .inbox.
"""

import logging
import uuid

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.backend import canonical_id
from cya.authorization.policy import permitted
from cya.checks.api import _reply
from cya.deficiencies.api import _json_body, _run, _stamp
from cya.deficiencies.service import DeficiencyError
from cya.notifications import commands, delivery, inbox
from cya.notifications.rules import NotificationError
from cya.operations.api import _operation, _reader
from cya.operations.commands import StatusError

logger = logging.getLogger(__name__)

PREF_FIELDS = frozenset({"operation_id", "event_type", "channel", "enabled"})
POLICY_FIELDS = frozenset({"operation_id", "event_type", "channel", "mandatory"})
READ_FIELDS = frozenset({"operation_id"})


def _post(request, label, command, fields, **fixed):
    """One notification POST; NotificationError is reported through the deficiency error shape."""

    def call(user, correlation_id):
        try:
            body = _json_body(request, fields)
            operation_id = _operation(body["operation_id"])
            ids = {}
            for name, value in fixed.items():
                try:
                    ids[name] = canonical_id(value)
                except (TypeError, ValueError):
                    raise NotificationError(
                        f"{name} is invalid", code="MALFORMED_REQUEST", status=400
                    ) from None
            result = command(
                operation_id=operation_id,
                organization_id=str(user.organization_id),
                actor_id=str(user.pk),
                correlation_id=correlation_id,
                request_attempt_id=str(uuid.uuid4()),
                **{k: v for k, v in body.items() if k != "operation_id"},
                **ids,
            )
        except (NotificationError, StatusError) as error:
            raise DeficiencyError(
                error.code, str(error), status=error.status, path=getattr(error, "path", None)
            ) from None
        return operation_id, result, lambda stored: dict(stored.body)

    return _run(request, label, call)


def _administrator(request, allowed=frozenset()):
    """(user, None, correlation id) for a permitted administrator GET, else the refusal."""
    user, refusal, correlation_id = _reader(request, allowed)
    if refusal is not None:
        return None, refusal, correlation_id
    from cya.notifications.commands import POLICY_ACTION
    from cya.shell.navigation import roles_for

    if not permitted(POLICY_ACTION, str(user.pk), roles_for(user)):
        return None, _reply(403, "FORBIDDEN", correlation_id), correlation_id
    return user, None, correlation_id


def _unavailable(label, correlation_id):
    logger.error("%s unavailable", label)
    return _reply(503, "RETRY_LATER", correlation_id)


@never_cache
@require_http_methods(["GET"])
def notifications(request):
    user, refusal, correlation_id = _reader(request, frozenset({"limit", "unread"}))
    if refusal is not None:
        return refusal
    limit, unread = request.GET.get("limit", str(inbox.DEFAULT_LIMIT)), request.GET.get("unread")
    if not limit.isdigit() or unread not in (None, "0", "1"):
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
    try:
        page = inbox.inbox(user, limit=int(limit), unread_only=unread == "1")
        unread_total = inbox.unread_count(user)
    except ValueError:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("limit", "INVALID")])
    except DatabaseError:
        return _unavailable("notification inbox", correlation_id)
    for item in page["items"]:
        item["recorded_at"] = _stamp(item["recorded_at"])
    return JsonResponse(
        {"status": "OK", "correlation_id": correlation_id, "unread_count": unread_total, **page}
    )


@never_cache
@require_http_methods(["POST"])
def reads(request, notification_id):
    return _post(
        request, "notification read", commands.mark_read, READ_FIELDS, outbox_id=notification_id
    )


@never_cache
@require_http_methods(["GET", "POST"])
def preferences(request):
    if request.method == "POST":
        return _post(request, "notification preference", commands.set_preference, PREF_FIELDS)
    user, refusal, correlation_id = _reader(request, frozenset())
    if refusal is not None:
        return refusal
    try:
        found = inbox.preferences(user)
    except DatabaseError:
        return _unavailable("notification preferences", correlation_id)
    return JsonResponse({"status": "OK", "correlation_id": correlation_id, "preferences": found})


@never_cache
@require_http_methods(["GET", "POST"])
def policy(request):
    if request.method == "POST":
        return _post(request, "notification policy", commands.set_policy, POLICY_FIELDS)
    user, refusal, correlation_id = _administrator(request)
    if refusal is not None:
        return refusal
    try:
        found = inbox.policies(user.organization_id)
    except DatabaseError:
        return _unavailable("notification policy", correlation_id)
    return JsonResponse({"status": "OK", "correlation_id": correlation_id, "policy": found})


@never_cache
@require_http_methods(["GET"])
def failures(request):
    user, refusal, correlation_id = _administrator(request)
    if refusal is not None:
        return refusal
    try:
        found = delivery.failures(user.organization_id)
    except DatabaseError:
        return _unavailable("notification failures", correlation_id)
    return JsonResponse({"status": "OK", "correlation_id": correlation_id, "failures": found})
