"""Authenticated, bounded JSON commands for the crew seal workflow."""

import uuid

from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.checks.api import Rejected
from cya.deficiencies.api import _json_body, _run
from cya.seals.commands import SealConflict, SealError, break_seal, request_seal_audit, verify_seal
from cya.seals.reseal import reseal

SUBJECT = frozenset({"operation_id", "vehicle_id"})
OPTIONAL_SUBJECT = frozenset({"kit_id", "slot_id", "expected_tip_id"})
ACTIONS = {
    "break": (break_seal, SUBJECT | {"reason"}),
    "audit": (request_seal_audit, SUBJECT),
    "verify": (verify_seal, SUBJECT | {"marking", "attested"}),
    "reseal": (
        reseal,
        SUBJECT | {"marking", "inspected_attestation", "written_attestation", "observations"},
    ),
}


@never_cache
@require_http_methods(["POST"])
def command(request, action):
    """Actor and organization are from the signed-in user, never the submitted body."""

    def call(user, correlation_id):
        if action not in ACTIONS:
            raise Rejected(404, "NOT_FOUND")
        handler, required = ACTIONS[action]
        body = _json_body(request, required, OPTIONAL_SUBJECT)
        try:
            result = handler(
                **body,
                organization_id=str(user.organization_id),
                actor_id=str(user.pk),
                correlation_id=correlation_id,
                request_attempt_id=str(uuid.uuid4()),
            )
        except SealConflict:
            raise Rejected(409, "SEAL_CHANGED") from None
        except (SealError, ValueError):
            raise Rejected(422, "INVALID_SEAL_REQUEST") from None
        return body["operation_id"], result, lambda stored: stored.body

    return _run(request, "seal command", call)
