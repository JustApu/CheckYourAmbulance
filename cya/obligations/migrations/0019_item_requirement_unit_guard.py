"""Item requirement units on requirement versions (SPEC 6.0.0 CHK-11; issue #256 C2).

After an organization's item-requirement cutover, a row outside a kit is owed per vehicle item
requirement: its requirement version pins the requirement version in force at the due instant,
and its kit_key (the stream's unit) is that requirement's id (migration 0018). This guard
requires the pinned version to belong to the requirement of the pinned item row (its item_requirement_map row), to be of the same vehicle and
item, and the kit_key to be its requirement's id.
"""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.obligations.migrations.0016_kit_presence_requirements")


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def requirement_of_row(column):
    """The item requirement of an item row, from its item_requirement_map row (SQL)."""
    return f"(SELECT m.requirement_id FROM item_requirement_map m WHERE m.item_def_id={column})"


UNIT_CHECK = f"""
IF NEW.item_requirement_version_id IS NOT NULL AND (NEW.item_def_id IS NULL
 OR NOT EXISTS (SELECT 1 FROM vehicle_item_requirement_version v
  WHERE v.id=NEW.item_requirement_version_id AND v.vehicle_id=NEW.vehicle_id
  AND v.item_id=NEW.item_id AND v.requirement_id=NEW.kit_key
  AND v.requirement_id={requirement_of_row("NEW.item_def_id")}))
THEN {signal("requirement version names another item requirement")}; END IF;"""


def version_trigger():
    sql = previous.version_trigger()
    assert sql.rstrip().endswith("END")
    head = sql.rstrip()[: -len("END")]
    return head + UNIT_CHECK + "\nEND"


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checklists", "0014_item_requirement_guards"),
        ("cya_obligations", "0018_item_requirement_units"),
    ]

    operations = [
        migrations.RunSQL(
            ["DROP TRIGGER cya_requirement_version_bi", version_trigger()],
            reverse_sql=["DROP TRIGGER cya_requirement_version_bi", previous.version_trigger()],
        )
    ]
