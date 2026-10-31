"""Receipt-linked, append-only role history (contracts section 2)."""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id


class RoleGrant(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey("cya_accounts.Organization", on_delete=models.PROTECT)
    user = models.ForeignKey(
        "cya_accounts.User", on_delete=models.PROTECT, related_name="role_events"
    )
    command = models.ForeignKey("cya_core.CommandReceipt", on_delete=models.PROTECT)
    actor_user = models.ForeignKey(
        "cya_accounts.User", null=True, on_delete=models.PROTECT, related_name="role_changes"
    )
    actor_roles_at_time = models.JSONField(default=list)
    recorded_at = models.DateTimeField()
    sequence = models.PositiveBigIntegerField()
    role = models.CharField(max_length=30)
    action = models.CharField(max_length=6)
    prior_grant = models.ForeignKey("self", null=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "role_grant"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="role_org_id"),
            models.UniqueConstraint(fields=["user", "sequence"], name="role_user_sequence"),
            models.UniqueConstraint(fields=["prior_grant"], name="role_one_revoke"),
            models.CheckConstraint(
                condition=models.Q(
                    role__in=["CREW", "EMT", "SUPERVISOR", "SUPPLY_OFFICER", "ADMINISTRATOR"]
                ),
                name="role_known",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(action="GRANT", prior_grant__isnull=True)
                    | models.Q(action="REVOKE", prior_grant__isnull=False)
                ),
                name="role_action_prior",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gt=0), name="role_positive_sequence"
            ),
        ]

    def __str__(self):
        return f"RoleGrant {self.pk}"
