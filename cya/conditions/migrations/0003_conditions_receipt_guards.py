"""Receipt attribution and command-type insert guards for cya_conditions models (issue 94 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/94))."""

from django.db import migrations


def signal(message: str) -> str:
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


CONDITIONAL_RULE_INSERT = f"""CREATE TRIGGER cya_conditional_rule_bi BEFORE INSERT ON conditional_rule
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.actor_user_id IS NOT NULL
 AND r.recorded_at=NEW.recorded_at
 AND r.command_type='CONDITIONAL_RULE_PUBLISH')
THEN {signal("conditional_rule receipt attribution mismatch")}; END IF;
END"""

ROSTER_SNAPSHOT_INSERT = f"""CREATE TRIGGER cya_roster_snapshot_bi BEFORE INSERT ON roster_snapshot
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.service_identity <=> NEW.service_identity AND r.job_key <=> NEW.job_key
 AND r.recorded_at=NEW.recorded_at
 AND r.command_type IN ('INGEST_ROSTER_SNAPSHOT', 'INGEST_ROSTER_UNAVAILABLE', 'RECORD_SESSION_ROSTER_CORRECTION'))
THEN {signal("roster_snapshot receipt attribution mismatch")}; END IF;
END"""

SESSION_ROSTER_CORRECTION_INSERT = f"""CREATE TRIGGER cya_session_roster_correction_bi BEFORE INSERT ON session_roster_correction
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.actor_user_id IS NOT NULL
 AND r.recorded_at=NEW.recorded_at
 AND r.command_type='RECORD_SESSION_ROSTER_CORRECTION')
THEN {signal("session_roster_correction receipt attribution mismatch")}; END IF;
END"""

OBLIGATION_DETERMINATION_INSERT = f"""CREATE TRIGGER cya_obligation_determination_bi BEFORE INSERT ON obligation_determination
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.service_identity <=> NEW.service_identity AND r.job_key <=> NEW.job_key
 AND r.recorded_at=NEW.recorded_at
 AND r.command_type IN (
  'RECORD_OBLIGATION_DETERMINATION',
  'INGEST_ROSTER_SNAPSHOT',
  'INGEST_ROSTER_UNAVAILABLE',
  'RECORD_SESSION_ROSTER_CORRECTION',
  'OBLIGATIONS_RECONCILE'
 ))
THEN {signal("obligation_determination receipt attribution mismatch")}; END IF;
END"""


def forward_sql():
    return [
        CONDITIONAL_RULE_INSERT,
        ROSTER_SNAPSHOT_INSERT,
        SESSION_ROSTER_CORRECTION_INSERT,
        OBLIGATION_DETERMINATION_INSERT,
    ]


def reverse_sql():
    return [
        "DROP TRIGGER IF EXISTS `cya_conditional_rule_bi`",
        "DROP TRIGGER IF EXISTS `cya_roster_snapshot_bi`",
        "DROP TRIGGER IF EXISTS `cya_session_roster_correction_bi`",
        "DROP TRIGGER IF EXISTS `cya_obligation_determination_bi`",
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_conditions", "0002_conditions_guards"),
    ]

    operations = [
        migrations.RunSQL(
            sql=forward_sql(),
            reverse_sql=reverse_sql(),
        ),
    ]
