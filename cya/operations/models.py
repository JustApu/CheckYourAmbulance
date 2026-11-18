"""Operational status event stream and status-change requests (SPEC section 9, OPS-1..OPS-6).

Both tables are append-only (A) in docs/data-model-contracts.md section 2. A vehicle's
current status is its latest vehicle_status_event; there is deliberately no status column on
vehicle. Migration 0002 installs the organization-pair foreign keys, UUID/role checks, the
append-only triggers and the chain/attribution triggers. Writers go through
cya.operations.commands; readers through cya.operations.status.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import Vehicle, non_blank, organization_fk

STATUSES = ("IN_SERVICE", "LIMITED_SERVICE", "OUT_OF_SERVICE")
REQUEST_ACTIONS = ("REQUEST", "APPROVE", "DENY")
MAX_TEXT = 4000


class Evidence(models.Model):
    """Receipt, recorded time and actor shared by every historical row (contracts section 2)."""

    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    actor_roles_at_time = models.JSONField(default=list)

    class Meta:
        abstract = True


class VehicleStatusEvent(Evidence):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    sequence = models.PositiveBigIntegerField()
    prior_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=20)
    explanation = models.CharField(max_length=MAX_TEXT)
    restrictions = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001

    class Meta:
        db_table = "vehicle_status_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="status_event_org_id"),
            models.UniqueConstraint(fields=["vehicle", "sequence"], name="status_event_sequence"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="status_event_seq"),
            models.CheckConstraint(
                condition=models.Q(status__in=STATUSES), name="status_event_status"
            ),
            models.CheckConstraint(
                condition=non_blank("explanation"), name="status_event_explanation"
            ),
            # Limited service carries free-text restrictions (OPS-3); other statuses carry none.
            models.CheckConstraint(
                condition=(
                    # A NULL CHECK result counts as satisfied, so require the column explicitly.
                    models.Q(
                        status="LIMITED_SERVICE",
                        restrictions__isnull=False,
                        restrictions__regex=r"\S",
                    )
                    | (~models.Q(status="LIMITED_SERVICE") & models.Q(restrictions__isnull=True))
                ),
                name="status_event_restrictions",
            ),
            models.CheckConstraint(
                condition=models.Q(prior_event__isnull=False) | models.Q(sequence=1),
                name="status_event_first",
            ),
        ]

    def __str__(self):
        return f"VehicleStatusEvent {self.pk}"


class StatusRequest(Evidence):
    """A crew request, or a supervisor's decision on one (OPS-4); decisions point at the request."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    requested_status = models.CharField(max_length=20)
    reason = models.CharField(max_length=MAX_TEXT)
    request = models.OneToOneField("self", null=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=10)
    decision_explanation = models.CharField(max_length=MAX_TEXT, null=True)  # noqa: DJ001

    class Meta:
        db_table = "status_request"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="status_request_org_id"),
            models.CheckConstraint(
                condition=models.Q(requested_status__in=STATUSES), name="status_request_status"
            ),
            models.CheckConstraint(
                condition=models.Q(action__in=REQUEST_ACTIONS), name="status_request_action"
            ),
            models.CheckConstraint(condition=non_blank("reason"), name="status_request_reason"),
            # A REQUEST stands alone; APPROVE/DENY decide one request, and a denial must say why
            # (explicitly NOT NULL: a NULL CHECK result counts as satisfied).
            models.CheckConstraint(
                condition=(
                    models.Q(
                        action="REQUEST", request__isnull=True, decision_explanation__isnull=True
                    )
                    | models.Q(action="APPROVE", request__isnull=False)
                    | models.Q(
                        action="DENY",
                        request__isnull=False,
                        decision_explanation__isnull=False,
                        decision_explanation__regex=r"\S",
                    )
                ),
                name="status_request_shape",
            ),
        ]

    def __str__(self):
        return f"StatusRequest {self.pk}"


ACTIVITY_BASES = ("CHANGE", "BASELINE")


class VehicleActivityEvent(models.Model):
    """When a vehicle became active or inactive (SPEC 4.8 CFG-1, issue #130).

    Append-only, one linear chain per vehicle like vehicle_status_event. A CHANGE event is
    written by vehicle creation, activation or duplication under that command's receipt. A
    BASELINE event is written once per vehicle that predates this table, by the
    VEHICLE_ACTIVITY_BASELINE command, which runs as a service identity: its receipt names the
    service and the event has no user actor. A baseline records the vehicle's flag "as known
    from this instant"; it never claims when an inactive vehicle was deactivated.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    sequence = models.PositiveBigIntegerField()
    prior_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    active = models.BooleanField()
    basis = models.CharField(max_length=10)
    command = models.ForeignKey(
        "cya_core.CommandReceipt", on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_roles_at_time = models.JSONField(default=list)

    class Meta:
        db_table = "vehicle_activity_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="activity_event_org_id"),
            models.UniqueConstraint(fields=["vehicle", "sequence"], name="activity_event_sequence"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="activity_event_seq"),
            models.CheckConstraint(
                condition=models.Q(basis__in=ACTIVITY_BASES), name="activity_event_basis"
            ),
            models.CheckConstraint(
                condition=models.Q(active__in=[True, False]), name="activity_event_active_bool"
            ),
            models.CheckConstraint(
                condition=models.Q(prior_event__isnull=False) | models.Q(sequence=1),
                name="activity_event_first",
            ),
            # Only a baseline, written by the service, has no user actor; a baseline is always
            # a vehicle's first event.
            models.CheckConstraint(
                condition=models.Q(basis="CHANGE", actor_user__isnull=False)
                | models.Q(basis="BASELINE", sequence=1),
                name="activity_event_actor",
            ),
        ]

    def __str__(self):
        return f"VehicleActivityEvent {self.pk}"


ASSIGNMENT_ACTIONS = ("ASSIGN", "MOVE", "EXTEND", "END")
ASSIGNMENT_SOURCES = ("SELF", "CREW", "SUPERVISOR")
END_REASONS = ("ENDED", "VEHICLE_INACTIVE")
# OQ-79: the longest an assignment or an extension may run; an Administrator setting.
DEFAULT_ASSIGNMENT_HOURS = 24
MAX_ASSIGNMENT_HOURS = 168


class VehicleAssignmentEvent(Evidence):
    """Who is working on which vehicle, and until when (SPEC 15.4 ASG-1 to ASG-8, issue #202).

    Append-only, one linear chain per person, because the rule it protects is per person: at
    most one vehicle at a time (ASG-3). The newest event of a person's chain says where they
    are: none after END or once its ends_at has passed (an expiry writes no event, ASG-4).
    ASSIGN starts a new assignment (the prior one ended or expired); MOVE puts a current
    assignee on another vehicle in one event; EXTEND sets a later end time; END stops it early,
    by a person (ENDED) or because the vehicle was deactivated (VEHICLE_INACTIVE). ROSTER, the
    Humanity source of ASG-1, is added with its adapter. Migration 0007 installs the guards.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    person = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    sequence = models.PositiveBigIntegerField()
    prior_event = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="+"
    )
    action = models.CharField(max_length=10)
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="+")
    from_vehicle = models.ForeignKey(Vehicle, null=True, on_delete=models.PROTECT, related_name="+")
    ends_at = models.DateTimeField(null=True)
    source = models.CharField(max_length=12)
    end_reason = models.CharField(max_length=20, null=True)  # noqa: DJ001

    class Meta:
        db_table = "vehicle_assignment_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="assignment_event_org_id"),
            models.UniqueConstraint(
                fields=["person", "sequence"], name="assignment_event_sequence"
            ),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="assignment_seq"),
            models.CheckConstraint(
                condition=models.Q(prior_event__isnull=False) | models.Q(sequence=1),
                name="assignment_first",
            ),
            models.CheckConstraint(
                condition=models.Q(action__in=ASSIGNMENT_ACTIONS), name="assignment_action"
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=ASSIGNMENT_SOURCES), name="assignment_source"
            ),
            # END carries a reason and no end time; every other action carries an end time
            # after it is recorded. Only MOVE names the vehicle it moved from, and never the
            # same one. Explicit NOT NULLs: a NULL CHECK result counts as satisfied.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        action="END",
                        ends_at__isnull=True,
                        end_reason__in=END_REASONS,
                        end_reason__isnull=False,
                    )
                    | (
                        ~models.Q(action="END")
                        & models.Q(
                            ends_at__isnull=False,
                            end_reason__isnull=True,
                            ends_at__gt=models.F("recorded_at"),
                        )
                    )
                ),
                name="assignment_end_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(action="MOVE", from_vehicle__isnull=False)
                    & ~models.Q(from_vehicle=models.F("vehicle"))
                )
                | (~models.Q(action="MOVE") & models.Q(from_vehicle__isnull=True)),
                name="assignment_move_shape",
            ),
            # A new chain starts with ASSIGN.
            models.CheckConstraint(
                condition=~models.Q(sequence=1) | models.Q(action="ASSIGN"),
                name="assignment_chain_start",
            ),
        ]

    def __str__(self):
        return f"VehicleAssignmentEvent {self.pk}"


class VehicleAssignmentLimit(Evidence):
    """One revision of the longest an assignment may run (SPEC 15.4 ASG-4, OQ-79).

    The latest revision for an organization applies; without one, DEFAULT_ASSIGNMENT_HOURS.
    """

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    revision = models.PositiveIntegerField()
    max_hours = models.PositiveSmallIntegerField()

    class Meta:
        db_table = "vehicle_assignment_limit"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="assignment_limit_org_id"),
            models.UniqueConstraint(
                fields=["organization", "revision"], name="assignment_limit_revision"
            ),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="assignment_lim_rev"),
            models.CheckConstraint(
                condition=models.Q(max_hours__gte=1, max_hours__lte=MAX_ASSIGNMENT_HOURS),
                name="assignment_limit_hours",
            ),
        ]

    def __str__(self):
        return f"VehicleAssignmentLimit {self.pk}"
