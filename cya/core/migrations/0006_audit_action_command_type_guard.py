"""Enforce that an audit_event row's action matches its command_receipt command_type."""

from django.db import migrations

GUARD_NEW = """CREATE TRIGGER cya_audit_attribution_bi BEFORE INSERT ON audit_event
FOR EACH ROW BEGIN
IF NEW.command_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM command_receipt r WHERE r.id = NEW.command_id AND
 (NOT (r.actor_user_id <=> NEW.actor_user_id) OR
  NOT (BINARY r.service_identity <=> BINARY NEW.service_identity) OR
  NOT (BINARY r.job_key <=> BINARY NEW.job_key) OR
  NOT (r.correlation_id <=> NEW.correlation_id) OR
  NOT (
    BINARY r.command_type = BINARY NEW.action OR (
      BINARY NEW.action IN (
        'ROSTER_CONFLICT_DETECTED',
        'ROSTER_CONFLICT_NOTIFICATION_DELIVERED',
        'ROSTER_CONFLICT_NOTIFICATION_FAILED'
      ) AND BINARY r.command_type = 'RECORD_SESSION_ROSTER_CORRECTION'
    )
  ))) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit attribution mismatch';
END IF;
IF (NEW.target_type = 'USER' AND NOT EXISTS (
 SELECT 1 FROM `user` u WHERE u.id=NEW.target_id AND u.organization_id=NEW.organization_id))
 OR (NEW.target_type = 'ORGANIZATION' AND NEW.target_id <> NEW.organization_id)
 OR (NEW.target_type = 'COMMAND_RECEIPT' AND NOT EXISTS (
 SELECT 1 FROM command_receipt r WHERE r.id=NEW.target_id AND r.organization_id=NEW.organization_id)) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit target scope mismatch';
END IF; END"""

GUARD_OLD = """CREATE TRIGGER cya_audit_attribution_bi BEFORE INSERT ON audit_event
FOR EACH ROW BEGIN
IF NEW.command_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM command_receipt r WHERE r.id = NEW.command_id AND
 (NOT (r.actor_user_id <=> NEW.actor_user_id) OR
  NOT (BINARY r.service_identity <=> BINARY NEW.service_identity) OR
  NOT (BINARY r.job_key <=> BINARY NEW.job_key) OR
  NOT (r.correlation_id <=> NEW.correlation_id))) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit attribution mismatch';
END IF;
IF (NEW.target_type = 'USER' AND NOT EXISTS (
 SELECT 1 FROM `user` u WHERE u.id=NEW.target_id AND u.organization_id=NEW.organization_id))
 OR (NEW.target_type = 'ORGANIZATION' AND NEW.target_id <> NEW.organization_id)
 OR (NEW.target_type = 'COMMAND_RECEIPT' AND NOT EXISTS (
 SELECT 1 FROM command_receipt r WHERE r.id=NEW.target_id AND r.organization_id=NEW.organization_id)) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit target scope mismatch';
END IF; END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_core", "0005_audit_roles_crew"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TRIGGER IF EXISTS cya_audit_attribution_bi",
                GUARD_NEW,
            ],
            reverse_sql=[
                "DROP TRIGGER IF EXISTS cya_audit_attribution_bi",
                GUARD_OLD,
            ],
        ),
    ]
