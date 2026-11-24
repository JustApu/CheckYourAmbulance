"""Pin every physical kit-presence check to its exact active assignment event."""

from importlib import import_module

from django.db import migrations, models
from django.db.models.deletion import PROTECT

previous = import_module("cya.checks.migrations.0020_physical_kit_presence")

PHYSICAL_CHECK = previous.PHYSICAL_CHECK
PINNED_PHYSICAL_CHECK = """IF NOT EXISTS (
 SELECT 1 FROM kit_instance_definition kd
 JOIN checklist_kit_placement pl ON pl.definition_id=kd.definition_id
 JOIN kit_definition d ON d.id=kd.definition_id
 JOIN kit_assignment_event e ON e.id=NEW.kit_assignment_event_id
 WHERE kd.kit_id=NEW.kit_id AND kd.organization_id=NEW.organization_id
 AND pl.organization_id=NEW.organization_id AND d.organization_id=NEW.organization_id
 AND e.organization_id=NEW.organization_id AND e.kit_id=NEW.kit_id
 AND pl.checklist_version_id=NEW.checklist_version_id
 AND d.presence_item_id=NEW.item_id AND e.vehicle_id=NEW.vehicle_id
 AND e.active=1 AND e.compartment_id IS NOT NULL
 AND e.effective_at<=NEW.recorded_at AND e.recorded_at<=NEW.recorded_at
 AND NOT EXISTS (SELECT 1 FROM kit_assignment_event newer
  WHERE newer.organization_id=e.organization_id AND newer.kit_id=e.kit_id
  AND newer.sequence>e.sequence AND newer.effective_at<=NEW.recorded_at))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='physical kit presence check does not match'; END IF;
"""

SOURCE = models.Q(
    models.Q(
        item_def__isnull=False,
        kit_definition_item__isnull=True,
        kit_requirement__isnull=True,
        kit_presence=False,
        kit_assignment_event__isnull=True,
    ),
    models.Q(
        item_def__isnull=True,
        kit_definition_item__isnull=False,
        kit__isnull=False,
        kit_requirement__isnull=True,
        kit_presence=False,
        kit_assignment_event__isnull=True,
    ),
    models.Q(
        item_def__isnull=True,
        kit_definition_item__isnull=True,
        kit__isnull=True,
        kit_requirement__isnull=False,
        kit_presence=False,
        kit_assignment_event__isnull=True,
    ),
    models.Q(
        item_def__isnull=True,
        kit_definition_item__isnull=True,
        kit__isnull=False,
        kit_requirement__isnull=True,
        kit_presence=True,
        kit_assignment_event__isnull=False,
    ),
    _connector="OR",
)


def check_trigger():
    sql = previous.check_trigger()
    occurrences = sql.count(PHYSICAL_CHECK)
    if occurrences != 2:
        raise ValueError("physical kit presence assignment-check text moved")
    return sql.replace(PHYSICAL_CHECK, PINNED_PHYSICAL_CHECK)


def refuse_reverse_with_assignment_evidence(apps, schema_editor):
    Check = apps.get_model("cya_checks", "CheckRecord")
    if Check.objects.filter(kit_assignment_event__isnull=False).exists():
        raise RuntimeError(
            "cannot reverse assignment-pinned kit presence while append-only checks exist"
        )


def require_unambiguous_assignment_history(apps, schema_editor):
    Check = apps.get_model("cya_checks", "CheckRecord")
    Assignment = apps.get_model("cya_kits", "KitAssignmentEvent")
    for check in Check.objects.filter(kit_presence=True, kit_assignment_event__isnull=True).iterator():
        event = (
            Assignment.objects.filter(
                organization_id=check.organization_id,
                kit_id=check.kit_id,
                effective_at__lte=check.recorded_at,
                recorded_at__lte=check.recorded_at,
            )
            .order_by("-sequence")
            .first()
        )
        if (
            event is None
            or not event.active
            or str(event.vehicle_id) != str(check.vehicle_id)
            or event.compartment_id is None
        ):
            raise RuntimeError(
                f"cannot pin historical physical-kit check {check.pk} to an active assignment"
            )


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0020_physical_kit_presence"),
        ("cya_kits", "0018_active_unassigned_kits"),
    ]

    operations = [
        migrations.AddField(
            model_name="checkrecord",
            name="kit_assignment_event",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=PROTECT,
                related_name="+",
                to="cya_kits.kitassignmentevent",
            ),
        ),
        migrations.RunPython(require_unambiguous_assignment_history, migrations.RunPython.noop),
        migrations.RunSQL(
            [
                "DROP TRIGGER cya_check_record_bu",
                "UPDATE check_record c JOIN kit_assignment_event e ON e.id=(SELECT latest.id "
                "FROM kit_assignment_event latest WHERE latest.organization_id=c.organization_id "
                "AND latest.kit_id=c.kit_id AND latest.effective_at<=c.recorded_at "
                "AND latest.recorded_at<=c.recorded_at ORDER BY latest.sequence DESC LIMIT 1) "
                "SET c.kit_assignment_event_id=e.id WHERE c.kit_presence=1 "
                "AND c.kit_assignment_event_id IS NULL",
                "CREATE TRIGGER cya_check_record_bu BEFORE UPDATE ON check_record "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='append-only table'",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RemoveConstraint(model_name="checkrecord", name="check_content_source"),
        migrations.AddConstraint(
            model_name="checkrecord",
            constraint=models.CheckConstraint(condition=SOURCE, name="check_content_source"),
        ),
        migrations.RunSQL(
            [
                "ALTER TABLE check_record ADD CONSTRAINT cya_check_record_kit_event_org_fk "
                "FOREIGN KEY (organization_id,kit_assignment_event_id) "
                "REFERENCES kit_assignment_event (organization_id,id) "
                "ON DELETE RESTRICT ON UPDATE RESTRICT",
                "DROP TRIGGER cya_check_record_bi",
                check_trigger(),
            ],
            reverse_sql=[
                "DROP TRIGGER cya_check_record_bi",
                "ALTER TABLE check_record DROP FOREIGN KEY cya_check_record_kit_event_org_fk",
                "ALTER TABLE check_record DROP INDEX cya_check_record_kit_event_org_fk",
                previous.check_trigger(),
            ],
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_assignment_evidence),
    ]
