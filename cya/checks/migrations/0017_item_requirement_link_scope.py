"""Checks of an item requirement and their links (SPEC 6.0.0 CHK-11, 7.2, SCH-6; issue #256 C2).

A check of a row outside a kit after the item-requirement cutover names the requirement version
of the row shown (migration 0016). The check guard requires that version to belong to the
row's requirement (its link, or the source-row map), only on a row outside a kit, and a
correction to keep its predecessor's.

The link guard scopes such a check by its requirement: it links obligations whose kit_key (the
stream's unit) is the requirement id, so a moved requirement's obligation at its earlier place
is completed at the new one (AC-42 (6)). The one exception is the transition (§18.3 item 4):
an obligation created before the cutover keeps kit_key NO_KIT, and the check links it only
while not past its late limit and when its pinned row resolves to the same requirement. Part
B's guard on the command side (cya.checks.linking) stays for checks naming no requirement.
"""

from importlib import import_module

from django.db import migrations

checks_0014 = import_module("cya.checks.migrations.0014_standalone_kit_presence")
links_0015 = import_module("cya.checks.migrations.0015_link_every_eligible_obligation")
units = import_module("cya.obligations.migrations.0019_item_requirement_unit_guard")

NO_KIT = "'00000000-0000-0000-0000-000000000000'"
SCOPE_OLD = f"c.kit_requirement_id IS NULL AND o.kit_key=COALESCE(c.kit_id,{NO_KIT}) "
SCOPE_NEW = (
    "c.kit_requirement_id IS NULL AND (c.item_requirement_version_id IS NULL "
    f"AND o.kit_key=COALESCE(c.kit_id,{NO_KIT}) "
    "OR c.item_requirement_version_id IS NOT NULL AND c.kit_id IS NULL AND ("
    "o.kit_key=(SELECT cv.requirement_id FROM vehicle_item_requirement_version cv "
    "WHERE cv.id=c.item_requirement_version_id) "
    f"OR o.kit_key={NO_KIT} AND NEW.recorded_at<=o.late_limit_at AND EXISTS (SELECT 1 "
    "FROM requirement_version ov JOIN vehicle_item_requirement_version cv "
    "ON cv.id=c.item_requirement_version_id WHERE ov.id=o.requirement_version_id "
    f"AND ov.item_def_id IS NOT NULL AND cv.requirement_id={units.requirement_of_row('ov.item_def_id')}))) "
)


def link_trigger():
    sql = links_0015.link_trigger()
    assert sql.count(SCOPE_OLD) == 1, "link scope text moved"
    return sql.replace(SCOPE_OLD, SCOPE_NEW)


PREDECESSOR_OLD = "p.checklist_version_id<=>NEW.checklist_version_id AND"
PREDECESSOR_NEW = (
    "p.checklist_version_id<=>NEW.checklist_version_id "
    "AND p.item_requirement_version_id<=>NEW.item_requirement_version_id AND"
)
REQUIREMENT_CHECK = f"""
IF NEW.item_requirement_version_id IS NOT NULL AND (NEW.kit_id IS NOT NULL
 OR NEW.kit_requirement_id IS NOT NULL OR NEW.item_def_id IS NULL
 OR NOT EXISTS (SELECT 1 FROM vehicle_item_requirement_version v
  WHERE v.id=NEW.item_requirement_version_id AND v.vehicle_id=NEW.vehicle_id
  AND v.item_id=NEW.item_id
  AND v.requirement_id={units.requirement_of_row("NEW.item_def_id")}))
THEN {units.signal("check names another item requirement")}; END IF;"""


def check_trigger():
    sql = checks_0014.check_trigger()
    assert sql.count(PREDECESSOR_OLD) == 1, "predecessor scope text moved"
    sql = sql.replace(PREDECESSOR_OLD, PREDECESSOR_NEW)
    assert sql.rstrip().endswith("END")
    return sql.rstrip()[: -len("END")] + REQUIREMENT_CHECK + "\nEND"


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0016_check_item_requirement"),
        ("cya_obligations", "0019_item_requirement_unit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            [
                "DROP TRIGGER cya_check_record_bi",
                check_trigger(),
                "DROP TRIGGER cya_check_obligation_link_bi",
                link_trigger(),
            ],
            reverse_sql=[
                "DROP TRIGGER cya_check_obligation_link_bi",
                links_0015.link_trigger(),
                "DROP TRIGGER cya_check_record_bi",
                checks_0014.check_trigger(),
            ],
        )
    ]
