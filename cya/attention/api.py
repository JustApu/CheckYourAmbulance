"""HTTP endpoints for the checklist views and the view preference (issue 83 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/83), SPEC 6.5).

GET  /api/v1/vehicles/{id}/checklist        rows, attention, known deficiencies, vehicle status
GET  /api/v1/me/view-preferences            the signed-in user's saved view and filter
POST /api/v1/me/view-preferences            save them (audited, idempotent on operation_id)

Authenticated same-origin JSON like the other endpoints. The checklist uses the user's saved view
and filter unless the query string names them, so every device shows the same default. Reads need
only a signed-in active user of the organization; a vehicle of another organization is 404.
"""

import logging
import uuid

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.attention import preferences
from cya.attention.checklist import ChecklistQueryError, vehicle_checklist
from cya.authorization.backend import canonical_id
from cya.checks.api import Rejected, _reply
from cya.checks.models import CheckRecord
from cya.deficiencies.api import _json_body, _run, _stamp
from tools.command_identity import human_operation_id

logger = logging.getLogger(__name__)

PREFERENCE_FIELDS = frozenset({"operation_id", "preferred_view", "needs_attention_only"})
CHECKLIST_QUERY = frozenset({"view", "needs_attention_only"})


def _stamped(value):
    return None if value is None else _stamp(value)


def _row(row, authors):
    return {
        "item_id": row.item_id,
        "item_label": row.item_label,
        "kit_id": row.kit_id,
        "kit_label": row.kit_label,
        "definition_id": row.definition_id,
        "kind": row.kind,
        "instructions": row.instructions,
        "compartment_id": row.compartment_id,
        "compartment_label": row.compartment_label,
        "system_id": row.system_id,
        "system_label": row.system_label,
        "display_order": row.display_order,
        "obligation_state": row.obligation_state,
        "obligation_outcome": row.obligation_outcome,
        "due_at": _stamped(row.due_at),
        "item_attention": row.attention.state,
        "attention_check_id": row.attention.check_id,
        "attention_check_outcome": row.attention.check_outcome,
        "latest_check_id": row.latest_check_id,
        "latest_check_outcome": row.latest_check_outcome,
        "latest_check_at": _stamped(row.latest_check_at),
        "latest_check_by": authors,
        "needs_action": row.needs_action,
        "deficiencies": [{**d, "reported_at": _stamp(d["reported_at"])} for d in row.deficiencies],
        "checklist_version_id": row.checklist_version_id,
        "expected_quantity": row.expected_quantity,
        "unit": row.unit,
        "allowed_outcomes": list(row.allowed_outcomes),
    }


def _authors(organization, rows):
    """check id -> author display name for each row's newest effective check.

    The name is what the conflict warning already shows every signed-in crew member (CON-3).
    """
    ids = {row.latest_check_id for row in rows if row.latest_check_id}
    if not ids:
        return {}
    found = CheckRecord.objects.filter(organization_id=organization, pk__in=ids).select_related(
        "actor_user"
    )
    return {str(c.pk): c.actor_user.display_name for c in found}


def _flag(value):
    if value in ("true", "false"):
        return value == "true"
    raise Rejected(400, "MALFORMED_REQUEST", [("needs_attention_only", "INVALID")])


@never_cache
@require_http_methods(["GET"])
def checklist(request, vehicle_id):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        if set(request.GET) - CHECKLIST_QUERY:
            raise Rejected(400, "MALFORMED_REQUEST", [("$", "INVALID_FIELDS")])
        try:
            vehicle = canonical_id(vehicle_id)
        except ValueError:
            raise Rejected(404, "NOT_FOUND") from None
        organization = str(user.organization_id)
        saved = preferences.get_preferences(organization, user.pk)
        view = request.GET.get("view", saved["preferred_view"])
        only = (
            _flag(request.GET["needs_attention_only"])
            if "needs_attention_only" in request.GET
            else saved["needs_attention_only"]
        )
        result = vehicle_checklist(organization, vehicle, view=view, needs_attention_only=only)
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except ChecklistQueryError as error:
        return _reply(error.status, error.code, correlation_id)
    except (DatabaseError, ValueError):
        logger.error("vehicle checklist unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    status = result.vehicle_status
    authors = _authors(organization, result.rows)
    return JsonResponse(
        {
            "status": "OK",
            "correlation_id": correlation_id,
            "vehicle_id": result.vehicle_id,
            "as_of": _stamp(result.as_of),
            "view": result.view,
            "needs_attention_only": result.needs_attention_only,
            "vehicle_status": {**status, "since": _stamped(status["since"])},
            "items": [_row(row, authors.get(row.latest_check_id)) for row in result.rows],
        }
    )


@never_cache
@require_http_methods(["GET", "POST"])
def view_preferences(request):
    if request.method == "GET":
        return _read_preferences(request)

    def call(user, correlation_id):
        body = _json_body(request, PREFERENCE_FIELDS)
        try:
            result = preferences.save_preferences(
                preferred_view=body["preferred_view"],
                needs_attention_only=body["needs_attention_only"],
                operation_id=_operation_id(body["operation_id"]),
                organization_id=str(user.organization_id),
                actor_id=str(user.pk),
                correlation_id=correlation_id,
                request_attempt_id=str(uuid.uuid4()),
            )
        except preferences.PreferenceError:
            raise Rejected(400, "MALFORMED_REQUEST", [("$", "INVALID_PREFERENCE")]) from None

        def acknowledge(stored):
            return {
                "preferred_view": stored.body["preferred_view"],
                "needs_attention_only": stored.body["needs_attention_only"],
            }

        return body["operation_id"], result, acknowledge

    return _run(request, "view preference", call)


def _operation_id(value):
    # The command layer requires a version 4 UUID; refuse anything else here as a 400.
    try:
        return str(human_operation_id(canonical_id(value)))
    except ValueError:
        raise Rejected(400, "MALFORMED_REQUEST", [("operation_id", "INVALID")]) from None


def _read_preferences(request):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    if request.GET:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
    try:
        saved = preferences.get_preferences(str(user.organization_id), user.pk)
    except DatabaseError:
        logger.error("view preferences unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    return JsonResponse({"status": "OK", "correlation_id": correlation_id, **saved})
