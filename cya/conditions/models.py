"""Conditional rule, roster snapshot, and obligation determination models (SPEC 5.6, 15.2, 15.3, contracts §2)."""

from django.core.exceptions import ValidationError
from django.db import models

from cya.accounts.fields import IdentityIDField, new_id


class ConditionalRule(models.Model):
    """Immutable versioned rule for conditional obligation satisfaction/waiver (Kind V)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    command = models.ForeignKey("cya_core.CommandReceipt", on_delete=models.PROTECT)
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, blank=True, on_delete=models.PROTECT
    )
    actor_roles_at_time = models.JSONField(default=list)
    revision = models.PositiveIntegerField()
    rule_type = models.CharField(max_length=50)
    typed_inputs = models.JSONField(default=dict)
    effective_at = models.DateTimeField()

    class Meta:
        db_table = "conditional_rule"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="cond_rule_org_id"),
            models.UniqueConstraint(
                fields=["organization", "rule_type", "revision"], name="cond_rule_org_type_rev"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    rule_type__in=["CARRY_OVER_FROM_PRIOR_SHIFT", "SEAL_SUBSTITUTION"]
                ),
                name="cond_rule_type_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gt=0), name="cond_rule_positive_rev"
            ),
        ]

    def __str__(self):
        return f"ConditionalRule {self.rule_type} r{self.revision} ({self.pk})"

    def clean(self):
        super().clean()
        if self.actor_user_id and self.actor_user.organization_id != self.organization_id:
            raise ValidationError("Actor user organization mismatch")
        if self.command_id and self.command.organization_id != self.organization_id:
            raise ValidationError("Command organization mismatch")


class RosterSnapshot(models.Model):
    """Periodic immutable snapshot of scheduled shifts from roster provider (Kind A)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    command = models.ForeignKey("cya_core.CommandReceipt", on_delete=models.PROTECT)
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, blank=True, on_delete=models.PROTECT
    )
    actor_roles_at_time = models.JSONField(default=list)
    service_identity = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    job_key = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    provider = models.CharField(max_length=100)
    fetched_at = models.DateTimeField()
    availability = models.CharField(max_length=50)
    shift_start = models.DateTimeField()
    shift_end = models.DateTimeField()
    vehicle_id = IdentityIDField(null=True, blank=True)

    class Meta:
        db_table = "roster_snapshot"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="roster_snap_org_id"),
            models.CheckConstraint(
                condition=models.Q(shift_start__lt=models.F("shift_end")),
                name="roster_snap_window",
            ),
            models.CheckConstraint(
                condition=models.Q(availability__in=["AVAILABLE", "UNAVAILABLE", "STALE"]),
                name="roster_snap_avail",
            ),
        ]

    def __str__(self):
        return f"RosterSnapshot {self.provider} ({self.pk})"

    def clean(self):
        super().clean()
        if self.command_id and self.command.organization_id != self.organization_id:
            raise ValidationError("Command organization mismatch")
        if self.actor_user_id and self.actor_user.organization_id != self.organization_id:
            raise ValidationError("Actor user organization mismatch")


class RosterMember(models.Model):
    """Attributed person assignment within an immutable roster snapshot (Kind A)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    snapshot = models.ForeignKey(RosterSnapshot, on_delete=models.PROTECT, related_name="members")
    person_identity = models.CharField(max_length=255)
    assignment = models.CharField(max_length=100, default="CREW")

    class Meta:
        db_table = "roster_member"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="roster_member_org_id"),
            models.UniqueConstraint(
                fields=["snapshot", "person_identity", "assignment"], name="roster_member_unique"
            ),
        ]

    def __str__(self):
        return f"RosterMember {self.person_identity} ({self.assignment})"

    def clean(self):
        super().clean()
        if self.snapshot_id and self.snapshot.organization_id != self.organization_id:
            raise ValidationError("Snapshot organization mismatch")


class SessionRosterCorrection(models.Model):
    """Actual roster correction recorded for an inspection session (Kind A, SPEC 15.3)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    command = models.ForeignKey("cya_core.CommandReceipt", on_delete=models.PROTECT)
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey("cya_accounts.User", on_delete=models.PROTECT)
    actor_roles_at_time = models.JSONField(default=list)
    session_id = IdentityIDField()
    vehicle_id = IdentityIDField()
    snapshot = models.ForeignKey(RosterSnapshot, null=True, blank=True, on_delete=models.PROTECT)
    covers_from = models.DateTimeField()
    covers_until = models.DateTimeField()
    resolves_correction = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "session_roster_correction"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="sess_roster_corr_org_id"),
            models.CheckConstraint(
                condition=models.Q(covers_from__lte=models.F("covers_until")),
                name="sess_roster_corr_window",
            ),
        ]

    def __str__(self):
        return f"SessionRosterCorrection {self.pk} (session {self.session_id})"

    def clean(self):
        super().clean()
        if self.command_id and self.command.organization_id != self.organization_id:
            raise ValidationError("Command organization mismatch")
        if self.actor_user_id and self.actor_user.organization_id != self.organization_id:
            raise ValidationError("Actor user organization mismatch")
        if self.snapshot_id and self.snapshot.organization_id != self.organization_id:
            raise ValidationError("Snapshot organization mismatch")
        if (
            self.resolves_correction_id
            and self.resolves_correction.organization_id != self.organization_id
        ):
            raise ValidationError("Resolves correction organization mismatch")


class ObligationDetermination(models.Model):
    """Append-only record of obligation state determinations (waived/reinstated/completed) (Kind A)."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    command = models.ForeignKey("cya_core.CommandReceipt", on_delete=models.PROTECT)
    recorded_at = models.DateTimeField()
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, blank=True, on_delete=models.PROTECT
    )
    actor_roles_at_time = models.JSONField(default=list)
    service_identity = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    job_key = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    obligation_id = IdentityIDField()
    vehicle_id = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    due_at = models.DateTimeField(null=True, blank=True)
    determination_type = models.CharField(max_length=50)
    rule = models.ForeignKey(ConditionalRule, null=True, blank=True, on_delete=models.PROTECT)
    prior_determination = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    explanation = models.CharField(max_length=4000, null=True, blank=True)  # noqa: DJ001
    evaluated_inputs = models.JSONField(default=dict)

    class Meta:
        db_table = "obligation_determination"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="ob_det_org_id"),
            models.CheckConstraint(
                condition=models.Q(
                    determination_type__in=[
                        "WAIVED",
                        "REINSTATED",
                        "SUPERVISOR_COMPLETED",
                        "INVALIDATED",
                        "SUPERSEDED",
                    ]
                ),
                name="ob_det_type_valid",
            ),
        ]

    def __str__(self):
        return f"ObligationDetermination {self.determination_type} ({self.pk})"

    def clean(self):
        super().clean()
        if self.command_id and self.command.organization_id != self.organization_id:
            raise ValidationError("Command organization mismatch")
        if self.actor_user_id and self.actor_user.organization_id != self.organization_id:
            raise ValidationError("Actor user organization mismatch")
        if self.rule_id and self.rule.organization_id != self.organization_id:
            raise ValidationError("Rule organization mismatch")
        if (
            self.prior_determination_id
            and self.prior_determination.organization_id != self.organization_id
        ):
            raise ValidationError("Prior determination organization mismatch")
