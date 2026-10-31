"""Immutable, effective-dated schedule revisions; no vehicle/checklist ownership."""

from django.core.exceptions import ValidationError
from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.schedules.engine import RECURRENCES, Rule


class ScheduleRule(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    slot = models.CharField(max_length=100, db_collation="utf8mb4_bin")
    revision = models.PositiveIntegerField()
    zone = models.CharField(max_length=100)
    wall_time = models.TimeField()
    recurrence = models.CharField(max_length=10, choices=[(r, r) for r in RECURRENCES])
    interval_days = models.PositiveIntegerField(null=True, blank=True)
    anchor_date = models.DateField(null=True, blank=True)
    early_seconds = models.PositiveIntegerField(default=3600)
    grace_seconds = models.PositiveIntegerField(default=3600)
    effective_at = models.DateTimeField()
    command = models.ForeignKey("cya_core.CommandReceipt", on_delete=models.PROTECT)
    actor_user = models.ForeignKey("cya_accounts.User", null=True, on_delete=models.PROTECT)
    actor_roles_at_time = models.JSONField(default=list)
    recorded_at = models.DateTimeField()

    class Meta:
        db_table = "schedule_rule"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="schedule_org_id"),
            models.UniqueConstraint(
                fields=["organization", "slot", "revision"], name="schedule_revision"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gt=0), name="schedule_positive_revision"
            ),
            models.CheckConstraint(
                condition=models.Q(recurrence__in=RECURRENCES), name="schedule_recurrence"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        recurrence="INTERVAL",
                        interval_days__gt=0,
                        interval_days__isnull=False,
                        anchor_date__isnull=False,
                    )
                    | (
                        ~models.Q(recurrence="INTERVAL")
                        & models.Q(interval_days__isnull=True, anchor_date__isnull=True)
                    )
                ),
                name="schedule_interval_parameters",
            ),
        ]

    def __str__(self):
        return f"Schedule {self.slot} revision {self.revision}"

    def as_rule(self):
        return Rule(**{field: getattr(self, field) for field in Rule.__dataclass_fields__})

    def clean(self):
        super().clean()
        try:
            self.as_rule()
        except (ValueError, TypeError, KeyError) as exc:
            raise ValidationError("Invalid schedule parameters") from exc
