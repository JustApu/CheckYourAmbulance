"""Allow an attributed cabinet count for a kit type without a bound checklist."""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

previous = import_module("cya.checks.migrations.0013_kit_presence_checks")
DJANGO_FK = "check_record_checklist_version_id_48f26fa5_fk_checklist"
ORG_FK = "cya_check_record_checklist_version_id_org_fk"
COLUMN = "checklist_version_id char(36) CHARACTER SET ascii COLLATE ascii_bin"
VERSION_CHECK = """IF NOT EXISTS (SELECT 1 FROM checklist_publication p WHERE p.organization_id=NEW.organization_id
 AND p.checklist_version_id=NEW.checklist_version_id AND p.effective_at<=NEW.recorded_at)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check names an unpublished checklist version'; END IF;"""
ROOT_TIME = "IF(NEW.predecessor_check_id IS NULL,NEW.recorded_at,(SELECT r.recorded_at FROM check_record r WHERE r.id=NEW.root_check_id))"
STANDALONE = f"""IF NEW.item_def_id IS NOT NULL OR NEW.kit_definition_item_id IS NOT NULL
 OR NEW.kit_requirement_id IS NULL OR NEW.kit_id IS NOT NULL OR NEW.session_id IS NOT NULL
 OR NEW.purpose<>'ROUTINE' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid standalone presence check'; END IF;
IF NOT EXISTS (SELECT 1 FROM vehicle_kit_requirement line
 JOIN vehicle_kit_list_version list ON list.id=line.version_id
 JOIN vehicle_kit_list_publication publication ON publication.version_id=list.id
 JOIN kit_definition definition ON definition.id=line.definition_id
 JOIN compartment position ON position.id=line.compartment_id
 WHERE line.id=NEW.kit_requirement_id AND line.organization_id=NEW.organization_id
 AND list.organization_id=NEW.organization_id AND list.vehicle_id=NEW.vehicle_id
 AND definition.organization_id=NEW.organization_id AND definition.presence_item_id=NEW.item_id
 AND publication.organization_id=NEW.organization_id AND publication.effective_at<={ROOT_TIME}
 AND publication.recorded_at<={ROOT_TIME}
 AND NOT EXISTS (SELECT 1 FROM vehicle_kit_list_publication later
  JOIN vehicle_kit_list_version later_list ON later_list.id=later.version_id
  WHERE later.organization_id=NEW.organization_id AND later_list.vehicle_id=NEW.vehicle_id
  AND later.effective_at<={ROOT_TIME} AND later.recorded_at<={ROOT_TIME}
  AND later.effective_at>publication.effective_at))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'standalone kit line is not current'; END IF;
IF NEW.predecessor_check_id IS NULL AND NOT EXISTS
 (SELECT 1 FROM command_receipt receipt JOIN command_receipt started
 ON started.organization_id=receipt.organization_id
 AND started.operation_id=JSON_UNQUOTE(JSON_EXTRACT(receipt.response_body,'$.inspection_start_operation_id'))
 JOIN vehicle_kit_requirement line ON line.id=NEW.kit_requirement_id
 JOIN compartment position ON position.id=line.compartment_id
 WHERE receipt.id=NEW.command_id AND started.command_type='SEAL_INSPECTION_START'
 AND started.actor_user_id=NEW.actor_user_id AND started.recorded_at<NEW.recorded_at
 AND JSON_CONTAINS(started.response_body, JSON_OBJECT('vehicle_id',NEW.vehicle_id,
  'kit_id',NULL,'slot_id',position.stable_slot_id)))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'standalone count needs inspection start'; END IF;"""


def check_trigger():
    sql = previous.check_insert()
    assert sql.count(VERSION_CHECK) == 1
    sql = sql.replace(
        VERSION_CHECK,
        "IF NEW.checklist_version_id IS NOT NULL THEN\n" + VERSION_CHECK + "\nEND IF;",
    )
    assert sql.count(previous.PRESENCE_CHECK) == 1
    sql = sql.replace(
        previous.PRESENCE_CHECK,
        "IF NEW.checklist_version_id IS NULL THEN\n"
        + STANDALONE
        + "\nELSE\n"
        + previous.PRESENCE_CHECK
        + "END IF;\n",
    )
    old = "p.checklist_version_id=NEW.checklist_version_id AND p.item_def_id"
    assert sql.count(old) == 1
    return sql.replace(old, "p.checklist_version_id<=>NEW.checklist_version_id AND p.item_def_id")


def change_column(nullable, trigger):
    return [
        "DROP TRIGGER cya_check_record_bi",
        f"ALTER TABLE check_record DROP FOREIGN KEY `{ORG_FK}`",
        f"ALTER TABLE check_record DROP FOREIGN KEY `{DJANGO_FK}`",
        f"ALTER TABLE check_record MODIFY {COLUMN} {'NULL' if nullable else 'NOT NULL'}",
        f"ALTER TABLE check_record ADD CONSTRAINT `{DJANGO_FK}` FOREIGN KEY (checklist_version_id) REFERENCES checklist_version (id)",
        f"ALTER TABLE check_record ADD CONSTRAINT `{ORG_FK}` FOREIGN KEY (organization_id,checklist_version_id) REFERENCES checklist_version (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        trigger,
    ]


def forbid_reverse(apps, schema_editor):
    check = apps.get_model("cya_checks", "CheckRecord")
    if check.objects.filter(checklist_version__isnull=True).exists():
        raise RuntimeError("cannot reverse while standalone presence checks exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checklists", "0006_item_expiration_tracking"),
        ("cya_checks", "0013_kit_presence_checks"),
        ("cya_deficiencies", "0023_seal_deficiency_guards"),
        ("cya_kits", "0012_vehicle_import_guards"),
    ]
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    change_column(True, check_trigger()),
                    reverse_sql=change_column(False, previous.check_insert()),
                )
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="checkrecord",
                    name="checklist_version",
                    field=models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.checklistversion",
                    ),
                )
            ],
        ),
        migrations.AddConstraint(
            model_name="checkrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(checklist_version__isnull=False)
                | models.Q(
                    checklist_version__isnull=True,
                    kit_requirement__isnull=False,
                    purpose="ROUTINE",
                    session__isnull=True,
                ),
                name="check_standalone_presence_shape",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, forbid_reverse),
    ]
