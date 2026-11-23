"""Attribute each discrepancy in a seal inspection to its containing receipt."""

from importlib import import_module

from django.db import migrations, models

base = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
evidence = import_module("cya.deficiencies.migrations.0004_correction_effects")
events = import_module("cya.deficiencies.migrations.0019_check_suspension_end")
OLD_COMMANDS = "'CHECK_RECORD','DEFICIENCY_REPORT'"
NEW_COMMANDS = "'CHECK_RECORD','DEFICIENCY_REPORT','SEAL_RESEAL'"


def _extend_commands(sql, expected):
    if sql.count(OLD_COMMANDS) != expected:
        raise ValueError("unexpected existing deficiency trigger")
    return sql.replace(OLD_COMMANDS, NEW_COMMANDS)


def _append_guard(sql, guard):
    if not sql.endswith("END"):
        raise ValueError("unexpected trigger terminator")
    return sql[:-3] + guard + "\nEND"


HEADER_GUARD = """IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND ((r.command_type IN ('CHECK_RECORD','DEFICIENCY_REPORT') AND NEW.operation_ordinal=0)
 OR (r.command_type='SEAL_RESEAL' AND NEW.operation_ordinal>0 AND
 JSON_CONTAINS(r.response_body, JSON_OBJECT('id', NEW.id, 'ordinal', NEW.operation_ordinal),
 '$.deficiencies'))))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'deficiency receipt ordinal mismatch'; END IF;"""

EVIDENCE_GUARD = """IF NEW.seal_inspection_item_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM seal_inspection_item i JOIN seal_inspection s ON s.id=i.inspection_id
 JOIN seal_event e ON e.id=s.reseal_id JOIN deficiency d ON d.id=NEW.deficiency_id
 WHERE i.id=NEW.seal_inspection_item_id AND i.organization_id=NEW.organization_id
 AND s.organization_id=NEW.organization_id AND e.organization_id=NEW.organization_id
 AND d.organization_id=NEW.organization_id AND d.command_id=NEW.command_id
 AND i.command_id=NEW.command_id AND i.outcome='FAILED' AND e.vehicle_id=d.vehicle_id
 AND d.item_id=i.item_id AND d.kit_id <=> i.kit_id)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal item evidence mismatch'; END IF;
IF NEW.seal_kit_count_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM seal_inspection_kit_count c JOIN seal_inspection s ON s.id=c.inspection_id
 JOIN seal_event e ON e.id=s.reseal_id JOIN vehicle_kit_requirement r ON r.id=c.kit_requirement_id
 JOIN kit_definition k ON k.id=r.definition_id JOIN deficiency d ON d.id=NEW.deficiency_id
 WHERE c.id=NEW.seal_kit_count_id AND c.organization_id=NEW.organization_id
 AND s.organization_id=NEW.organization_id AND e.organization_id=NEW.organization_id
 AND r.organization_id=NEW.organization_id AND k.organization_id=NEW.organization_id
 AND d.organization_id=NEW.organization_id AND d.command_id=NEW.command_id
 AND c.command_id=NEW.command_id AND c.count<>r.required_count
 AND e.vehicle_id=d.vehicle_id AND d.item_id=k.presence_item_id AND d.kit_id IS NULL)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal kit count evidence mismatch'; END IF;"""


def event_insert():
    """The deficiency_event insert trigger this migration installs."""
    return _extend_commands(events.event_insert(), 2)


def forward():
    header = base.HEADER_GUARD.replace(
        " THEN SIGNAL SQLSTATE",
        " OR NOT (OLD.operation_ordinal <=> NEW.operation_ordinal) THEN SIGNAL SQLSTATE",
    )
    if header == base.HEADER_GUARD:
        raise ValueError("unexpected deficiency header guard")
    return [
        "ALTER TABLE deficiency_evidence ADD CONSTRAINT cya_defevidence_sealitem_org_fk "
        "FOREIGN KEY (organization_id,seal_inspection_item_id) REFERENCES seal_inspection_item "
        "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "ALTER TABLE deficiency_evidence ADD CONSTRAINT cya_defevidence_sealkitcount_org_fk "
        "FOREIGN KEY (organization_id,seal_kit_count_id) REFERENCES seal_inspection_kit_count "
        "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "DROP TRIGGER cya_deficiency_bi",
        _append_guard(_extend_commands(base.DEFICIENCY_INSERT, 1), HEADER_GUARD),
        "DROP TRIGGER cya_deficiency_immutable_bu",
        header,
        "DROP TRIGGER cya_deficiency_affected_item_bi",
        _extend_commands(base.AFFECTED_INSERT, 1),
        "DROP TRIGGER cya_deficiency_event_bi",
        event_insert(),
        "DROP TRIGGER cya_deficiency_evidence_bi",
        _append_guard(_extend_commands(evidence.EVIDENCE_INSERT, 1), EVIDENCE_GUARD),
    ]


def reverse():
    return [
        "DROP TRIGGER cya_deficiency_evidence_bi",
        evidence.EVIDENCE_INSERT,
        "DROP TRIGGER cya_deficiency_event_bi",
        events.event_insert(),
        "DROP TRIGGER cya_deficiency_affected_item_bi",
        base.AFFECTED_INSERT,
        "DROP TRIGGER cya_deficiency_bi",
        base.DEFICIENCY_INSERT,
        "DROP TRIGGER cya_deficiency_immutable_bu",
        base.HEADER_GUARD,
        "ALTER TABLE deficiency_evidence DROP FOREIGN KEY cya_defevidence_sealkitcount_org_fk",
        "ALTER TABLE deficiency_evidence DROP INDEX cya_defevidence_sealkitcount_org_fk",
        "ALTER TABLE deficiency_evidence DROP FOREIGN KEY cya_defevidence_sealitem_org_fk",
        "ALTER TABLE deficiency_evidence DROP INDEX cya_defevidence_sealitem_org_fk",
    ]


def forbid_reverse_with_evidence(apps, schema_editor):
    Deficiency = apps.get_model("cya_deficiencies", "Deficiency")
    Evidence = apps.get_model("cya_deficiencies", "DeficiencyEvidence")
    if (
        Deficiency.objects.filter(operation_ordinal__gt=0).exists()
        or Evidence.objects.filter(
            models.Q(seal_inspection_item__isnull=False) | models.Q(seal_kit_count__isnull=False)
        ).exists()
    ):
        raise RuntimeError("cannot reverse seal deficiency guards while seal evidence exists")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_deficiencies", "0022_remove_deficiency_deficiency_org_operation_and_more")
    ]
    operations = [
        migrations.RunSQL(forward(), reverse_sql=reverse()),
        migrations.RunPython(migrations.RunPython.noop, forbid_reverse_with_evidence),
    ]
