"""The organization's inspection resume limit (SPEC 6.3 SES-8, OQ-81; issue #256 Part E).

An Administrator sets it in whole hours from 1 to 24 (default 6). Each revision is an
append-only row; the latest applies. A session captures the value when it opens, so changing the
limit never moves an existing session's deadline.

Serialization. Setting the limit takes the organization row lock exclusively. Opening a session
takes the same row in share mode first (cya.inspections.commands._execute), before the actor's
lock and the vehicle's, so opens run concurrently with each other and the value an open reads is
the one committed when its share lock was granted. The database repeats the rule: a session
header must capture the organization's latest revision (cya_inspections 0007).
"""

from cya.accounts.fields import new_id
from cya.authorization.persistence import digest
from cya.core.audit_commands import Plan, run_command
from cya.inspections.models import (
    DEFAULT_RESUME_LIMIT_HOURS,
    MAX_RESUME_LIMIT_HOURS,
    SessionResumeLimit,
)

LIMIT_COMMAND = "SESSION_RESUME_LIMIT_SET"
LIMIT_ACTION = "set_inspection_resume_limit"


class ResumeLimitError(ValueError):
    def __init__(self, message, *, code, status, path=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.path = path


def resume_limit_hours(organization_id):
    """The current resume limit in hours: the latest revision, else the default."""
    latest = (
        SessionResumeLimit.objects.filter(organization_id=organization_id)
        .order_by("-revision")
        .values_list("hours", flat=True)
        .first()
    )
    return DEFAULT_RESUME_LIMIT_HOURS if latest is None else latest


def set_resume_limit(
    *, hours, organization_id, actor_id, operation_id, correlation_id, request_attempt_id
):
    """An Administrator sets the resume limit for sessions opened from now on (SES-8)."""
    from cya.authorization.backend import canonical_id
    from cya.kits import definitions

    canonical_id(organization_id)
    canonical_id(actor_id)
    if type(hours) is not int or not 1 <= hours <= MAX_RESUME_LIMIT_HOURS:
        raise ResumeLimitError(
            f"hours must be a whole number from 1 to {MAX_RESUME_LIMIT_HOURS}",
            code="MALFORMED_REQUEST",
            status=400,
            path="hours",
        )
    org = organization_id

    def build(actor):
        previous = resume_limit_hours(org)
        if previous == hours:
            raise ResumeLimitError(
                f"the limit is already {hours} hours", code="NO_CHANGE", status=409
            )
        revision = SessionResumeLimit.objects.filter(organization_id=org).count() + 1
        limit_id = new_id()

        def apply(receipt, current):
            SessionResumeLimit.objects.create(
                id=limit_id,
                organization_id=org,
                revision=revision,
                hours=hours,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )

        body = {"code": LIMIT_COMMAND, "revision": revision, "hours": hours}
        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            prior_snapshot={"schema_version": 1, "values": {"hours": previous}},
            new_snapshot={"schema_version": 1, "values": body},
        )

    return run_command(
        organization_id=org,
        operation_id=operation_id,
        command_type=LIMIT_COMMAND,
        request_digest=digest({"hours": hours}),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        # The organization row lock, taken exclusively, serializes revision numbers and waits for
        # every session open holding it in share mode.
        authorize=definitions.authorizer(org, actor_id, LIMIT_ACTION, lock_organization=True),
        prepare=build,
    )
