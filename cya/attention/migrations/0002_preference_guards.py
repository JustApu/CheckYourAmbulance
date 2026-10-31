"""Schema-owner guards for user_preference: UUID check, organization-pair key, frozen identity.

The row is mutable (SPEC 6.5, OQ-38) but never changes whose it is: a trigger rejects any change
to id, organization_id or user_id, and the organization-pair foreign key keeps a preference in
its user's organization.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import immutable_header_guard

TABLE = "user_preference"


def forward_statements():
    return [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_user_org_fk` "
        "FOREIGN KEY (organization_id,user_id) REFERENCES `user` (organization_id,id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        immutable_header_guard(TABLE),
    ]


def reverse_statements():
    return [
        f"DROP TRIGGER cya_{TABLE}_immutable_bu",
        f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_user_org_fk`",
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`",
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_attention", "0001_initial"),
        ("cya_accounts", "0002_identity_guards"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
