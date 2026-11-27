"""Authenticated, bounded JSON commands for the crew seal workflow."""

import uuid

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.backend import canonical_id
from cya.checks.api import Rejected, _reply
from cya.checks.commands import CheckError
from cya.deficiencies.api import _json_body, _run
from cya.seals import readers
from cya.seals.commands import (
    SealAuditOpen,
    SealConflict,
    SealError,
    SealRefusal,
    break_seal,
    request_seal_audit,
    start_inspection,
    verify_seal,
)
from cya.seals.locking import SealLockError
from cya.seals.reseal import reseal
from cya.seals.scope import SealScopeError

SUBJECT = frozenset({"operation_id", "vehicle_id"})
OPTIONAL_SUBJECT = frozenset({"kit_id", "slot_id", "expected_tip_id"})
ACTIONS = {
    "break": (break_seal, SUBJECT | {"reason"}),
    "audit": (request_seal_audit, SUBJECT),
    "start": (start_inspection, SUBJECT),
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
        optional = OPTIONAL_SUBJECT | (
            {
                "start_operation_id",
                "nested_substitution_ids",
                "kit_count_checks",
                "kit_presence_checks",
                "paper_entry_batch_id",
            }
            if action == "reseal"
            # SPEC 8.5.0 LATE-7: a break, a verification or a reseal is entered from paper.
            else {"paper_entry_batch_id"}
            if action in ("break", "verify")
            else set()
        )
        body = _json_body(request, required, optional)
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
        except SealAuditOpen:
            raise Rejected(422, "AUDIT_REQUIRES_SEAL") from None
        except CheckError as error:
            # The paper-entry batch refusals (NOT_FOUND, PAPER_BATCH_MISMATCH, MALFORMED_REQUEST).
            raise Rejected(
                error.status, error.code, [(error.path, error.code)] if error.path else []
            ) from None
        except SealRefusal as error:
            raise Rejected(422, error.code) from None
        except (SealLockError, SealScopeError):
            raise Rejected(503, "CONFIGURATION_UNAVAILABLE") from None
        except (SealError, ValueError):
            raise Rejected(422, "INVALID_SEAL_REQUEST") from None
        return body["operation_id"], result, lambda stored: stored.body

    return _run(request, "seal command", call)


@never_cache
@require_http_methods(["GET"])
def vehicle_subjects(request, vehicle_id):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        canonical_id(vehicle_id)
    except ValueError:
        return _reply(404, "NOT_FOUND", correlation_id)
    try:
        return JsonResponse({"subjects": readers.subjects(str(user.organization_id), vehicle_id)})
    except LookupError:
        return _reply(404, "NOT_FOUND", correlation_id)
    except (DatabaseError, ValueError):
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)


@never_cache
@require_http_methods(["GET"])
def inspection_scope(request, vehicle_id, kind, subject_id):
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        canonical_id(vehicle_id)
        canonical_id(subject_id)
        if kind not in ("kit", "slot"):
            raise ValueError
    except ValueError:
        return _reply(404, "NOT_FOUND", correlation_id)
    try:
        return JsonResponse(
            readers.inspection_scope(str(user.organization_id), vehicle_id, kind, subject_id)
        )
    except LookupError:
        return _reply(404, "NOT_FOUND", correlation_id)
    except (DatabaseError, ValueError):
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)
