"""The binding insert guard knows vehicle-owned checklists (SPEC 5.0.0 CHK-2, CFG-7, 18.2).

Since amendment 5.0.0 a requirement_binding row is a vehicle checklist's schedule assignment.
The trigger is cya.kits 0014's with three refusals added before its end:
- an assignment names a checklist its own vehicle owns (the owner of a vehicle checklist is
  fixed at creation, so this cannot change later);
- vehicle checklists are assigned only once the organization's cutover is recorded;
- after the cutover nothing new is bound to or unbound from a legacy template, whose bindings
  are kept for history and as-of reads.
"""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.kits.migrations.0014_vehicle_duplicate_binding_guard")

TAIL = "\nEND"
ADDED = """
IF EXISTS (SELECT 1 FROM checklist_template t WHERE t.id=NEW.template_id
 AND t.vehicle_id IS NOT NULL AND NOT (BINARY t.vehicle_id <=> BINARY NEW.vehicle_id))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'an assignment names another vehicle checklist'; END IF;
IF EXISTS (SELECT 1 FROM checklist_template t WHERE t.id=NEW.template_id AND t.vehicle_id IS NOT NULL)
 AND NOT EXISTS (SELECT 1 FROM checklist_cutover c WHERE c.organization_id=NEW.organization_id)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'vehicle checklists start at the cutover'; END IF;
IF EXISTS (SELECT 1 FROM checklist_template t WHERE t.id=NEW.template_id AND t.vehicle_id IS NULL)
 AND EXISTS (SELECT 1 FROM checklist_cutover c WHERE c.organization_id=NEW.organization_id)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'legacy checklists are read-only after the cutover'; END IF;
END"""


def binding_insert():
    sql = previous.binding_insert()
    assert sql.endswith(TAIL), sql[-20:]
    return sql[: -len(TAIL)] + ADDED


NAME = "cya_requirement_binding_bi"


class Migration(migrations.Migration):
    dependencies = [
        ("cya_obligations", "0016_kit_presence_requirements"),
        ("cya_checklists", "0009_vehicle_checklist_guards"),
    ]

    operations = [
        migrations.RunSQL(
            [f"DROP TRIGGER {NAME}", binding_insert()],
            reverse_sql=[f"DROP TRIGGER {NAME}", previous.binding_insert()],
        )
    ]
