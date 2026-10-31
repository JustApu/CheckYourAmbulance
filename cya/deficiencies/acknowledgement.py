"""Audited escalation acknowledgements and their organization-level maximum (DEF-7)."""

from datetime import timedelta

from django.utils import timezone

from cya.accounts.fields import new_id
from cya.accounts.models import Organization
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies import service
from cya.deficiencies.commands import _authorizer, _lock_for_change, _operation_id, publish_changed
from cya.deficiencies.models import (
    ACKNOWLEDGEMENT_EVENT_TYPES,
    DeficiencyAcknowledgementRule,
    DeficiencyEvent,
    DeficiencyProjection,
)
from cya.deficiencies.service import DeficiencyError
from cya.operations.models import MAX_TEXT

ACK_ACTION = "acknowledge_escalation"
CONFIGURE_ACTION = "configure_schedules_thresholds_notifications"
ACK_COMMAND = "DEFICIENCY_ESCALATION_ACKNOWLEDGE"
RULE_COMMAND = "DEFICIENCY_ACKNOWLEDGEMENT_MAXIMUM_SET"
MAX_RULES = 1000
MAX_EVENTS = 5000


def _positive_days(value, path):
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise service.malformed(path, f"{path} must be a positive whole number")
    return value


def _history_values(rule):
    return {
        "rule_id": str(rule.pk),
        "revision": rule.revision,
        "maximum_days": rule.maximum_days,
        "recorded_at": rule.recorded_at,
        "recorded_by": str(rule.actor_user_id),
    }


def set_acknowledgement_maximum(
    *, maximum_days, organization_id, actor_id, operation_id, correlation_id, request_attempt_id
):
    """Append a new organization maximum; there is deliberately no default."""
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    maximum_days = _positive_days(maximum_days, "maximum_days")

    def build(_actor):
        Organization.objects.select_for_update().get(pk=organization_id)
        latest = (
            DeficiencyAcknowledgementRule.objects.filter(organization_id=organization_id)
            .order_by("-revision")
            .first()
        )
        if latest is not None and latest.maximum_days == maximum_days:
            raise DeficiencyError(
                "ACKNOWLEDGEMENT_MAXIMUM_UNCHANGED",
                "the acknowledgement maximum already has this value",
                path="maximum_days",
            )
        revision = latest.revision + 1 if latest else 1
        rule_id = new_id()

        def apply(receipt, current):
            DeficiencyAcknowledgementRule.objects.create(
                id=rule_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                operation_id=operation_id,
                revision=revision,
                maximum_days=maximum_days,
            )

        return Plan(
            response_body={
                "code": "DEFICIENCY_ACKNOWLEDGEMENT_MAXIMUM_SET",
                "rule_id": rule_id,
                "revision": revision,
            },
            apply=apply,
            response_status=201,
            target_type="ORGANIZATION",
            target_id=organization_id,
            prior_snapshot={"schema_version": 1, "values": {"revision": revision - 1}},
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "rule_id": rule_id,
                    "revision": revision,
                    "maximum_days": maximum_days,
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=RULE_COMMAND,
        request_digest=digest({"maximum_days": maximum_days}),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, CONFIGURE_ACTION),
        prepare=build,
    )


def acknowledge_escalation(
    *,
    deficiency_id,
    explanation,
    action_plan,
    period_days,
    expected_event_id,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Append an acknowledgement or renewal, bounded by the current administrator setting."""
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    deficiency_id = service.canonical(deficiency_id, "deficiency_id")
    expected_event_id = service.canonical(expected_event_id, "expected_event_id")
    explanation = service.text(explanation, "explanation", MAX_TEXT)
    action_plan = service.text(action_plan, "action_plan", MAX_TEXT)
    period_days = _positive_days(period_days, "period_days")

    def build(_actor):
        projection, last = _lock_for_change(
            _actor,
            ACK_ACTION,
            organization_id,
            actor_id,
            deficiency_id,
            expected_event_id,
        )
        service.require_active(projection)
        Organization.objects.select_for_update().get(pk=organization_id)
        now = timezone.now()
        rule = (
            DeficiencyAcknowledgementRule.objects.filter(
                organization_id=organization_id, recorded_at__lte=now
            )
            .order_by("-revision")
            .first()
        )
        if rule is None:
            raise DeficiencyError(
                "ACKNOWLEDGEMENT_MAXIMUM_UNCONFIGURED",
                "an administrator must configure an acknowledgement maximum first",
                path="period_days",
            )
        if period_days > rule.maximum_days:
            raise DeficiencyError(
                "ACKNOWLEDGEMENT_PERIOD_TOO_LONG",
                "period_days exceeds the administrator-configured maximum",
                path="period_days",
            )
        try:
            until = now + timedelta(days=period_days)
        except OverflowError:
            raise DeficiencyError(
                "ACKNOWLEDGEMENT_PERIOD_OUT_OF_RANGE",
                "period_days cannot be represented as a date",
                path="period_days",
            ) from None
        event_id = new_id()

        def apply(receipt, current):
            event = DeficiencyEvent.objects.create(
                id=event_id,
                organization_id=organization_id,
                deficiency_id=deficiency_id,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type=ACKNOWLEDGEMENT_EVENT_TYPES[0],
                from_status=last.to_status,
                to_status=last.to_status,
                explanation=explanation,
                action_plan=action_plan,
                acknowledgement_until=until,
                **service.stamp(receipt, current),
            )
            DeficiencyProjection.objects.filter(
                organization_id=organization_id, deficiency_id=deficiency_id
            ).update(last_applied_event=event)
            publish_changed(organization_id, deficiency_id, event_id)

        return Plan(
            response_body={
                "code": "DEFICIENCY_ESCALATION_ACKNOWLEDGED",
                "deficiency_id": deficiency_id,
                "event_id": event_id,
                "status": projection.status,
                "acknowledgement_until": until.isoformat(),
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
                    "event_type": ACKNOWLEDGEMENT_EVENT_TYPES[0],
                },
            },
            recorded_at=now,
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=ACK_COMMAND,
        request_digest=digest(
            {
                "deficiency_id": deficiency_id,
                "expected_event_id": expected_event_id,
                "explanation": explanation,
                "action_plan": action_plan,
                "period_days": period_days,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, ACK_ACTION),
        prepare=build,
    )


def acknowledgement_maximum(organization_id, as_of=None):
    rows = DeficiencyAcknowledgementRule.objects.filter(organization_id=organization_id)
    if as_of is not None:
        rows = rows.filter(recorded_at__lte=as_of)
    rows = list(rows.order_by("revision")[: MAX_RULES + 1])
    if len(rows) > MAX_RULES:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "the rule history is too long to return", status=503
        )
    history = [_history_values(row) for row in rows]
    return {"maximum": history[-1] if history else None, "history": history}


def acknowledgement_state(organization_id, deficiency_id, as_of=None):
    at = as_of or timezone.now()
    newest, events = service._events(
        organization_id,
        deficiency_id,
        as_of,
        MAX_EVENTS,
        types=ACKNOWLEDGEMENT_EVENT_TYPES,
    )
    current = events[-1] if events else None
    status = (
        "NONE"
        if current is None
        else ("ACTIVE" if current.acknowledgement_until > at else "EXPIRED")
    )
    history = []
    for index, event in enumerate(events):
        event_status = (
            "RENEWED"
            if index < len(events) - 1
            else ("ACTIVE" if event.acknowledgement_until > at else "EXPIRED")
        )
        history.append(
            {
                "event_id": str(event.pk),
                "recorded_at": event.recorded_at,
                "actor_user_id": str(event.actor_user_id),
                "acknowledgement_until": event.acknowledgement_until,
                "explanation": event.explanation,
                "action_plan": event.action_plan,
                "state": event_status,
            }
        )
    maximum = (
        DeficiencyAcknowledgementRule.objects.filter(
            organization_id=organization_id,
            recorded_at__lte=at,
        )
        .order_by("-revision")
        .first()
    )
    return {
        "deficiency_status": newest.to_status,
        "acknowledgement": None
        if current is None
        else {
            "status": status,
            "active": status == "ACTIVE",
            "event_id": str(current.pk),
            "recorded_at": current.recorded_at,
            "actor_user_id": str(current.actor_user_id),
            "acknowledgement_until": current.acknowledgement_until,
            "explanation": current.explanation,
            "action_plan": current.action_plan,
        },
        "maximum_days": maximum.maximum_days if maximum else None,
        "last_event_id": str(newest.pk),
        "history": history,
    }
