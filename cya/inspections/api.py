"""HTTP endpoints for inspection sessions (issue 85 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/85), SPEC 6.1-6.3).

POST /api/v1/inspection-sessions               open a session on a vehicle, or join the open one
POST /api/v1/inspection-sessions/{id}/reopen   a participant resumes an ended session (SES-3)
POST /api/v1/inspection-sessions/{id}/end      a participant ends an open session

Authenticated same-origin JSON like the other endpoints: actor, organization and roles come from
the signed-in user, never the body (SPEC 3.2). The rules, authorization and audit are in
cya.inspections.commands; this module parses, bounds and maps outcomes to the common rejection
shape. A refusal never echoes the command's text.
"""

import uuid

from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.attention.api import _operation_id
from cya.authorization.backend import canonical_id
from cya.checks.api import Rejected
from cya.deficiencies.api import _json_body, _run
from cya.inspections import commands
from cya.inspections.commands import SessionError

OPEN_REQUIRED = frozenset({"operation_id", "vehicle_id", "template_id"})
OPEN_OPTIONAL = frozenset({"separate"})
REOPEN_FIELDS = frozenset({"operation_id"})
END_REQUIRED = frozenset({"operation_id"})
END_OPTIONAL = frozenset({"state"})


def _identifier(value, path):
    try:
        return canonical_id(value)
    except (ValueError, TypeError, AttributeError):
        raise Rejected(400, "MALFORMED_REQUEST", [(path, "INVALID")]) from None


def _acknowledge(stored):
    body = stored.body
    result = {"code": body["code"], "session_id": body["session_id"], "state": body["state"]}
    if "checklist_version_id" in body:
        result["checklist_version_id"] = body["checklist_version_id"]
    return result


def _call(operation_id, user, correlation_id, run, **arguments):
    try:
        result = run(
            operation_id=_operation_id(operation_id),
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
            **arguments,
        )
    except SessionError:
        raise Rejected(409, "SESSION_UNAVAILABLE") from None
    return operation_id, result, _acknowledge


@never_cache
@require_http_methods(["POST"])
def sessions(request):
    def call(user, correlation_id):
        body = _json_body(request, OPEN_REQUIRED, OPEN_OPTIONAL)
        separate = body.get("separate", False)
        if type(separate) is not bool:
            raise Rejected(400, "MALFORMED_REQUEST", [("separate", "INVALID")])
        return _call(
            body["operation_id"],
            user,
            correlation_id,
            commands.open_or_join_session,
            vehicle_id=_identifier(body["vehicle_id"], "vehicle_id"),
            template_id=_identifier(body["template_id"], "template_id"),
            separate=separate,
        )

    return _run(request, "inspection session", call)


@never_cache
@require_http_methods(["POST"])
def reopen(request, session_id):
    def call(user, correlation_id):
        body = _json_body(request, REOPEN_FIELDS)
        return _call(
            body["operation_id"],
            user,
            correlation_id,
            commands.reopen_session,
            session_id=_identifier(session_id, "session_id"),
        )

    return _run(request, "inspection session", call)


@never_cache
@require_http_methods(["POST"])
def end(request, session_id):
    def call(user, correlation_id):
        body = _json_body(request, END_REQUIRED, END_OPTIONAL)
        state = body.get("state", "CLOSED")
        if state not in ("CLOSED", "INCOMPLETE"):
            raise Rejected(400, "MALFORMED_REQUEST", [("state", "INVALID")])
        return _call(
            body["operation_id"],
            user,
            correlation_id,
            commands.end_session,
            session_id=_identifier(session_id, "session_id"),
            state=state,
        )

    return _run(request, "inspection session", call)
