"""Store the check supporting a supervisor completion (issue 82 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/82) Part A)."""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers

conditions_guards = import_module("cya.conditions.migrations.0003_conditions_receipt_guards")


def trigger_sql():
    source = conditions_guards.OBLIGATION_DETERMINATION_INSERT
    source = source.replace(
        "'OBLIGATIONS_RECONCILE'\n ))",
        "'OBLIGATIONS_RECONCILE',\n  'SUPERVISOR_COMPLETE_UNABLE',\n"
        "  'SUPERVISOR_COMPLETION_INVALIDATION'\n ))",
    )
    if source == conditions_guards.OBLIGATION_DETERMINATION_INSERT:
        raise RuntimeError("conditions receipt trigger command list changed")
    body = source[: source.rfind("END")]
    body += """IF NEW.determination_type='SUPERVISOR_COMPLETED' AND (
 NEW.explanation IS NULL OR TRIM(NEW.explanation)='' OR NEW.actor_user_id IS NULL
 OR NOT (JSON_CONTAINS(NEW.actor_roles_at_time,'\"SUPERVISOR\"')
  OR JSON_CONTAINS(NEW.actor_roles_at_time,'\"ADMINISTRATOR\"'))
 OR JSON_TYPE(JSON_EXTRACT(NEW.evaluated_inputs,'$.check_id')) IS NULL
 OR NOT EXISTS (SELECT 1 FROM obligation o JOIN check_record c
  ON c.organization_id=o.organization_id
  JOIN check_obligation_link l ON l.check_id=c.id AND l.obligation_id=NEW.obligation_id
   AND l.recorded_at<=NEW.recorded_at
  WHERE o.id=NEW.obligation_id AND o.organization_id=NEW.organization_id
   AND NEW.vehicle_id=o.vehicle_id AND NEW.due_at=o.due_at
   AND c.id=JSON_UNQUOTE(JSON_EXTRACT(NEW.evaluated_inputs,'$.check_id'))
   AND c.organization_id=NEW.organization_id AND c.outcome='UNABLE_TO_INSPECT'
   AND c.recorded_at<=NEW.recorded_at
   AND NOT EXISTS (SELECT 1 FROM check_record n WHERE n.predecessor_check_id=c.id
    AND n.recorded_at<=NEW.recorded_at)
   AND NOT EXISTS (SELECT 1 FROM check_event e WHERE e.root_check_id=c.root_check_id
    AND e.event_type='INVALIDATION' AND e.recorded_at<=NEW.recorded_at))
 OR (NEW.prior_determination_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM obligation_determination p WHERE p.id=NEW.prior_determination_id
   AND p.organization_id=NEW.organization_id AND p.obligation_id=NEW.obligation_id
   AND p.determination_type='INVALIDATED')))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='invalid supervisor completion'; END IF;
IF NEW.determination_type='INVALIDATED' AND (
 NEW.explanation IS NULL OR TRIM(NEW.explanation)=''
 OR NOT EXISTS (SELECT 1 FROM obligation_determination p
  WHERE p.id=NEW.prior_determination_id AND p.organization_id=NEW.organization_id
   AND p.obligation_id=NEW.obligation_id AND p.determination_type='SUPERVISOR_COMPLETED'
   AND p.vehicle_id <=> NEW.vehicle_id AND p.due_at <=> NEW.due_at)
 OR NOT JSON_CONTAINS(NEW.actor_roles_at_time,'\"ADMINISTRATOR\"'))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='invalid completion invalidation'; END IF;
END"""
    return source, body


def determination_check_trigger():
    return """CREATE TRIGGER cya_determination_check_bi BEFORE INSERT ON determination_check
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r
 JOIN obligation_determination d ON d.id=NEW.determination_id
 JOIN check_record c ON c.id=NEW.check_id
 JOIN check_obligation_link l ON l.check_id=c.id AND l.obligation_id=d.obligation_id
 WHERE r.id=NEW.command_id AND r.organization_id=NEW.organization_id
  AND r.command_type='SUPERVISOR_COMPLETE_UNABLE'
  AND r.actor_user_id=NEW.actor_user_id AND r.recorded_at=NEW.recorded_at
  AND d.organization_id=NEW.organization_id AND d.command_id=NEW.command_id
  AND d.actor_user_id=NEW.actor_user_id AND d.recorded_at=NEW.recorded_at
  AND d.actor_roles_at_time=NEW.actor_roles_at_time
  AND d.determination_type='SUPERVISOR_COMPLETED'
  AND d.explanation IS NOT NULL AND TRIM(d.explanation)<>''
  AND c.organization_id=NEW.organization_id AND c.recorded_at<=NEW.recorded_at
  AND c.outcome='UNABLE_TO_INSPECT'
  AND JSON_UNQUOTE(JSON_EXTRACT(d.evaluated_inputs,'$.check_id'))=c.id
  AND NOT EXISTS (SELECT 1 FROM check_record n WHERE n.predecessor_check_id=c.id
   AND n.recorded_at<=NEW.recorded_at)
  AND NOT EXISTS (SELECT 1 FROM check_event e WHERE e.root_check_id=c.root_check_id
   AND e.event_type='INVALIDATION' AND e.recorded_at<=NEW.recorded_at)
  AND (JSON_CONTAINS(NEW.actor_roles_at_time,'\"SUPERVISOR\"')
   OR JSON_CONTAINS(NEW.actor_roles_at_time,'\"ADMINISTRATOR\"')))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='determination check scope mismatch'; END IF;
END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0007_chain_invalidation"),
        ("cya_conditions", "0003_conditions_receipt_guards"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeterminationCheck",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                (
                    "id",
                    cya.accounts.fields.IdentityIDField(
                        db_collation="ascii_bin",
                        default=cya.accounts.fields.new_id,
                        editable=False,
                        max_length=36,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
                (
                    "check_record",
                    models.ForeignKey(
                        db_column="check_id",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supervisor_determinations",
                        to="cya_checks.checkrecord",
                    ),
                ),
                (
                    "command",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_core.commandreceipt",
                    ),
                ),
                (
                    "determination",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supporting_checks",
                        to="cya_conditions.obligationdetermination",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.organization",
                    ),
                ),
            ],
            options={
                "db_table": "determination_check",
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="detcheck_org_id"),
                    models.UniqueConstraint(
                        fields=("determination", "check_record"), name="detcheck_pair"
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            [
                f"ALTER TABLE `determination_check` ADD CONSTRAINT `cya_detcheck_uuid` CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
                'ALTER TABLE `determination_check` ADD CONSTRAINT `cya_detcheck_roles` CHECK (JSON_TYPE(actor_roles_at_time)=\'ARRAY\' AND JSON_LENGTH(actor_roles_at_time)<=4 AND JSON_CONTAINS(\'["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\', actor_roles_at_time))',
                "ALTER TABLE `determination_check` ADD CONSTRAINT `cya_detcheck_det_org_fk` FOREIGN KEY (organization_id,determination_id) REFERENCES `obligation_determination` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `determination_check` ADD CONSTRAINT `cya_detcheck_check_org_fk` FOREIGN KEY (organization_id,check_id) REFERENCES `check_record` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `determination_check` ADD CONSTRAINT `cya_detcheck_user_org_fk` FOREIGN KEY (organization_id,actor_user_id) REFERENCES `user` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                *append_only_triggers("determination_check"),
                determination_check_trigger(),
                "DROP TRIGGER cya_obligation_determination_bi",
                trigger_sql()[1],
            ],
            reverse_sql=[
                "DROP TRIGGER cya_determination_check_bi",
                "DROP TRIGGER cya_determination_check_bu",
                "DROP TRIGGER cya_determination_check_bd",
                "DROP TRIGGER cya_obligation_determination_bi",
                trigger_sql()[0],
                "ALTER TABLE `determination_check` DROP FOREIGN KEY `cya_detcheck_user_org_fk`, DROP FOREIGN KEY `cya_detcheck_check_org_fk`, DROP FOREIGN KEY `cya_detcheck_det_org_fk`, DROP CONSTRAINT `cya_detcheck_roles`, DROP CONSTRAINT `cya_detcheck_uuid`",
            ],
        ),
    ]
