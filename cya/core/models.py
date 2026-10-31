"""Durable audit and idempotency records (SPEC 18 and 19)."""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id


class CommandReceipt(models.Model):
    """Immutable outcome reserved before command business writes."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    operation_id = IdentityIDField()
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, blank=True, on_delete=models.PROTECT
    )
    # SQL NULL distinguishes human commands from service commands (contracts §2).
    service_identity = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    job_key = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    command_type = models.CharField(max_length=100)
    request_digest = models.CharField(max_length=64)
    result_schema_version = models.PositiveIntegerField()
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    recorded_at = models.DateTimeField()
    correlation_id = IdentityIDField()

    class Meta:
        db_table = "command_receipt"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="receipt_org_operation"
            ),
            models.UniqueConstraint(fields=["organization", "id"], name="receipt_org_id"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        actor_user__isnull=False,
                        service_identity__isnull=True,
                        job_key__isnull=True,
                    )
                    | models.Q(
                        actor_user__isnull=True,
                        service_identity__isnull=False,
                        job_key__isnull=False,
                    )
                ),
                name="receipt_actor_kind",
            ),
        ]

    def __str__(self):
        return f"CommandReceipt {self.pk}"


class AuditEvent(models.Model):
    """Append-only, sanitized audit evidence; no raw request or secret payloads."""

    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    command = models.ForeignKey(CommandReceipt, null=True, blank=True, on_delete=models.PROTECT)
    request_attempt_id = IdentityIDField()
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, blank=True, on_delete=models.PROTECT
    )
    # Named worker attribution survives even when its business transaction fails.
    service_identity = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    job_key = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    roles_at_time = models.JSONField(default=list)
    action = models.CharField(max_length=100)
    # Anonymous/targetless audit entries deliberately retain SQL NULL.
    target_type = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    target_id = IdentityIDField(null=True, blank=True)
    recorded_at = models.DateTimeField()
    correlation_id = IdentityIDField()
    outcome = models.CharField(max_length=30)
    reason_code = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    prior_snapshot = models.JSONField(null=True, blank=True)
    new_snapshot = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "audit_event"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="audit_org_id"),
        ]

    def __str__(self):
        return f"AuditEvent {self.pk}"
