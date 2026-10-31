"""Organization and user identity (docs/data-model-contracts.md, Identity and fleet).

A user is identified by the identity provider's issuer and stable subject,
compared case-sensitively, never by email (SPEC AUTH-3). Users are never
deleted; a disabled user stays as an inactive row so historical attribution
remains intact (AUTH-2, AUTH-4). Roles are not stored here: role grants belong
to the authorization app (issue 22 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/22)).

The schema (table, column and constraint names, representation, guards in
migration 0002) matches the identity anchors drafted in PR 42 (https://github.com/JustApu/CheckYourAmbulance-archive/pull/42) for issue 22 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/22),
so there is exactly one user table.
"""

from django.db import models

from cya.accounts.fields import IdentityIDField, new_id


class Organization(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    name = models.CharField(max_length=200)
    station_zone = models.CharField(max_length=100)  # IANA zone of the station (OQ-25)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "organization"
        constraints = [
            models.CheckConstraint(condition=models.Q(active__in=[True, False]), name="org_bool"),
        ]

    def __str__(self):
        return f"Organization {self.pk}"


class User(models.Model):
    id = IdentityIDField(primary_key=True, default=new_id, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    # Identity tuple: case-sensitive, immutable after insert (migration 0002).
    idp_issuer = models.CharField(max_length=255, db_collation="utf8mb4_bin")
    idp_subject = models.CharField(max_length=255, db_collation="utf8mb4_bin")
    # Attributes captured at sign-in; may change, so never used as identity.
    email = models.EmailField(max_length=254)
    display_name = models.CharField(max_length=200)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "user"
        constraints = [
            # Lets child tables reference (organization_id, user_id) pairs.
            models.UniqueConstraint(fields=["organization", "id"], name="user_org_id"),
            models.UniqueConstraint(
                fields=["organization", "idp_issuer", "idp_subject"], name="user_provider_identity"
            ),
            models.CheckConstraint(condition=models.Q(active__in=[True, False]), name="user_bool"),
        ]

    def __str__(self):
        return f"User {self.pk}"
