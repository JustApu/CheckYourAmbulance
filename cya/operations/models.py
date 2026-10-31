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
