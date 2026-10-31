"""Per-crew-member view preference (SPEC 6.5 VW-1, VW-2; OQ-38, issue 83 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/83)).

user_preference is an M table: one row per user, created on first save and updated in place by
the audited command in cya.attention.preferences, which records the prior and new values in the
audit trail. It is per crew member, not per device, so every device the person signs in on reads
the same row. Item attention and the checklist views are derived on demand and have no table
(SPEC 17.4): see cya.attention.state and cya.attention.checklist.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id
from cya.checklists.models import organization_fk

VIEWS = ("NORMAL", "PRIORITY")


class UserPreference(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = organization_fk()
    user = models.OneToOneField("cya_accounts.User", on_delete=models.PROTECT, related_name="+")
    preferred_view = models.CharField(max_length=8)
    needs_attention_only = models.BooleanField()
    updated_at = models.DateTimeField()

    class Meta:
        db_table = "user_preference"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="userpref_org_id"),
            models.CheckConstraint(
                condition=models.Q(preferred_view__in=VIEWS), name="userpref_view"
            ),
            models.CheckConstraint(
                condition=models.Q(needs_attention_only__in=[True, False]), name="userpref_bool"
            ),
        ]

    def __str__(self):
        return f"UserPreference {self.user_id}"
