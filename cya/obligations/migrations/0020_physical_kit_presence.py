"""Pin kit presence obligations to assigned physical units and their assignment run."""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

latest = import_module("cya.obligations.migrations.0019_item_requirement_unit_guard")
kit_presence = import_module("cya.obligations.migrations.0016_kit_presence_requirements")
content_sources = import_module("cya.obligations.migrations.0015_kit_content_requirements")

PHYSICAL_CHECK = """IF NOT EXISTS (
 SELECT 1 FROM kit_instance_definition kd
 JOIN checklist_kit_placement pl ON pl.definition_id=kd.definition_id
 JOIN checklist_publication cp ON cp.checklist_version_id=pl.checklist_version_id
 JOIN kit_definition d ON d.id=kd.definition_id
 JOIN kit_assignment_event e ON e.id=NEW.kit_event_id
 WHERE kd.kit_id=NEW.kit_instance_id AND kd.organization_id=NEW.organization_id
 AND pl.id=NEW.placement_id AND pl.organization_id=NEW.organization_id
 AND cp.id=NEW.publication_id AND cp.organization_id=NEW.organization_id
 AND d.organization_id=NEW.organization_id AND e.organization_id=NEW.organization_id
 AND d.presence_item_id=NEW.item_id
 AND e.kit_id=NEW.kit_instance_id AND e.vehicle_id=NEW.vehicle_id
 AND e.active=1 AND e.compartment_id IS NOT NULL)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='physical kit presence requirement mismatch'; END IF;
"""
PHYSICAL_EVENT = """IF NEW.kit_presence THEN
 IF NEW.kit_instance_id IS NULL OR NEW.kit_event_id IS NULL OR NEW.placement_id IS NULL
 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='physical kit presence requires an assigned unit'; END IF;
 IF NOT EXISTS (SELECT 1 FROM kit_assignment_event e
  JOIN kit_instance_definition kd ON kd.kit_id=e.kit_id
  JOIN checklist_kit_placement pl ON pl.definition_id=kd.definition_id
  JOIN kit_definition d ON d.id=kd.definition_id
  WHERE e.id=NEW.kit_event_id AND e.organization_id=NEW.organization_id
  AND e.kit_id=NEW.kit_instance_id AND kd.organization_id=NEW.organization_id
  AND pl.organization_id=NEW.organization_id AND d.organization_id=NEW.organization_id
  AND e.vehicle_id=NEW.vehicle_id AND e.active=1 AND e.compartment_id IS NOT NULL
  AND pl.id=NEW.placement_id AND d.presence_item_id=NEW.item_id)
 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='physical kit assignment does not match its presence item'; END IF;
ELSE
"""
def version_trigger():
    original_content = content_sources.CONTENT_CHECK
    original_instance = content_sources.KIT_CONTENT_INSTANCE_CHECK
    content_sources.CONTENT_CHECK = "IF NEW.kit_presence THEN\n" + PHYSICAL_CHECK + "ELSE\n" + original_content + "END IF;\n"
    content_sources.KIT_CONTENT_INSTANCE_CHECK = PHYSICAL_EVENT + original_instance + "END IF;\n"
    try:
        return latest.version_trigger()
    finally:
        content_sources.CONTENT_CHECK = original_content
        content_sources.KIT_CONTENT_INSTANCE_CHECK = original_instance

SOURCE = models.Q(
    models.Q(item_def__isnull=False, kit_definition_item__isnull=True,
             kit_requirement__isnull=True, placement__isnull=True, kit_presence=False),
    models.Q(item_def__isnull=True, kit_definition_item__isnull=False,
             kit_instance__isnull=False, kit_requirement__isnull=True,
             placement__isnull=False, kit_presence=False),
    models.Q(item_def__isnull=True, kit_definition_item__isnull=True,
             kit_instance__isnull=True, kit_requirement__isnull=False,
             placement__isnull=False, kit_presence=False),
    models.Q(item_def__isnull=True, kit_definition_item__isnull=True,
             kit_instance__isnull=False, kit_requirement__isnull=True,
             placement__isnull=False, kit_presence=True),
    _connector="OR",
)

class Migration(migrations.Migration):
    dependencies = [("cya_obligations", "0019_item_requirement_unit_guard")]

    operations = [
        migrations.AddField(
            model_name="requirementversion",
            name="kit_presence",
            field=models.BooleanField(default=False),
        ),
        migrations.RemoveConstraint(
            model_name="requirementversion",
            name="reqversion_content_source",
        ),
        migrations.AddConstraint(
            model_name="requirementversion",
            constraint=models.CheckConstraint(condition=SOURCE, name="reqversion_content_source"),
        ),
        migrations.RunSQL(
            ["DROP TRIGGER cya_requirement_version_bi", version_trigger()],
            reverse_sql=[
                "DROP TRIGGER cya_requirement_version_bi",
                latest.version_trigger(),
            ],
        ),
    ]
