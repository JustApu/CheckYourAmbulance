"""Explicit administrator and empty-scope closure commands (SPEC 8.8, issue 152 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/152))."""

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import Vehicle
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies import service
from cya.deficiencies.commands import _authorizer, _lock_for_change, _operation_id, publish_changed
from cya.deficiencies.models import Deficiency, DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.transitions import (
    ACTIVE_STATUSES,
    CLOSED_ADMINISTRATOR,
    CLOSED_UNVERIFIABLE,
    OPEN,
    validate_transition,
)
from cya.operations.models import MAX_TEXT

CLOSE_ACTION = "explicit_deficiency_close"
CLOSE_COMMAND = "DEFICIENCY_ADMINISTRATOR_CLOSE"
REOPEN_COMMAND = "DEFICIENCY_ADMINISTRATOR_REOPEN"
EMPTY_SCOPE_CLOSE_ACTION = "close_empty_scope_deficiency"
EMPTY_SCOPE_CLOSE_COMMAND = "DEFICIENCY_EMPTY_SCOPE_CLOSE"


def _change(
    *,
    event_type,
    command_type,
    target_status,
    action,
    deficiency_id,
    explanation,
    expected_event_id,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    explanation = service.text(explanation, "explanation", MAX_TEXT)

    def build(actor):
        if event_type == "EMPTY_SCOPE_CLOSED":
            # Match correction's lock order so checklist publication and kit assignment cannot
            # change the scope between the empty-scope decision and its closure event.
            vehicle_id = (
                Deficiency.objects.filter(organization_id=organization_id, pk=deficiency_id)
                .values_list("vehicle_id", flat=True)
                .first()
            )
            if vehicle_id is not None:
                vehicle = (
                    Vehicle.objects.select_for_update()
                    .filter(organization_id=organization_id, pk=vehicle_id)
                    .first()
                )
                if vehicle is None:
                    raise DeficiencyError(
                        "CONFIGURATION_UNAVAILABLE", "deficiency vehicle is unavailable", status=503
                    )
                service.lock_checklist_templates(organization_id)
        projection, last = _lock_for_change(
            actor, action, organization_id, actor_id, deficiency_id, expected_event_id
        )
        if event_type == "ADMINISTRATOR_CLOSED" and projection.status not in ACTIVE_STATUSES:
            raise DeficiencyError("INVALID_TRANSITION", "only an active deficiency can be closed")
        if event_type == "ADMINISTRATOR_REOPENED" and projection.status not in (
            CLOSED_ADMINISTRATOR,
            CLOSED_UNVERIFIABLE,
        ):
            raise DeficiencyError(
                "INVALID_TRANSITION", "only an explicitly closed deficiency can be reopened"
            )
        try:
            validate_transition(projection.status, target_status)
        except ValueError:
            raise DeficiencyError(
                "INVALID_TRANSITION", "the status transition is not allowed"
            ) from None
        if event_type == "EMPTY_SCOPE_CLOSED":
            deficiency = Deficiency.objects.select_related("vehicle").get(
                organization_id=organization_id, pk=deficiency_id
            )
            if service.correction_scope(organization_id, deficiency, timezone.now()):
                raise DeficiencyError(
                    "SCOPE_AVAILABLE",
                    "the deficiency has current verification items and must follow correction",
                    path="deficiency",
                )
        event_id = new_id()
        now = timezone.now()

        def apply(receipt, current):
            event = DeficiencyEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                deficiency_id=deficiency_id,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type=event_type,
                from_status=projection.status,
                to_status=target_status,
                explanation=explanation,
                suspensions_ended=[],
                **service.stamp(receipt, current),
            )
            DeficiencyProjection.objects.filter(
                organization_id=organization_id, deficiency_id=deficiency_id
            ).update(status=target_status, last_applied_event=event)
            publish_changed(organization_id, deficiency_id, event_id)

        return Plan(
            response_body={
                "code": {
                    "ADMINISTRATOR_CLOSED": "DEFICIENCY_ADMINISTRATOR_CLOSED",
                    "ADMINISTRATOR_REOPENED": "DEFICIENCY_ADMINISTRATOR_REOPENED",
                    "EMPTY_SCOPE_CLOSED": "DEFICIENCY_EMPTY_SCOPE_CLOSED",
                }[event_type],
                "deficiency_id": deficiency_id,
                "event_id": event_id,
                "status": target_status,
                "suspensions_ended": [],
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            prior_snapshot={"schema_version": 1, "values": {"status": projection.status}},
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "event_id": event_id,
                    "event_type": event_type,
                    "status": target_status,
                    **(
                        {"verification_scope_count": 0}
                        if event_type == "EMPTY_SCOPE_CLOSED"
                        else {}
                    ),
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
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, action),
        prepare=build,
    )


def close_administrator(*, deficiency_id, explanation, expected_event_id, **context):
    return _change(
        event_type="ADMINISTRATOR_CLOSED",
        command_type=CLOSE_COMMAND,
        target_status=CLOSED_ADMINISTRATOR,
        action=CLOSE_ACTION,
        deficiency_id=deficiency_id,
        explanation=explanation,
        expected_event_id=expected_event_id,
        **context,
    )


def close_empty_scope(*, deficiency_id, explanation, expected_event_id, **context):
    """Close an active deficiency explicitly when no current verification item remains."""
    return _change(
        event_type="EMPTY_SCOPE_CLOSED",
        command_type=EMPTY_SCOPE_CLOSE_COMMAND,
        target_status=CLOSED_UNVERIFIABLE,
        action=EMPTY_SCOPE_CLOSE_ACTION,
        deficiency_id=deficiency_id,
        explanation=explanation,
        expected_event_id=expected_event_id,
        **context,
    )


def reopen_administrator(*, deficiency_id, explanation, expected_event_id, **context):
    return _change(
        event_type="ADMINISTRATOR_REOPENED",
        command_type=REOPEN_COMMAND,
        target_status=OPEN,
        action=CLOSE_ACTION,
        deficiency_id=deficiency_id,
        explanation=explanation,
        expected_event_id=expected_event_id,
        **context,
    )
