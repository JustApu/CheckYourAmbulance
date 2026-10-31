"""Deadline extension requests and decisions as audited commands (issue 150 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/150); SPEC DEF-5).

A crew member (or a Supply Officer on a deficiency assigned to them) asks for more time with a
reason and a proposed date; a supervisor approves it, denies it or chooses a different date, with
an explanation. Each request and each decision is an appended event, extensions are unlimited, and
the date of the newest approval governs overdue notices until a new deadline revision supersedes
it (cya.deficiencies.deadlines). Both commands follow the re-grade conventions: run_command owns
the transaction, receipt and audit, the deficiency lock serializes every writer, the caller names
the newest event it saw (409 DEFICIENCY_CHANGED), and the audit snapshot holds identifiers and the
outcome only, never the reason or the explanation.

Requesting needs request_extension and deciding needs decide_extension (SPEC 3.3; roles combine,
SPEC 3.2). No command here changes the status, the assignee or the deadline revision itself.
"""

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.authorization.policy import ResourceContext
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies import deadlines, service
from cya.deficiencies.commands import (
    _authorizer,
    _lock_for_change,
    _operation_id,
    publish_changed,
)
from cya.deficiencies.models import DECISIONS
from cya.deficiencies.service import DeficiencyError
from cya.operations.models import MAX_TEXT

REQUEST_ACTION = "request_extension"
DECIDE_ACTION = "decide_extension"
REQUEST_COMMAND_TYPE = "DEFICIENCY_EXTENSION_REQUEST"
DECIDE_COMMAND_TYPE = "DEFICIENCY_EXTENSION_DECIDE"


def _unprocessable(code, message, path):
    return DeficiencyError(code, message, path=path)


def _later_than_governing(date, governing, now, path):
    """A requested or decided date must lie in the future and after the deadline now governing."""
    if date <= now:
        raise _unprocessable("INVALID_EXTENSION_DATE", "the date must be in the future", path)
    if governing is None or date <= governing:
        raise _unprocessable(
            "INVALID_EXTENSION_DATE",
            "the date must be later than the deadline now governing",
            path,
        )


def _iso(value):
    return value.isoformat() if value else None


def _run(
    event_type,
    command_type,
    action,
    context,
    *,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    deficiency_id,
    expected_event_id,
    request_digest,
    check,
):
    """Shared transaction: lock, stale check, the command's own rules, then the one event."""

    def build(actor):
        projection, last = _lock_for_change(
            actor, action, organization_id, actor_id, deficiency_id, expected_event_id
        )
        service.require_active(projection)
        now = timezone.now()
        _newest, events = service.deadline_events(organization_id, deficiency_id)
        fields, snapshot = check(service.replay_deadlines(events), now)
        event_id = new_id()

        def apply(receipt, current):
            service.insert_extension_event(
                receipt,
                current,
                organization_id=organization_id,
                event_id=event_id,
                last=last,
                event_type=event_type,
                **fields,
            )
            publish_changed(organization_id, deficiency_id, event_id)

        return Plan(
            response_body={
                "code": f"DEFICIENCY_{event_type}",
                "deficiency_id": deficiency_id,
                "event_id": event_id,
                "status": projection.status,
                **snapshot["response"],
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "deficiency_id": deficiency_id,
                    "event_id": event_id,
                    "event_type": event_type,
                    **snapshot["audit"],
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=command_type,
        request_digest=digest(request_digest),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, action, context=context),
        prepare=build,
    )


def request_extension(
    *,
    deficiency_id,
    reason,
    proposed_deadline,
    expected_event_id,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Ask for more time on a deficiency that has a deadline (DEF-5); extensions are unlimited."""
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    reason = service.text(reason, "reason", MAX_TEXT)
    proposed = service.instant(proposed_deadline, "proposed_deadline")
    if proposed is None:
        raise service.malformed("proposed_deadline", "proposed_deadline must be an instant")

    def check(state, now):
        if state["governing_deadline"] is None:
            raise _unprocessable(
                "NO_DEADLINE", "the deficiency has no deadline to extend", "deficiency"
            )
        _later_than_governing(proposed, state["governing_deadline"], now, "proposed_deadline")
        fields = {"explanation": reason, "proposed_deadline": proposed}
        return fields, {
            "response": {"proposed_deadline": proposed.isoformat()},
            "audit": {},
        }

    return _run(
        "EXTENSION_REQUESTED",
        REQUEST_COMMAND_TYPE,
        REQUEST_ACTION,
        # Before the lock the assignee is unknown, so the most permissive fact is assumed here and
        # the real one is decided from the locked projection.
        ResourceContext(supply_related=True, assignee_id=actor_id),
        organization_id=organization_id,
        actor_id=actor_id,
        operation_id=operation_id,
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        deficiency_id=deficiency_id,
        expected_event_id=expected_event_id,
        request_digest={
            "deficiency_id": deficiency_id,
            "expected_event_id": expected_event_id,
            "reason": reason,
            "proposed_deadline": proposed.isoformat(),
        },
        check=check,
    )


def decide_extension(
    *,
    deficiency_id,
    request_event_id,
    decision,
    explanation,
    expected_event_id,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    deadline=None,
):
    """Approve, deny or change the date of one pending request, with an explanation (DEF-5).

    Approval governs the requested date; a different date is chosen by the decider and is the only
    outcome that takes a date; a denial takes none. The request must belong to the deadline now in
    force: a later deadline revision supersedes it.
    """
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    request_event_id = service.canonical(request_event_id, "request_event_id")
    if decision not in DECISIONS:
        raise service.malformed("decision", f"decision must be one of {list(DECISIONS)}")
    explanation = service.text(explanation, "explanation", MAX_TEXT)
    chosen = service.instant(deadline, "deadline")
    if (decision == "DIFFERENT_DATE") != (chosen is not None):
        raise service.malformed(
            "deadline", "deadline is required for DIFFERENT_DATE and accepted for nothing else"
        )

    def check(state, now):
        request = next(
            (r for r in state["requests"] if r["request_event_id"] == request_event_id), None
        )
        if request is None:
            raise _unprocessable(
                "REQUEST_NOT_FOUND",
                "no extension request with this id on this deficiency",
                "request_event_id",
            )
        if request["status"] != deadlines.PENDING:
            raise _unprocessable(
                "REQUEST_ALREADY_DECIDED",
                "this extension request has already been decided",
                "request_event_id",
            )
        if request["superseded"]:
            raise _unprocessable(
                "REQUEST_SUPERSEDED",
                "a new deadline was set after this request; ask again against that deadline",
                "request_event_id",
            )
        date = request["proposed_deadline"] if decision == "APPROVED" else chosen
        if date is not None:
            _later_than_governing(date, state["governing_deadline"], now, "deadline")
        fields = {
            "explanation": explanation,
            "decision": decision,
            "request_event_id": request_event_id,
            "deadline": date,
        }
        return fields, {
            "response": {"decision": decision, "governing_deadline": _iso(date)},
            "audit": {"decision": decision, "request_event_id": request_event_id},
        }

    return _run(
        "EXTENSION_DECIDED",
        DECIDE_COMMAND_TYPE,
        DECIDE_ACTION,
        ResourceContext(),
        organization_id=organization_id,
        actor_id=actor_id,
        operation_id=operation_id,
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        deficiency_id=deficiency_id,
        expected_event_id=expected_event_id,
        request_digest={
            "deficiency_id": deficiency_id,
            "expected_event_id": expected_event_id,
            "request_event_id": request_event_id,
            "decision": decision,
            "explanation": explanation,
            "deadline": _iso(chosen),
        },
        check=check,
    )
