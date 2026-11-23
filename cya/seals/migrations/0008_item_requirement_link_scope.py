"""Seal inspection links of an item requirement (SPEC 6.0.0 CHK-11; issue #256 C2).

A loose item's inspection row names the item requirement version of its checklist row
(migration 0007). Its links follow the same scope as a check's (cya_checks 0017): the
requirement's stream, or, during the transition of SPEC 18.3 item 4, an obligation created
before the cutover (kit_key NO_KIT) not past its late limit whose pinned row resolves to the same
requirement. A separate insert guard holds the inspection row's version to its row's requirement.
"""

from importlib import import_module

from django.db import migrations

guards = import_module("cya.seals.migrations.0002_evidence_guards")
links = import_module("cya.seals.migrations.0004_inspection_obligation_guards")
units = import_module("cya.obligations.migrations.0019_item_requirement_unit_guard")

TABLE = links.TABLE
NO_KIT = "'00000000-0000-0000-0000-000000000000'"
SCOPE_OLD = f" AND ob.kit_key=COALESCE(item.kit_id,{NO_KIT})\n"
SCOPE_NEW = (
    " AND (item.item_requirement_version_id IS NULL"
    f" AND ob.kit_key=COALESCE(item.kit_id,{NO_KIT})"
    " OR item.item_requirement_version_id IS NOT NULL AND item.kit_id IS NULL AND ("
    "ob.kit_key=(SELECT iv.requirement_id FROM vehicle_item_requirement_version iv"
    " WHERE iv.id=item.item_requirement_version_id)"
    f" OR ob.kit_key={NO_KIT} AND NEW.recorded_at<=ob.late_limit_at AND EXISTS (SELECT 1"
    " FROM requirement_version ov JOIN vehicle_item_requirement_version iv"
    " ON iv.id=item.item_requirement_version_id WHERE ov.id=ob.requirement_version_id"
    f" AND ov.item_def_id IS NOT NULL AND iv.requirement_id={units.requirement_of_row('ov.item_def_id')})))\n"
)


def link_guard():
    assert links.LINK_GUARD.count(SCOPE_OLD) == 1, "seal link scope text moved"
    return links.LINK_GUARD.replace(SCOPE_OLD, SCOPE_NEW)


ITEM_GUARD = f"""CREATE TRIGGER cya_seal_inspection_item_itemreq_bi BEFORE INSERT
ON seal_inspection_item FOR EACH ROW BEGIN
IF NEW.item_requirement_version_id IS NOT NULL AND (NEW.kit_id IS NOT NULL
 OR NEW.item_def_id IS NULL OR NOT EXISTS (SELECT 1 FROM vehicle_item_requirement_version v
  WHERE v.id=NEW.item_requirement_version_id AND v.item_id=NEW.item_id
  AND v.requirement_id={units.requirement_of_row("NEW.item_def_id")}))
THEN {units.signal("seal inspection names another item requirement")}; END IF;
END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_obligations", "0019_item_requirement_unit_guard"),
        ("cya_seals", "0007_inspection_item_requirement"),
    ]

    operations = [
        migrations.RunSQL(
            [
                f"DROP TRIGGER cya_{TABLE}_bi",
                guards.insert_guard(TABLE, "'SEAL_RESEAL'", link_guard()),
                ITEM_GUARD,
            ],
            reverse_sql=[
                "DROP TRIGGER cya_seal_inspection_item_itemreq_bi",
                f"DROP TRIGGER cya_{TABLE}_bi",
                guards.insert_guard(TABLE, "'SEAL_RESEAL'", links.LINK_GUARD),
            ],
        )
    ]
