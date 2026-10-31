"""The administrator's age-highlight rule as an audited, versioned command (issue 150 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/150); DEF-6).

set_age_rule appends the next revision of the organization's rule: up to five ascending age
thresholds in days that a dashboard marks, and the "long-running" test (an age in days, an
extension-request count, or both; either may be left off). Nothing is hard-coded: until an
administrator records a revision nothing is highlighted, and a revision with no thresholds and both
tests off switches highlighting off again. Every revision is kept (AUD-4) and the one recorded by an
instant applies at that instant, so the as-of reader answers for the past. There is no age-based
notification (DEF-6).

Like the other administrator commands it runs through run_command and the organization row
serializes administrators, so revision numbers count up without gaps.
"""

from cya.accounts.fields import new_id
from cya.accounts.models import Organization
from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.core.audit_commands import Plan, run_command
from cya.deficiencies import service
from cya.deficiencies.commands import _authorizer, _operation_id
from cya.deficiencies.models import MAX_AGE_DAYS, MAX_AGE_THRESHOLDS, DeficiencyAgeRule
from cya.deficiencies.service import DeficiencyError

ACTION = "configure_schedules_thresholds_notifications"
COMMAND_TYPE = "DEFICIENCY_AGE_RULE_SET"
MAX_LONG_RUNNING_EXTENSIONS = 1000


def _whole(value, path, low, high):
    if type(value) is not int or not low <= value <= high:
        raise service.malformed(path, f"{path} must be a whole number from {low} to {high}")
    return value


def _thresholds(value):
    if not isinstance(value, list) or len(value) > MAX_AGE_THRESHOLDS:
        raise service.malformed(
            "age_thresholds_days",
            f"age_thresholds_days must be a list of at most {MAX_AGE_THRESHOLDS}",
        )
    days = [_whole(item, "age_thresholds_days", 1, MAX_AGE_DAYS) for item in value]
    if days != sorted(set(days)):
        raise service.malformed(
            "age_thresholds_days", "age_thresholds_days must be strictly ascending"
        )
    return days


def rule_values(rule):
    """The fields of one revision, as the reader and the API show them."""
    return {
        "revision": rule.revision,
        "age_thresholds_days": list(rule.age_thresholds_days),
        "long_running_age_days": rule.long_running_age_days,
        "long_running_extensions": rule.long_running_extensions,
        "recorded_at": rule.recorded_at,
    }


def set_age_rule(
    *,
    age_thresholds_days,
    long_running_age_days,
    long_running_extensions,
    organization_id,
    actor_id,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Append the next revision of the organization's age-highlight rule (administrators only)."""
    for value in (organization_id, actor_id):
        canonical_id(value)
    _operation_id(operation_id)
    days = _thresholds(age_thresholds_days)
    if long_running_age_days is not None:
        _whole(long_running_age_days, "long_running_age_days", 1, MAX_AGE_DAYS)
    if long_running_extensions is not None:
        _whole(long_running_extensions, "long_running_extensions", 1, MAX_LONG_RUNNING_EXTENSIONS)

    def build(_actor):
        Organization.objects.select_for_update().get(pk=organization_id)
        latest = (
            DeficiencyAgeRule.objects.filter(organization_id=organization_id)
            .order_by("-revision")
            .first()
        )
        wanted = (days, long_running_age_days, long_running_extensions)
        if (latest is None and wanted == ([], None, None)) or (
            latest is not None
            and wanted
            == (
                list(latest.age_thresholds_days),
                latest.long_running_age_days,
                latest.long_running_extensions,
            )
        ):
            raise DeficiencyError(
                "AGE_RULE_UNCHANGED", "the age rule already says this", path="age_thresholds_days"
            )
        revision = latest.revision + 1 if latest else 1
        rule_id = new_id()

        def apply(receipt, current):
            DeficiencyAgeRule.objects.create(
                id=rule_id,
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                operation_id=operation_id,
                revision=revision,
                age_thresholds_days=days,
                long_running_age_days=long_running_age_days,
                long_running_extensions=long_running_extensions,
            )

        return Plan(
            response_body={
                "code": "DEFICIENCY_AGE_RULE_SET",
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
                    "age_thresholds_days": days,
                    "long_running_age_days": long_running_age_days,
                    "long_running_extensions": long_running_extensions,
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=COMMAND_TYPE,
        request_digest=digest(
            {
                "age_thresholds_days": days,
                "long_running_age_days": long_running_age_days,
                "long_running_extensions": long_running_extensions,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=_authorizer(organization_id, actor_id, ACTION),
        prepare=build,
    )


MAX_AGE_RULES = 1000


def age_rule_view(organization_id, as_of=None):
    """The revision in force at as_of (now when omitted) and every revision recorded by then.

    Bounded: past the cap the answer is refused, never truncated. An organization with no revision
    gets rule null: nothing is highlighted (DEF-6, owner decision of 2026-10-27).
    """
    rows = DeficiencyAgeRule.objects.filter(organization_id=organization_id)
    if as_of is not None:
        rows = rows.filter(recorded_at__lte=as_of)
    rows = list(rows.order_by("revision")[: MAX_AGE_RULES + 1])
    if len(rows) > MAX_AGE_RULES:
        raise DeficiencyError(
            "CONFIGURATION_UNAVAILABLE", "the rule history is too long to return", status=503
        )
    history = [rule_values(row) for row in rows]
    return {"rule": dict(history[-1]) if history else None, "history": history}
