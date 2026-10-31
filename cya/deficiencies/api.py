"""Deficiency HTTP endpoints over authenticated same-origin JSON (contracts section 4).

POST /api/v1/deficiencies reports one independently of any check. POST .../links and
.../link-removals add and remove an informational link. POST .../severity-changes re-grades a
deficiency's severity (the regrade_deficiency_severity permission) and GET .../severity returns its
current level and grade history. POST .../corrections records a repair and materializes the
verification scope at the correction instant. POST .../assignments, .../deadlines and .../work-starts assign a
deficiency, set or revise its deadline and start work on it (the assign_deficiency_deadline
permission), and GET .../assignment returns the assignee, deadline and status, now or as of an
instant. POST .../dependencies, .../dependency-revisions and .../dependency-exits enter, revise and
leave Awaiting External Dependency (the update_external_dependency permission) and GET .../dependency
returns the current record and its history; POST and GET .../progress-notes append and list
timestamped progress notes (the add_progress_note permission). GET /api/v1/deficiencies/suggestions
lists possible matches for a deficiency about to be reported; it reads only, and the crew member
decides. Actor, organization and roles come from the signed-in user, never the body (SPEC 3.2).
Errors use the record-check shape and codes; business rules live in cya.deficiencies.commands.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from django.db import DatabaseError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.authorization.backend import AuthorizationUnavailable
from cya.checks.api import Rejected, _read_body, _reply
from cya.core.audit_commands import CommandDenied, OperationConflict, OutcomeUnknown, RetryLater
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.deficiencies import (
    acknowledgement,
    age,
    closure,
    commands,
    dependency,
    expansion,
    extensions,
    service,
)
from cya.deficiencies.models import Deficiency, DeficiencyEvent
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.severity import active_levels

logger = logging.getLogger(__name__)

REPORT_REQUIRED = frozenset(
    {"operation_id", "vehicle_id", "target", "category_id", "severity", "summary", "description"}
)
REPORT_OPTIONAL = frozenset({"session_id", "related_deficiency_ids", "assignee_id", "deadline"})
LINK_FIELDS = frozenset({"operation_id", "related_deficiency_id"})
REGRADE_FIELDS = frozenset({"operation_id", "severity", "explanation", "expected_event_id"})
ASSIGN_FIELDS = frozenset({"operation_id", "assignee_id", "expected_event_id"})
DEADLINE_FIELDS = frozenset({"operation_id", "deadline", "expected_event_id"})
START_WORK_FIELDS = frozenset({"operation_id", "expected_event_id"})
CORRECTION_FIELDS = frozenset({"operation_id", "expected_event_id"})
DEPENDENCY_REQUIRED = frozenset({"operation_id", "explanation", "expected_event_id"})
DEPENDENCY_END_REQUIRED = frozenset({"operation_id", "expected_event_id"})
NOTE_FIELDS = frozenset({"operation_id", "note", "expected_event_id"})
EXTENSION_REQUEST_FIELDS = frozenset(
    {"operation_id", "reason", "proposed_deadline", "expected_event_id"}
)
EXTENSION_DECISION_FIELDS = frozenset(
    {"operation_id", "request_event_id", "decision", "explanation", "expected_event_id"}
)
AGE_RULE_FIELDS = frozenset(
    {
        "operation_id",
        "age_thresholds_days",
        "long_running_age_days",
        "long_running_extensions",
    }
)
ACKNOWLEDGEMENT_FIELDS = frozenset(
    {
        "operation_id",
        "explanation",
        "action_plan",
        "period_days",
        "expected_event_id",
    }
)
ACKNOWLEDGEMENT_MAXIMUM_FIELDS = frozenset({"operation_id", "maximum_days"})
ADMIN_CLOSE_FIELDS = frozenset({"operation_id", "explanation", "expected_event_id"})
EXPANSION_FIELDS = frozenset({"operation_id", "explanation", "expected_event_id", "items"})
ADDED_VERIFICATION_FIELDS = EXPANSION_FIELDS | {"deadline"}
# Body fields copied to the command as text, and those read as instants.
TEXT_FIELDS = (
    "explanation",
    "action_plan",
    "note",
    "return_to",
    "reason",
    "request_event_id",
    "decision",
)
INSTANT_FIELDS = ("follow_up_at", "proposed_deadline", "deadline")
# Command-result fields an acknowledgement repeats when the command carries them.
ACK_FIELDS = (
    "follow_up_at",
    "proposed_deadline",
    "decision",
    "governing_deadline",
    "acknowledgement_until",
    "suspensions_ended",
    "deadline",
    "items",
    "scope_revision",
    "scope_count",
)


def _json_body(request, required, optional=frozenset()):
    if request.content_type != "application/json":
        raise Rejected(400, "MALFORMED_REQUEST")
    try:
        body = json.loads(_read_body(request).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Rejected(400, "MALFORMED_REQUEST") from None
    if not isinstance(body, dict):
        raise Rejected(400, "MALFORMED_REQUEST")
    problems = [(name, "REQUIRED") for name in sorted(required) if name not in body]
    if set(body) - required - optional:
        problems.append(("$", "UNKNOWN_FIELD"))
    if problems:
        raise Rejected(400, "MALFORMED_REQUEST", problems)
    return body


def _instant(value, path):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise Rejected(400, "MALFORMED_REQUEST", [(path, "INVALID")]) from None


def _stamp(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _run(request, label, call):
    """Common authentication, error mapping and replay handling for the three POST commands."""
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        operation_id, result, acknowledge = call(user, correlation_id)
        if result.operation_reused:
            return _reply(409, "OPERATION_REUSED", correlation_id, [("operation_id", "REUSED")])
        return JsonResponse(
            {
                "status": "COMMITTED",
                "operation_id": operation_id,
                "correlation_id": result.correlation_id,
                "result": acknowledge(result),
            },
            status=result.status,
        )
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except DeficiencyError as error:
        errors = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, errors)
    except CommandDenied:
        return _reply(403, "FORBIDDEN", correlation_id)
    except OperationConflict:
        return _reply(409, "OPERATION_OWNED_BY_ANOTHER_ACTOR", correlation_id)
    except AuditPersistenceFailure:
        logger.error("%s not confirmed: audit unavailable", label)
        return _reply(503, "AUDIT_UNAVAILABLE", correlation_id)
    except AuthorizationUnavailable:
        logger.error("%s not confirmed: authorization unavailable", label)
        return _reply(503, "CONFIGURATION_UNAVAILABLE", correlation_id)
    except (RetryLater, OutcomeUnknown, DatabaseError):
        logger.error("%s not confirmed: retry with the same operation id", label)
        return _reply(503, "RETRY_LATER", correlation_id)


@never_cache
@require_http_methods(["POST"])
def deficiencies(request):
    def call(user, correlation_id):
        body = _json_body(request, REPORT_REQUIRED, REPORT_OPTIONAL)
        fields = {k: v for k, v in body.items() if k != "deadline"}
        result = commands.report_deficiency(
            deadline=_instant(body.get("deadline"), "deadline"),
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
            **fields,
        )

        def acknowledge(stored):
            row = (
                Deficiency.objects.filter(
                    organization_id=user.organization_id, pk=stored.body["deficiency_id"]
                )
                .values("recorded_at")
                .first()
            )
            if row is None:
                raise DatabaseError("committed deficiency could not be read back")
            return {
                "deficiency_id": stored.body["deficiency_id"],
                "creation_event_id": stored.body["creation_event_id"],
                "related_link_event_ids": stored.body["related_link_event_ids"],
                "server_recorded_at": _stamp(row["recorded_at"]),
            }

        return body["operation_id"], result, acknowledge

    return _run(request, "deficiency report", call)


def _link_view(request, deficiency_id, change, label):
    def call(user, correlation_id):
        body = _json_body(request, LINK_FIELDS)
        result = change(
            deficiency_id=deficiency_id,
            related_deficiency_id=body["related_deficiency_id"],
            operation_id=body["operation_id"],
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
        )
        return (
            body["operation_id"],
            result,
            lambda stored: {
                "link_event_id": stored.body["link_event_id"],
                "lower_deficiency_id": stored.body["lower_deficiency_id"],
                "upper_deficiency_id": stored.body["upper_deficiency_id"],
            },
        )

    return _run(request, label, call)


@never_cache
@require_http_methods(["POST"])
def links(request, deficiency_id):
    return _link_view(request, deficiency_id, commands.add_link, "deficiency link")


@never_cache
@require_http_methods(["POST"])
def link_removals(request, deficiency_id):
    return _link_view(request, deficiency_id, commands.remove_link, "deficiency link removal")


@never_cache
@require_http_methods(["GET"])
def suggestions(request):
    """Possible matches for a new deficiency; read-only, nothing is chosen or attached."""
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    try:
        allowed = {"vehicle_id", "item_id"}
        if set(request.GET) - allowed or "vehicle_id" not in request.GET:
            raise Rejected(400, "MALFORMED_REQUEST", [("$", "INVALID_FIELDS")])
        vehicle_id = service.canonical(request.GET["vehicle_id"], "vehicle_id")
        item_id = request.GET.get("item_id")
        if item_id is not None:
            item_id = service.canonical(item_id, "item_id")
        rows = service.suggest_related(str(user.organization_id), vehicle_id, item_id)
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except DeficiencyError as error:
        return _reply(error.status, error.code, correlation_id, [(error.path, error.code)])
    except DatabaseError:
        logger.error("deficiency suggestions unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    for row in rows:
        row["reported_at"] = _stamp(row["reported_at"])
    return JsonResponse({"status": "OK", "correlation_id": correlation_id, "suggestions": rows})


@never_cache
@require_http_methods(["GET"])
def severities(request):
    """The severity levels a reporter may choose now, least severe first (SPEC DEF-8).

    Each carries its label and concise criteria so the choice can be made with them in view.
    """
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    if request.GET:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
    try:
        levels = active_levels(str(user.organization_id), timezone.now())
    except DatabaseError:
        logger.error("deficiency severities unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    return JsonResponse(
        {
            "status": "OK",
            "correlation_id": correlation_id,
            "severities": [
                {
                    "code": level.code,
                    "label": level.label,
                    "description": level.description,
                    "rank": level.rank,
                }
                for level in levels
            ],
        }
    )


@never_cache
@require_http_methods(["POST"])
def severity_changes(request, deficiency_id):
    def call(user, correlation_id):
        body = _json_body(request, REGRADE_FIELDS)
        result = commands.regrade_severity(
            deficiency_id=deficiency_id,
            severity=body["severity"],
            explanation=body["explanation"],
            expected_event_id=body["expected_event_id"],
            operation_id=body["operation_id"],
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
        )

        def acknowledge(stored):
            row = (
                DeficiencyEvent.objects.filter(
                    organization_id=user.organization_id, pk=stored.body["event_id"]
                )
                .values("recorded_at")
                .first()
            )
            if row is None:
                raise DatabaseError("committed severity change could not be read back")
            return {
                "deficiency_id": stored.body["deficiency_id"],
                "event_id": stored.body["event_id"],
                "previous_severity": stored.body["previous_severity"],
                "severity": stored.body["severity"],
                "server_recorded_at": _stamp(row["recorded_at"]),
            }

        return body["operation_id"], result, acknowledge

    return _run(request, "deficiency severity change", call)


@never_cache
@require_http_methods(["GET"])
def severity(request, deficiency_id):
    """A deficiency's current severity level and every grade it has had (SPEC DEF-8).

    last_event_id is the event a re-grade must name as expected. Read-only; any signed-in member
    of the organization may read it, as the checklist already shows deficiencies to the crew.
    """
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    if request.GET:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
    try:
        deficiency_id = service.canonical(deficiency_id, "deficiency_id")
        view = service.severity_history(str(user.organization_id), deficiency_id)
    except DeficiencyError as error:
        errors = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, errors)
    except DatabaseError:
        logger.error("deficiency severity unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    for grade in view["history"]:
        grade["recorded_at"] = _stamp(grade["recorded_at"])
    return JsonResponse(
        {"status": "OK", "correlation_id": correlation_id, "deficiency_id": deficiency_id, **view}
    )


def _lifecycle_post(request, deficiency_id, label, command, fields):
    """One of the three lifecycle POSTs; they differ only in the fields their command takes."""

    def call(user, correlation_id):
        body = _json_body(request, fields)
        extra = {}
        if "assignee_id" in fields:
            extra["assignee_id"] = body["assignee_id"]
        if "deadline" in fields:
            extra["deadline"] = _instant(body["deadline"], "deadline")
        result = command(
            deficiency_id=deficiency_id,
            expected_event_id=body["expected_event_id"],
            operation_id=body["operation_id"],
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
            **extra,
        )

        def acknowledge(stored):
            row = (
                DeficiencyEvent.objects.filter(
                    organization_id=user.organization_id, pk=stored.body["event_id"]
                )
                .values("recorded_at")
                .first()
            )
            if row is None:
                raise DatabaseError(f"committed {label} could not be read back")
            return {
                "deficiency_id": stored.body["deficiency_id"],
                "event_id": stored.body["event_id"],
                "status": stored.body["status"],
                "assignee_id": stored.body["assignee_id"],
                "deadline": stored.body["deadline"],
                **{
                    key: stored.body[key]
                    for key in ("scope_revision", "scope_count")
                    if key in stored.body
                },
                "server_recorded_at": _stamp(row["recorded_at"]),
            }

        return body["operation_id"], result, acknowledge

    return _run(request, label, call)


@never_cache
@require_http_methods(["POST"])
def assignments(request, deficiency_id):
    return _lifecycle_post(
        request, deficiency_id, "deficiency assignment", commands.assign_deficiency, ASSIGN_FIELDS
    )


@never_cache
@require_http_methods(["POST"])
def deadlines(request, deficiency_id):
    return _lifecycle_post(
        request, deficiency_id, "deficiency deadline", commands.set_deadline, DEADLINE_FIELDS
    )


@never_cache
@require_http_methods(["POST"])
def work_starts(request, deficiency_id):
    return _lifecycle_post(
        request, deficiency_id, "deficiency work start", commands.start_work, START_WORK_FIELDS
    )


@never_cache
@require_http_methods(["POST"])
def corrections(request, deficiency_id):
    return _lifecycle_post(
        request,
        deficiency_id,
        "deficiency correction",
        commands.record_correction,
        CORRECTION_FIELDS,
    )


@never_cache
@require_http_methods(["GET"])
def assignment(request, deficiency_id):
    """A deficiency's assignee, deadline and status, now or as of an instant (DEF-4, AC-27).

    last_event_id is the event a lifecycle command must name as expected. Read-only; any
    signed-in member of the organization may read it, as the checklist already shows deficiencies
    to the crew. The only query parameter is as_of, a timezone-aware ISO 8601 instant.
    """
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    if set(request.GET) - {"as_of"} or len(request.GET.getlist("as_of")) > 1:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
    try:
        as_of = _instant(request.GET.get("as_of"), "as_of")
        if as_of is not None:
            service.instant(as_of, "as_of")
        deficiency_id = service.canonical(deficiency_id, "deficiency_id")
        view = service.assignment_state(str(user.organization_id), deficiency_id, as_of)
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except DeficiencyError as error:
        errors = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, errors)
    except DatabaseError:
        logger.error("deficiency assignment unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    view["deadline"] = None if view["deadline"] is None else _stamp(view["deadline"])
    for change in view["history"]:
        change["recorded_at"] = _stamp(change["recorded_at"])
        if change["deadline"] is not None:
            change["deadline"] = _stamp(change["deadline"])
    return JsonResponse(
        {"status": "OK", "correlation_id": correlation_id, "deficiency_id": deficiency_id, **view}
    )


def _event_post(request, deficiency_id, label, command, required, optional=frozenset()):
    """One of the dependency or progress-note POSTs; they differ only in their body fields."""

    def call(user, correlation_id):
        body = _json_body(request, required, optional)
        extra = {name: body[name] for name in TEXT_FIELDS if name in body}
        for name in INSTANT_FIELDS:
            if name in required or name in optional:
                extra[name] = _instant(body.get(name), name)
        if "period_days" in body:
            extra["period_days"] = body["period_days"]
        if "items" in body:
            extra["items"] = body["items"]
        result = command(
            deficiency_id=deficiency_id,
            expected_event_id=body["expected_event_id"],
            operation_id=body["operation_id"],
            organization_id=str(user.organization_id),
            actor_id=str(user.pk),
            correlation_id=correlation_id,
            request_attempt_id=str(uuid.uuid4()),
            **extra,
        )

        def acknowledge(stored):
            row = (
                DeficiencyEvent.objects.filter(
                    organization_id=user.organization_id, pk=stored.body["event_id"]
                )
                .values("recorded_at")
                .first()
            )
            if row is None:
                raise DatabaseError(f"committed {label} could not be read back")
            return {
                "deficiency_id": stored.body["deficiency_id"],
                "event_id": stored.body["event_id"],
                "status": stored.body["status"],
                **{name: stored.body[name] for name in ACK_FIELDS if name in stored.body},
                "server_recorded_at": _stamp(row["recorded_at"]),
            }

        return body["operation_id"], result, acknowledge

    return _run(request, label, call)


@never_cache
@require_http_methods(["POST"])
def dependencies(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "deficiency dependency",
        dependency.enter_dependency,
        DEPENDENCY_REQUIRED,
        frozenset({"follow_up_at"}),
    )


@never_cache
@require_http_methods(["POST"])
def dependency_revisions(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "deficiency dependency revision",
        dependency.revise_dependency,
        DEPENDENCY_REQUIRED,
        frozenset({"follow_up_at"}),
    )


@never_cache
@require_http_methods(["POST"])
def dependency_exits(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "deficiency dependency exit",
        dependency.end_dependency,
        DEPENDENCY_END_REQUIRED,
        frozenset({"return_to"}),
    )


def _history_read(request, deficiency_id, label, read):
    """Shared GET for the dependency record and the progress notes; as_of is the only parameter."""
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    if set(request.GET) - {"as_of"} or len(request.GET.getlist("as_of")) > 1:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
    try:
        as_of = _instant(request.GET.get("as_of"), "as_of")
        if as_of is not None:
            service.instant(as_of, "as_of")
        deficiency_id = service.canonical(deficiency_id, "deficiency_id")
        view = read(str(user.organization_id), deficiency_id, as_of)
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except DeficiencyError as error:
        errors = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, errors)
    except DatabaseError:
        logger.error("%s unavailable", label)
        return _reply(503, "RETRY_LATER", correlation_id)
    return view, correlation_id, deficiency_id


def _stamped(row, *names):
    for name in names:
        if row.get(name) is not None:
            row[name] = _stamp(row[name])


@never_cache
@require_http_methods(["GET"])
def dependency_record(request, deficiency_id):
    """The current Awaiting External Dependency record and its history (SPEC 8.5).

    The follow-up date is shown and never reminds anyone. last_event_id is the event a lifecycle
    command must name as expected. Read-only; any signed-in member of the organization may read
    it. The only query parameter is as_of, a timezone-aware ISO 8601 instant.
    """
    answer = _history_read(
        request, deficiency_id, "deficiency dependency", service.dependency_state
    )
    if isinstance(answer, JsonResponse):
        return answer
    view, correlation_id, deficiency_id = answer
    if view["dependency"] is not None:
        _stamped(view["dependency"], "follow_up_at", "entered_at", "revised_at")
    for event in view["history"]:
        _stamped(event, "recorded_at", "follow_up_at")
    return JsonResponse(
        {"status": "OK", "correlation_id": correlation_id, "deficiency_id": deficiency_id, **view}
    )


@never_cache
@require_http_methods(["GET", "POST"])
def progress_notes(request, deficiency_id):
    """GET lists the progress notes of a deficiency; POST appends one (SPEC 8.1)."""
    if request.method == "POST":
        return _event_post(
            request,
            deficiency_id,
            "deficiency progress note",
            dependency.add_progress_note,
            NOTE_FIELDS,
        )
    answer = _history_read(
        request, deficiency_id, "deficiency progress notes", service.progress_notes
    )
    if isinstance(answer, JsonResponse):
        return answer
    view, correlation_id, deficiency_id = answer
    for note in view["notes"]:
        _stamped(note, "recorded_at")
    return JsonResponse(
        {"status": "OK", "correlation_id": correlation_id, "deficiency_id": deficiency_id, **view}
    )


@never_cache
@require_http_methods(["POST"])
def extension_requests(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "deficiency extension request",
        extensions.request_extension,
        EXTENSION_REQUEST_FIELDS,
    )


@never_cache
@require_http_methods(["POST"])
def extension_decisions(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "deficiency extension decision",
        extensions.decide_extension,
        EXTENSION_DECISION_FIELDS,
        frozenset({"deadline"}),
    )


@never_cache
@require_http_methods(["GET"])
def deadline_record(request, deficiency_id):
    """The base and governing deadline, extensions, overdue state, age and highlight (SPEC DEF-5/6).

    Overdue, age and the highlight are derived from the events (and the age rule in force) at the
    instant answered for, now or as_of; nothing is stored. last_event_id is the event a lifecycle
    command must name as expected. Read-only; any signed-in member of the organization may read
    it. The only query parameter is as_of, a timezone-aware ISO 8601 instant.
    """
    answer = _history_read(request, deficiency_id, "deficiency deadline", service.extension_state)
    if isinstance(answer, JsonResponse):
        return answer
    view, correlation_id, deficiency_id = answer
    _stamped(view, "as_of", "reported_at", "base_deadline", "governing_deadline")
    for item in view["requests"]:
        _stamped(item, "requested_at", "proposed_deadline", "decided_at", "decided_deadline")
    return JsonResponse(
        {"status": "OK", "correlation_id": correlation_id, "deficiency_id": deficiency_id, **view}
    )


@never_cache
@require_http_methods(["POST"])
def escalation_acknowledgements(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "deficiency escalation acknowledgement",
        acknowledgement.acknowledge_escalation,
        ACKNOWLEDGEMENT_FIELDS,
    )


@never_cache
@require_http_methods(["GET"])
def escalation_acknowledgement(request, deficiency_id):
    answer = _history_read(
        request,
        deficiency_id,
        "deficiency escalation acknowledgement",
        acknowledgement.acknowledgement_state,
    )
    if isinstance(answer, JsonResponse):
        return answer
    view, correlation_id, deficiency_id = answer
    for event in view["history"]:
        _stamped(event, "recorded_at", "acknowledgement_until")
    if view["acknowledgement"] is not None:
        _stamped(view["acknowledgement"], "recorded_at", "acknowledgement_until")
    return JsonResponse(
        {"status": "OK", "correlation_id": correlation_id, "deficiency_id": deficiency_id, **view}
    )


@never_cache
@require_http_methods(["POST"])
def administrator_closures(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "administrator deficiency closure",
        closure.close_administrator,
        ADMIN_CLOSE_FIELDS,
    )


@never_cache
@require_http_methods(["POST"])
def empty_scope_closures(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "empty-scope deficiency closure",
        closure.close_empty_scope,
        ADMIN_CLOSE_FIELDS,
    )


@never_cache
@require_http_methods(["POST"])
def scope_expansions(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "verification scope expansion",
        expansion.expand_scope,
        EXPANSION_FIELDS,
    )


@never_cache
@require_http_methods(["POST"])
def added_verifications(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "added verification",
        expansion.add_verification,
        ADDED_VERIFICATION_FIELDS,
    )


@never_cache
@require_http_methods(["POST"])
def administrator_reopenings(request, deficiency_id):
    return _event_post(
        request,
        deficiency_id,
        "administrator deficiency reopening",
        closure.reopen_administrator,
        ADMIN_CLOSE_FIELDS,
    )


@never_cache
@require_http_methods(["GET", "POST"])
def acknowledgement_maximum(request):
    if request.method == "POST":

        def call(user, correlation_id):
            body = _json_body(request, ACKNOWLEDGEMENT_MAXIMUM_FIELDS)
            result = acknowledgement.set_acknowledgement_maximum(
                maximum_days=body["maximum_days"],
                organization_id=str(user.organization_id),
                actor_id=str(user.pk),
                operation_id=body["operation_id"],
                correlation_id=correlation_id,
                request_attempt_id=str(uuid.uuid4()),
            )
            return (
                body["operation_id"],
                result,
                lambda stored: {
                    "rule_id": stored.body["rule_id"],
                    "revision": stored.body["revision"],
                },
            )

        return _run(request, "deficiency acknowledgement maximum", call)

    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    if set(request.GET) - {"as_of"} or len(request.GET.getlist("as_of")) > 1:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
    try:
        as_of = _instant(request.GET.get("as_of"), "as_of")
        if as_of is not None:
            service.instant(as_of, "as_of")
        view = acknowledgement.acknowledgement_maximum(str(user.organization_id), as_of)
    except Rejected as error:
        return _reply(error.status, "MALFORMED_REQUEST", correlation_id, error.field_errors)
    except DeficiencyError as error:
        errors = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, errors)
    except DatabaseError:
        logger.error("deficiency acknowledgement maximum unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    for revision in view["history"]:
        revision["recorded_at"] = _stamp(revision["recorded_at"])
    # maximum aliases the last row in history, so stamping history already formatted it.
    return JsonResponse({"status": "OK", "correlation_id": correlation_id, **view})


@never_cache
@require_http_methods(["GET", "POST"])
def age_rule(request):
    """GET the age rule in force (now or as_of) with its history; POST appends a revision.

    Setting the rule is the administrator's configure_schedules_thresholds_notifications
    permission; reading it needs only a signed-in member. Until a revision is recorded nothing is
    highlighted (the owner's decision of 2026-10-27), and there is no age-based notification.
    """
    if request.method == "POST":

        def call(user, correlation_id):
            body = _json_body(request, AGE_RULE_FIELDS)
            result = age.set_age_rule(
                age_thresholds_days=body["age_thresholds_days"],
                long_running_age_days=body["long_running_age_days"],
                long_running_extensions=body["long_running_extensions"],
                organization_id=str(user.organization_id),
                actor_id=str(user.pk),
                operation_id=body["operation_id"],
                correlation_id=correlation_id,
                request_attempt_id=str(uuid.uuid4()),
            )

            def acknowledge(stored):
                return {"rule_id": stored.body["rule_id"], "revision": stored.body["revision"]}

            return body["operation_id"], result, acknowledge

        return _run(request, "deficiency age rule", call)
    correlation_id = str(uuid.uuid4())
    user = request.cya_user
    if user is None:
        return _reply(401, "AUTHENTICATION_REQUIRED", correlation_id)
    if set(request.GET) - {"as_of"} or len(request.GET.getlist("as_of")) > 1:
        return _reply(400, "MALFORMED_REQUEST", correlation_id, [("$", "INVALID_FIELDS")])
    try:
        as_of = _instant(request.GET.get("as_of"), "as_of")
        if as_of is not None:
            service.instant(as_of, "as_of")
        view = age.age_rule_view(str(user.organization_id), as_of)
    except Rejected as error:
        return _reply(error.status, error.code, correlation_id, error.field_errors)
    except DeficiencyError as error:
        errors = [(error.path, error.code)] if error.path else []
        return _reply(error.status, error.code, correlation_id, errors)
    except DatabaseError:
        logger.error("deficiency age rule unavailable")
        return _reply(503, "RETRY_LATER", correlation_id)
    for revision in view["history"]:
        revision["recorded_at"] = _stamp(revision["recorded_at"])
    if view["rule"] is not None:
        view["rule"]["recorded_at"] = _stamp(view["rule"]["recorded_at"])
    return JsonResponse({"status": "OK", "correlation_id": correlation_id, **view})
