"""Allow checklist checks to record presence of an assigned physical kit."""

from importlib import import_module

from django.db import migrations, models

latest = import_module("cya.checks.migrations.0017_item_requirement_link_scope")
presence = import_module("cya.checks.migrations.0013_kit_presence_checks")
content_checks = import_module("cya.checks.migrations.0012_kit_content_checks")

PHYSICAL_CHECK = """IF NOT EXISTS (
 SELECT 1 FROM kit_instance_definition kd
 JOIN checklist_kit_placement pl ON pl.definition_id=kd.definition_id
 JOIN kit_definition d ON d.id=kd.definition_id
 JOIN kit_assignment_event e ON e.kit_id=NEW.kit_id
 WHERE kd.kit_id=NEW.kit_id AND kd.organization_id=NEW.organization_id
 AND pl.organization_id=NEW.organization_id AND d.organization_id=NEW.organization_id
 AND e.organization_id=NEW.organization_id
 AND pl.checklist_version_id=NEW.checklist_version_id
 AND d.presence_item_id=NEW.item_id AND e.vehicle_id=NEW.vehicle_id
 AND e.active=1 AND e.compartment_id IS NOT NULL)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='physical kit presence check does not match'; END IF;
"""
PHYSICAL_KIT_CHECK = """IF NEW.kit_presence AND NEW.kit_id IS NULL THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='physical kit presence check must name a kit';
END IF;
"""
SOURCE_CHECK = """IF NEW.kit_presence THEN
""" + PHYSICAL_CHECK + """ELSE
"""
SOURCE_KIT_CHECK = """IF NEW.kit_presence THEN
""" + PHYSICAL_KIT_CHECK + """ELSE
"""

def check_trigger():
    old_content = content_checks.CONTENT_CHECK
    old_kit = content_checks.KIT_CONTENT_CHECK
    old_predecessor = presence.PREDECESSOR_NEW
    content_checks.CONTENT_CHECK = SOURCE_CHECK + old_content + "END IF;\n"
    content_checks.KIT_CONTENT_CHECK = SOURCE_KIT_CHECK + old_kit + "END IF;\n"
    presence.PREDECESSOR_NEW = old_predecessor + " AND p.kit_presence=NEW.kit_presence"
    try:
        return latest.check_trigger()
    finally:
        content_checks.CONTENT_CHECK = old_content
        content_checks.KIT_CONTENT_CHECK = old_kit
        presence.PREDECESSOR_NEW = old_predecessor

SOURCE = models.Q(
    models.Q(item_def__isnull=False, kit_definition_item__isnull=True,
             kit_requirement__isnull=True, kit_presence=False),
    models.Q(item_def__isnull=True, kit_definition_item__isnull=False,
             kit__isnull=False, kit_requirement__isnull=True, kit_presence=False),
    models.Q(item_def__isnull=True, kit_definition_item__isnull=True,
             kit__isnull=True, kit_requirement__isnull=False, kit_presence=False),
    models.Q(item_def__isnull=True, kit_definition_item__isnull=True,
             kit__isnull=False, kit_requirement__isnull=True, kit_presence=True),
    _connector="OR",
)

class Migration(migrations.Migration):
    dependencies = [("cya_checks", "0017_item_requirement_link_scope")]

    operations = [
        migrations.AddField(
            model_name="checkrecord",
            name="kit_presence",
            field=models.BooleanField(default=False),
        ),
        migrations.RemoveConstraint(model_name="checkrecord", name="check_content_source"),
        migrations.AddConstraint(
            model_name="checkrecord",
            constraint=models.CheckConstraint(condition=SOURCE, name="check_content_source"),
        ),
        migrations.RunSQL(
            ["DROP TRIGGER cya_check_record_bi", check_trigger()],
            reverse_sql=[
                "DROP TRIGGER cya_check_record_bi",
                latest.check_trigger(),
            ],
        ),
    ]
