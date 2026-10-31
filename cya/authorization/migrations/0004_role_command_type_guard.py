"""Enforce command_type on role_grant receipt attribution (issue 95 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/95))."""

from django.db import migrations

GUARD_FORWARD = """CREATE TRIGGER cya_role_integrity_bi BEFORE INSERT ON role_grant
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.actor_user_id)
 AND r.recorded_at=NEW.recorded_at
 AND r.command_type IN ('ROLE_CHANGE', 'INITIAL_CREW', 'BOOTSTRAP_ADMINISTRATOR', 'INITIAL_EMT')) THEN
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

GUARD_REVERSE = """CREATE TRIGGER cya_role_integrity_bi BEFORE INSERT ON role_grant
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


class Migration(migrations.Migration):
    dependencies = [
        ("cya_authorization", "0003_role_crew"),
    ]

    operations = [
        migrations.RunSQL(
            [
                "DROP TRIGGER cya_role_integrity_bi",
                GUARD_FORWARD,
            ],
            [
                "DROP TRIGGER cya_role_integrity_bi",
                GUARD_REVERSE,
            ],
        ),
    ]
