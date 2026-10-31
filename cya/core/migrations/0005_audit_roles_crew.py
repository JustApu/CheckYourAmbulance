"""Permit CREW base role in audit_event roles_at_time while preserving EMT."""

from django.db import migrations

AUDIT_ROLES_CREW = (
    "JSON_TYPE(roles_at_time) = 'ARRAY' "
    "AND JSON_LENGTH(roles_at_time) <= 5 "
    'AND JSON_CONTAINS(\'["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\', roles_at_time) '
    "AND (actor_user_id IS NOT NULL OR JSON_LENGTH(roles_at_time) = 0)"
)

AUDIT_ROLES_LEGACY = (
    "JSON_TYPE(roles_at_time) = 'ARRAY' "
    "AND JSON_LENGTH(roles_at_time) <= 4 "
    'AND JSON_CONTAINS(\'["EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\', roles_at_time) '
    "AND (actor_user_id IS NOT NULL OR JSON_LENGTH(roles_at_time) = 0)"
)


class Migration(migrations.Migration):
    dependencies = [("cya_core", "0004_audit_integrity")]
    operations = [
        migrations.RunSQL(
            [
                "ALTER TABLE audit_event DROP CONSTRAINT audit_roles",
                f"ALTER TABLE audit_event ADD CONSTRAINT audit_roles CHECK ({AUDIT_ROLES_CREW})",
            ],
            [
                "ALTER TABLE audit_event DROP CONSTRAINT audit_roles",
                f"ALTER TABLE audit_event ADD CONSTRAINT audit_roles CHECK ({AUDIT_ROLES_LEGACY})",
            ],
        ),
    ]
