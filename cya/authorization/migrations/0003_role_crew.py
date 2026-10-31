"""Permit CREW base role in role_grant while preserving EMT for historical evidence."""

from django.db import migrations, models

ROLE_CODES_CREW = (
    "BINARY role IN ('CREW','EMT','SUPERVISOR','SUPPLY_OFFICER','ADMINISTRATOR') "
    "AND BINARY action IN ('GRANT','REVOKE')"
)
ROLE_ACTOR_ROLES_CREW = (
    "JSON_TYPE(actor_roles_at_time) = 'ARRAY' "
    "AND JSON_LENGTH(actor_roles_at_time) <= 5 "
    'AND JSON_CONTAINS(\'["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\', actor_roles_at_time) '
    "AND (actor_user_id IS NOT NULL OR JSON_LENGTH(actor_roles_at_time) = 0)"
)

ROLE_CODES_LEGACY = (
    "BINARY role IN ('EMT','SUPERVISOR','SUPPLY_OFFICER','ADMINISTRATOR') "
    "AND BINARY action IN ('GRANT','REVOKE')"
)
ROLE_ACTOR_ROLES_LEGACY = (
    "JSON_TYPE(actor_roles_at_time) = 'ARRAY' "
    "AND JSON_LENGTH(actor_roles_at_time) <= 4 "
    'AND JSON_CONTAINS(\'["EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\', actor_roles_at_time) '
    "AND (actor_user_id IS NOT NULL OR JSON_LENGTH(actor_roles_at_time) = 0)"
)

GUARD_CREW = """CREATE TRIGGER cya_role_integrity_bi BEFORE INSERT ON role_grant
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.actor_user_id)
 AND r.recorded_at=NEW.recorded_at) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role receipt attribution mismatch';
END IF;
IF NEW.sequence <> (SELECT COALESCE(MAX(g.sequence),0)+1 FROM role_grant g WHERE g.user_id=NEW.user_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role sequence mismatch';
END IF;
IF BINARY NEW.action='REVOKE' AND NOT EXISTS (
 SELECT 1 FROM role_grant g WHERE g.id=NEW.prior_grant_id AND g.user_id=NEW.user_id
 AND g.organization_id=NEW.organization_id AND BINARY g.role=BINARY NEW.role
 AND BINARY g.action='GRANT') THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role revocation mismatch';
END IF;
IF BINARY NEW.action='GRANT' AND EXISTS (
 SELECT 1 FROM role_grant g WHERE g.user_id=NEW.user_id
 AND (BINARY g.role=BINARY NEW.role OR (BINARY g.role IN ('CREW','EMT') AND BINARY NEW.role IN ('CREW','EMT')))
 AND BINARY g.action='GRANT' AND NOT EXISTS (
 SELECT 1 FROM role_grant r WHERE r.prior_grant_id=g.id)) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role already granted';
END IF;
END"""

GUARD_LEGACY = """CREATE TRIGGER cya_role_integrity_bi BEFORE INSERT ON role_grant
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.actor_user_id)
 AND r.recorded_at=NEW.recorded_at) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role receipt attribution mismatch';
END IF;
IF NEW.sequence <> (SELECT COALESCE(MAX(g.sequence),0)+1 FROM role_grant g WHERE g.user_id=NEW.user_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role sequence mismatch';
END IF;
IF BINARY NEW.action='REVOKE' AND NOT EXISTS (
 SELECT 1 FROM role_grant g WHERE g.id=NEW.prior_grant_id AND g.user_id=NEW.user_id
 AND g.organization_id=NEW.organization_id AND BINARY g.role=BINARY NEW.role
 AND BINARY g.action='GRANT') THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role revocation mismatch';
END IF;
IF BINARY NEW.action='GRANT' AND EXISTS (
 SELECT 1 FROM role_grant g WHERE g.user_id=NEW.user_id AND BINARY g.role=BINARY NEW.role
 AND BINARY g.action='GRANT' AND NOT EXISTS (
 SELECT 1 FROM role_grant r WHERE r.prior_grant_id=g.id)) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role already granted';
END IF;
END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_authorization", "0002_role_guards"),
        ("cya_core", "0005_audit_roles_crew"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="rolegrant",
            name="role_known",
        ),
        migrations.AddConstraint(
            model_name="rolegrant",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    role__in=["CREW", "EMT", "SUPERVISOR", "SUPPLY_OFFICER", "ADMINISTRATOR"]
                ),
                name="role_known",
            ),
        ),
        migrations.RunSQL(
            [
                "ALTER TABLE role_grant DROP CONSTRAINT role_codes",
                "ALTER TABLE role_grant DROP CONSTRAINT role_actor_roles",
                f"ALTER TABLE role_grant ADD CONSTRAINT role_codes CHECK ({ROLE_CODES_CREW})",
                f"ALTER TABLE role_grant ADD CONSTRAINT role_actor_roles CHECK ({ROLE_ACTOR_ROLES_CREW})",
                "DROP TRIGGER cya_role_integrity_bi",
                GUARD_CREW,
            ],
            [
                "DROP TRIGGER cya_role_integrity_bi",
                GUARD_LEGACY,
                "ALTER TABLE role_grant DROP CONSTRAINT role_codes",
                "ALTER TABLE role_grant DROP CONSTRAINT role_actor_roles",
                f"ALTER TABLE role_grant ADD CONSTRAINT role_codes CHECK ({ROLE_CODES_LEGACY})",
                f"ALTER TABLE role_grant ADD CONSTRAINT role_actor_roles CHECK ({ROLE_ACTOR_ROLES_LEGACY})",
            ],
        ),
    ]
