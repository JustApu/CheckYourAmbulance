"""Awaiting External Dependency and progress notes as audited commands (issue 149 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/149)).

SPEC 8.5: a deficiency waiting on a third party carries one current dependency record with a
mandatory explanation and an optional follow-up instant; every revision is an appended event, never
overwritten text, and nothing reminds anyone automatically. SPEC 8.1: a progress note is a
timestamped, attributed, append-only entry. Both follow the conventions of the re-grade and the
assignment commands: run_command owns the transaction, receipt and audit, the deficiency lock
serializes every writer, the caller names the newest event it saw (409 DEFICIENCY_CHANGED), and
the audit snapshot holds identifiers only, never the explanation or the note.

Dependency updates need update_external_dependency and a note needs add_progress_note; the two
SPEC 3.3 rows give the same roles (Crew, Supervisor and Administrator on any deficiency, a Supply
Officer only on a supply-related one). No command here accepts or changes the deadline, the
assignee or the escalation (SPEC 8.5), and none changes the vehicle's status (OPS-2).
"""

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.authorization.policy import ResourceContext
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies import service
from cya.deficiencies.commands import (
    _authorizer,
    _lock_for_change,
    _operation_id,
    publish_changed,
)
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.transitions import (
    AWAITING_EXTERNAL_DEPENDENCY,
    IN_PROGRESS,
    OPEN,
    TransitionRejected,
    validate_transition,
)
from cya.operations.models import MAX_TEXT

DEPENDENCY_ACTION = "update_external_dependency"
NOTE_ACTION = "add_progress_note"
ENTER_COMMAND_TYPE = "DEFICIENCY_DEPENDENCY_ENTER"
REVISE_COMMAND_TYPE = "DEFICIENCY_DEPENDENCY_REVISE"
END_COMMAND_TYPE = "DEFICIENCY_DEPENDENCY_END"
NOTE_COMMAND_TYPE = "DEFICIENCY_PROGRESS_NOTE"
RETURN_STATUSES = (OPEN, IN_PROGRESS)
CODES = {
    "DEPENDENCY_ENTERED": "DEFICIENCY_DEPENDENCY_ENTERED",
    "DEPENDENCY_REVISED": "DEFICIENCY_DEPENDENCY_REVISED",
    "DEPENDENCY_ENDED": "DEFICIENCY_DEPENDENCY_ENDED",
    "PROGRESS_NOTE": "DEFICIENCY_PROGRESS_NOTE_ADDED",
}


def _not_awaiting():
    return DeficiencyError(
        "NO_ACTIVE_DEPENDENCY",
        "the deficiency is not awaiting an external dependency",
        path="deficiency",
    )


def _status_after(event_type, projection, return_to, deficiency_id):
    """The status the event leaves the deficiency in, after the rules of its type.

    Entering needs an Open or In Progress deficiency (the SPEC 8.2 arrows); revising and ending
    need the one current dependency record; ending returns to the status the caller names or,
    when it names none, to the status recorded by the entering event.
    """
    status = projection.status
    if event_type == "PROGRESS_NOTE":
        return status
    if event_type == "DEPENDENCY_ENTERED":
        if status == AWAITING_EXTERNAL_DEPENDENCY:
            raise DeficiencyError(
                "DEPENDENCY_ALREADY_RECORDED",
                "the deficiency already awaits a dependency; revise its record instead",
                path="deficiency",
            )
        target = AWAITING_EXTERNAL_DEPENDENCY
    elif status != AWAITING_EXTERNAL_DEPENDENCY:
        raise _not_awaiting()
    elif event_type == "DEPENDENCY_REVISED":
        return status
    else:
        target = return_to
        if target is None:
            entered = service.entering_event(deficiency_id)
            if entered is None:
                raise DeficiencyError(
                    "CONFIGURATION_UNAVAILABLE", "dependency state is unavailable", status=503
                )
            target = entered.from_status
    try:
        validate_transition(status, target)
    except TransitionRejected:
        raise DeficiencyError(
            "INVALID_TRANSITION",
            "a dependency starts from an Open or In Progress deficiency and ends in one",
            path="deficiency",
        ) from None
    return target


def _change(
    event_type,
    command_type,
    *,
    deficiency_id,
    expected_event_id,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
    explanation=None,
    follow_up_at=None,
    return_to=None,
):
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    if event_type in ("DEPENDENCY_ENTERED", "DEPENDENCY_REVISED", "PROGRESS_NOTE"):
        path = "note" if event_type == "PROGRESS_NOTE" else "explanation"
        explanation = service.text(explanation, path, MAX_TEXT)
    if event_type in ("DEPENDENCY_ENTERED", "DEPENDENCY_REVISED"):
        follow_up_at = service.instant(follow_up_at, "follow_up_at")
    if event_type == "DEPENDENCY_ENDED" and return_to is not None:
        if return_to not in RETURN_STATUSES:
            raise service.malformed("return_to", "return_to must be OPEN or IN_PROGRESS")
    action = NOTE_ACTION if event_type == "PROGRESS_NOTE" else DEPENDENCY_ACTION

    def build(actor):
        projection, last = _lock_for_change(
            actor, action, organization_id, actor_id, deficiency_id, expected_event_id
        )
        service.require_active(projection)
        now = timezone.now()
        to_status = _status_after(event_type, projection, return_to, deficiency_id)
        if event_type == "DEPENDENCY_REVISED":
            record = service.dependency_state(organization_id, deficiency_id)["dependency"]
            if record is None:
                raise _not_awaiting()
            if (record["explanation"], record["follow_up_at"]) == (explanation, follow_up_at):
                raise DeficiencyError(
                    "DEPENDENCY_UNCHANGED",
                    "the dependency record already says this",
                    path="explanation",
                )
        # A follow-up date is a date to look at again, so one carried on an event lies in the
        # future, as the insert trigger also requires. A revision states the whole record, so it
        # moves the date or clears it.
        if follow_up_at is not None and follow_up_at <= now:
            raise DeficiencyError(
                "INVALID_FOLLOW_UP", "the follow-up date must be in the future", path="follow_up_at"
            )
        event_id = new_id()

        def apply(receipt, current):
            service.insert_dependency_event(
                receipt,
                current,
                organization_id=organization_id,
                event_id=event_id,
                last=last,
                event_type=event_type,
                to_status=to_status,
                explanation=explanation,
                follow_up_at=follow_up_at,
            )
            publish_changed(organization_id, deficiency_id, event_id)

        return Plan(
            response_body={
                "code": CODES[event_type],
                "deficiency_id": deficiency_id,
                "event_id": event_id,
                "status": to_status,
                "follow_up_at": follow_up_at.isoformat() if follow_up_at else None,
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
                    "to_status": to_status,
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=command_type,
        request_digest=digest(
            {
                "deficiency_id": deficiency_id,
                "expected_event_id": expected_event_id,
                "explanation": explanation,
                "follow_up_at": follow_up_at.isoformat() if follow_up_at else None,
                "return_to": return_to,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(
            organization_id,
            actor_id,
            action,
            context=ResourceContext(supply_related=True),
        ),
        prepare=build,
    )


def enter_dependency(
    *, deficiency_id, explanation, expected_event_id, follow_up_at=None, **context
):
    """Move an Open or In Progress deficiency to Awaiting External Dependency (SPEC 8.5, 8.2).

    The explanation is mandatory. The event records the status the deficiency leaves, which is
    where it returns by default when the dependency ends.
    """
    return _change(
        "DEPENDENCY_ENTERED",
        ENTER_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        explanation=explanation,
        follow_up_at=follow_up_at,
        expected_event_id=expected_event_id,
        **context,
    )


def revise_dependency(
    *, deficiency_id, explanation, expected_event_id, follow_up_at=None, **context
):
    """Replace the current dependency record's text and follow-up date; the old stays in history.

    A revision states the whole record, so leaving out follow_up_at clears the date.
    """
    return _change(
        "DEPENDENCY_REVISED",
        REVISE_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        explanation=explanation,
        follow_up_at=follow_up_at,
        expected_event_id=expected_event_id,
        **context,
    )


def end_dependency(*, deficiency_id, expected_event_id, return_to=None, **context):
    """Leave Awaiting External Dependency for Open or In Progress, as named (SPEC 8.2, 8.5).

    With no return_to the deficiency goes back to the status it was in before it began waiting.
    """
    return _change(
        "DEPENDENCY_ENDED",
        END_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        return_to=return_to,
        expected_event_id=expected_event_id,
        **context,
    )


def add_progress_note(*, deficiency_id, note, expected_event_id, **context):
    """Append a timestamped, attributed progress note to an active deficiency (SPEC 8.1)."""
    return _change(
        "PROGRESS_NOTE",
        NOTE_COMMAND_TYPE,
        deficiency_id=deficiency_id,
        explanation=note,
        expected_event_id=expected_event_id,
        **context,
    )
