"""A verification check counts for the scope row of its place (SPEC 6.0.0 VER-3, VER-4; #256 C3).

The check guard of migration 0010 accepts a verification check only for an item and instance of
the deficiency's newest scope revision. A scope row outside a kit recorded after the
item-requirement cutover keys by its item requirement (cya_deficiencies 0027), so the guard
compares the check's unit the same way: its kit, else the requirement of its version, else
NO_KIT. A scope row with the all-zero key and no version (per item: recorded before the cutover,
or for a deficiency with no place) keeps accepting any check of the item outside a kit.
"""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.checks.migrations.0017_item_requirement_link_scope")

NO_KIT = "'00000000-0000-0000-0000-000000000000'"
SCOPE_OLD = (
    f"AND s.item_id=NEW.item_id AND BINARY s.instance_key=BINARY IFNULL(NEW.kit_id,{NO_KIT})"
)
SCOPE_NEW = (
    "AND s.item_id=NEW.item_id AND (BINARY s.instance_key=BINARY COALESCE(NEW.kit_id,"
    "(SELECT rv.requirement_id FROM vehicle_item_requirement_version rv "
    f"WHERE rv.id=NEW.item_requirement_version_id),{NO_KIT}) OR (NEW.kit_id IS NULL "
    f"AND s.item_requirement_version_id IS NULL AND BINARY s.instance_key=BINARY {NO_KIT}))"
)


def check_trigger():
    sql = previous.check_trigger()
    if sql.count(SCOPE_OLD) != 1:
        raise ValueError("verification scope text moved")
    return sql.replace(SCOPE_OLD, SCOPE_NEW)


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0017_item_requirement_link_scope"),
        ("cya_deficiencies", "0027_item_requirement_place_guards"),
    ]

    operations = [
        migrations.RunSQL(
            ["DROP TRIGGER cya_check_record_bi", check_trigger()],
            reverse_sql=["DROP TRIGGER cya_check_record_bi", previous.check_trigger()],
        )
    ]
