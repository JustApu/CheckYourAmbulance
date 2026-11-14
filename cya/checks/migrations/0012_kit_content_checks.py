"""Checks of content from a placed kit definition (SPEC 4.6 KDEF-4, KDEF-7; issue #129).

A check names exactly one content source: a checklist_item_def (as before), or a
kit_definition_item (check_content_source). The schema owner adds an organization-pair key and
an item-pair key for the new column and rewrites the insert guard: migration 0010's checks on
the definition and kit apply when item_def_id is set; otherwise the content entry must belong to
a version, published by the check's instant, of a kit definition that the check's checklist
version places, and the kit must have that definition (kit_instance_definition) and be on the
vehicle, in any compartment (SPEC 2.0.0 KDEF-4). A correction keeps its predecessor's content source. Checks written
before this migration all name an item definition, so they keep their meaning (KDEF-8).

item_def_id's keys (migrations 0001, 0002) are dropped around its nullability change and added
back unchanged; the column is changed by hand because Django's MODIFY would drop its ascii_bin
collation. The new column has no Django key (db_constraint=False), so the reverse can drop the
new keys' indexes, which MariaDB otherwise leaves behind.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

previous = import_module("cya.checks.migrations.0010_verification_checks")
ORG = "organization_id"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


DEFINITION_START = "IF NOT EXISTS (SELECT 1 FROM checklist_item_def d WHERE d.id=NEW.item_def_id"
DEFINITION_END = "'check definition does not belong to the version'; END IF;\n"
KIT_START = "IF NEW.kit_id IS NULL AND EXISTS (SELECT 1 FROM checklist_item_def d"
KIT_END = "'check kit is not a kit of the vehicle for the item'; END IF;\n"
PREDECESSOR_OLD = "AND p.item_def_id=NEW.item_def_id"
PREDECESSOR_NEW = (
    "AND p.item_def_id<=>NEW.item_def_id AND p.kit_definition_item_id<=>NEW.kit_definition_item_id"
)

CONTENT_CHECK = f"""IF NOT EXISTS (SELECT 1 FROM kit_definition_item i
 JOIN kit_definition_version kv ON kv.id=i.version_id
 JOIN kit_definition_publication kp ON kp.version_id=kv.id AND kp.effective_at<=NEW.recorded_at
 JOIN checklist_kit_placement pl ON pl.definition_id=kv.definition_id
  AND pl.checklist_version_id=NEW.checklist_version_id
 WHERE i.id=NEW.kit_definition_item_id AND i.{ORG}=NEW.{ORG} AND i.item_id=NEW.item_id)
THEN {signal("check kit content does not belong to a kit the version places")}; END IF;
"""

KIT_CONTENT_CHECK = f"""IF NOT EXISTS (SELECT 1 FROM kit_instance k
 JOIN kit_instance_definition kd ON kd.kit_id=k.id
 JOIN kit_definition_item i ON i.id=NEW.kit_definition_item_id
 JOIN kit_definition_version kv ON kv.id=i.version_id AND kv.definition_id=kd.definition_id
 JOIN checklist_kit_placement pl ON pl.checklist_version_id=NEW.checklist_version_id
  AND pl.definition_id=kd.definition_id
 WHERE k.id=NEW.kit_id AND k.{ORG}=NEW.{ORG} AND k.vehicle_id=NEW.vehicle_id
 AND k.compartment_id IS NOT NULL)
THEN {signal("check kit does not have the placed definition on the vehicle")}; END IF;
"""


def _wrap(sql, start, end, alternative):
    first = sql.index(start)
    last = sql.index(end, first) + len(end)
    return (
        sql[:first]
        + "IF NEW.item_def_id IS NOT NULL THEN\n"
        + sql[first:last]
        + "ELSE\n"
        + alternative
        + "END IF;\n"
        + sql[last:]
    )


def check_insert():
    sql = previous.check_insert()
    sql = _wrap(sql, DEFINITION_START, DEFINITION_END, CONTENT_CHECK)
    sql = _wrap(sql, KIT_START, KIT_END, KIT_CONTENT_CHECK)
    assert sql.count(PREDECESSOR_OLD) == 1
    return sql.replace(PREDECESSOR_OLD, PREDECESSOR_NEW)


ITEM_DEF_KEYS = [
    (
        "cya_check_record_item_def_id_org_fk",
        f"FOREIGN KEY ({ORG},item_def_id) REFERENCES `checklist_item_def` ({ORG},id)",
    ),
    (
        "cya_check_record_item_def_pair_fk",
        "FOREIGN KEY (item_def_id,item_id) REFERENCES `checklist_item_def` (id,item_id)",
    ),
]
DROP_ITEM_DEF_KEYS = [
    f"ALTER TABLE check_record DROP FOREIGN KEY `{name}`" for name, _ in ITEM_DEF_KEYS
]
ADD_ITEM_DEF_KEYS = [
    f"ALTER TABLE check_record ADD CONSTRAINT `{name}` {body} ON DELETE RESTRICT ON UPDATE RESTRICT"
    for name, body in ITEM_DEF_KEYS
]
DJANGO_KEY = "check_record_item_def_id_97d6b8a1_fk_checklist_item_def_id"
COLUMN = "item_def_id char(36) CHARACTER SET ascii COLLATE ascii_bin"


def _nullability(null):
    return [
        f"ALTER TABLE check_record DROP FOREIGN KEY `{DJANGO_KEY}`",
        f"ALTER TABLE check_record MODIFY {COLUMN} {'NULL' if null else 'NOT NULL'}",
        f"ALTER TABLE check_record ADD CONSTRAINT `{DJANGO_KEY}` "
        "FOREIGN KEY (item_def_id) REFERENCES `checklist_item_def` (id)",
    ]


NEW_KEYS = ("cya_check_record_kitdefitem_pair_fk", "cya_check_record_kitdefitem_org_fk")
FORWARD = [
    *ADD_ITEM_DEF_KEYS,
    f"ALTER TABLE check_record ADD CONSTRAINT `{NEW_KEYS[1]}` "
    f"FOREIGN KEY ({ORG},kit_definition_item_id) REFERENCES `kit_definition_item` ({ORG},id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    f"ALTER TABLE check_record ADD CONSTRAINT `{NEW_KEYS[0]}` "
    "FOREIGN KEY (kit_definition_item_id,item_id) REFERENCES `kit_definition_item` (id,item_id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    "DROP TRIGGER cya_check_record_bi",
    check_insert(),
]
REVERSE = [
    "DROP TRIGGER cya_check_record_bi",
    previous.check_insert(),
    *DROP_ITEM_DEF_KEYS,
    *(f"ALTER TABLE check_record DROP FOREIGN KEY `{name}`" for name in NEW_KEYS),
    *(f"ALTER TABLE check_record DROP INDEX `{name}`" for name in NEW_KEYS),
]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0011_check_vehicle_item_time"),
        ("cya_kits", "0008_kit_definition_item_id_item"),
    ]

    operations = [
        migrations.RunSQL(DROP_ITEM_DEF_KEYS, reverse_sql=ADD_ITEM_DEF_KEYS),
        migrations.AddField(
            model_name="checkrecord",
            name="kit_definition_item",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.kitdefinitionitem",
                db_constraint=False,
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="checkrecord",
                    name="item_def",
                    field=models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.checklistitemdef",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(_nullability(True), reverse_sql=_nullability(False))
            ],
        ),
        migrations.AddConstraint(
            model_name="checkrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("item_def__isnull", False), ("kit_definition_item__isnull", True)),
                    models.Q(
                        ("item_def__isnull", True),
                        ("kit__isnull", False),
                        ("kit_definition_item__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="check_content_source",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
