"""Requirements whose content comes from a placed kit definition (SPEC 4.6 KDEF-4, issue #129).

A requirement version pins exactly one content source: a checklist_item_def (as before), or a
kit_definition_item together with the checklist_kit_placement that placed its kit definition
(the check constraint reqversion_content_source). The schema owner adds organization-pair keys
for the new columns, a composite key so the content entry names the requirement's own item, and
a new insert guard. The guard is migration 0006's, with the checklist-definition checks taken
only when item_def_id is set; otherwise it checks that the placement belongs to the pinned
publication's version, that the content entry belongs to a published version of the placed
definition, and that the kit instance has that definition and is active on the vehicle, in any
compartment (SPEC 2.0.0 KDEF-4), at its pinned assignment event. Rows written before this migration
all name an item definition, so they keep their meaning (KDEF-8).
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

previous = import_module("cya.obligations.migrations.0006_ad_hoc_obligations")
ORG = "organization_id"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


DEFINITION_CHECK_START = (
    "IF NOT EXISTS (SELECT 1 FROM checklist_item_def d\n JOIN checklist_publication p"
)
DEFINITION_CHECK_END = "'requirement definition mismatch'; END IF;\n"
KIT_CHECK_START = (
    "IF NEW.kit_instance_id IS NULL THEN\n IF EXISTS (SELECT 1 FROM checklist_item_def d"
)
KIT_CHECK_END = (
    "'requirement kit instance is not active on the vehicle in the item slot'; END IF;\nEND IF;\n"
)

CONTENT_CHECK = f"""IF NOT EXISTS (SELECT 1 FROM checklist_kit_placement pl
 JOIN checklist_publication p ON p.id=NEW.publication_id
 JOIN kit_definition_item i ON i.id=NEW.kit_definition_item_id
 JOIN kit_definition_version kv ON kv.id=i.version_id
 JOIN kit_definition_publication kp ON kp.version_id=kv.id
 WHERE pl.id=NEW.placement_id AND pl.{ORG}=NEW.{ORG} AND i.{ORG}=NEW.{ORG}
 AND pl.checklist_version_id=p.checklist_version_id
 AND kv.definition_id=pl.definition_id AND i.item_id=NEW.item_id)
THEN {signal("requirement kit content mismatch")}; END IF;
"""

KIT_CONTENT_INSTANCE_CHECK = f"""IF NEW.kit_instance_id IS NULL THEN
 {signal("requirement for kit content must name a kit instance")};
ELSEIF NOT EXISTS (SELECT 1 FROM kit_assignment_event e
  JOIN checklist_kit_placement pl ON pl.id=NEW.placement_id
  JOIN kit_instance_definition kd ON kd.kit_id=NEW.kit_instance_id AND kd.definition_id=pl.definition_id
  WHERE e.id=NEW.kit_event_id AND e.{ORG}=NEW.{ORG} AND e.kit_id=NEW.kit_instance_id
  AND e.vehicle_id=NEW.vehicle_id AND e.active=1 AND e.compartment_id IS NOT NULL)
THEN {signal("requirement kit instance does not have the placed definition on the vehicle")};
END IF;
"""


def _segment(sql, start, end):
    first = sql.index(start)
    last = sql.index(end, first) + len(end)
    return first, last


def version_trigger():
    """Migration 0006's insert guard with a second content source (see the module docstring)."""
    sql = previous.version_trigger()
    first, last = _segment(sql, DEFINITION_CHECK_START, DEFINITION_CHECK_END)
    sql = (
        sql[:first]
        + "IF NEW.item_def_id IS NOT NULL THEN\n"
        + sql[first:last]
        + "ELSE\n"
        + CONTENT_CHECK
        + "END IF;\n"
        + sql[last:]
    )
    first, last = _segment(sql, KIT_CHECK_START, KIT_CHECK_END)
    return (
        sql[:first]
        + "IF NEW.item_def_id IS NOT NULL THEN\n"
        + sql[first:last]
        + "ELSE\n"
        + KIT_CONTENT_INSTANCE_CHECK
        + "END IF;\n"
        + sql[last:]
    )


# MariaDB will not change item_def_id's nullability while these keys (migration 0002) use it,
# so they are dropped before the AlterField and re-added, unchanged, after it.
ITEM_DEF_KEYS = [
    (
        "cya_obl_org_fk_08",
        f"FOREIGN KEY ({ORG},item_def_id) REFERENCES `checklist_item_def` ({ORG},id)",
    ),
    (
        "cya_obl_item_def_item_fk",
        "FOREIGN KEY (item_def_id, item_id) REFERENCES `checklist_item_def` (id, item_id)",
    ),
]
DROP_ITEM_DEF_KEYS = [
    f"ALTER TABLE requirement_version DROP FOREIGN KEY `{name}`" for name, _ in ITEM_DEF_KEYS
]
ADD_ITEM_DEF_KEYS = [
    f"ALTER TABLE requirement_version ADD CONSTRAINT `{name}` {body} "
    "ON DELETE RESTRICT ON UPDATE RESTRICT"
    for name, body in ITEM_DEF_KEYS
]

DJANGO_ITEM_DEF_KEY = "requirement_version_item_def_id_087a8fa1_fk_checklist"
ITEM_DEF_COLUMN = "item_def_id char(36) CHARACTER SET ascii COLLATE ascii_bin"
NULLABLE_ITEM_DEF = [
    f"ALTER TABLE requirement_version DROP FOREIGN KEY `{DJANGO_ITEM_DEF_KEY}`",
    f"ALTER TABLE requirement_version MODIFY {ITEM_DEF_COLUMN} NULL",
    f"ALTER TABLE requirement_version ADD CONSTRAINT `{DJANGO_ITEM_DEF_KEY}` "
    "FOREIGN KEY (item_def_id) REFERENCES `checklist_item_def` (id)",
]
REQUIRED_ITEM_DEF = [
    f"ALTER TABLE requirement_version DROP FOREIGN KEY `{DJANGO_ITEM_DEF_KEY}`",
    f"ALTER TABLE requirement_version MODIFY {ITEM_DEF_COLUMN} NOT NULL",
    f"ALTER TABLE requirement_version ADD CONSTRAINT `{DJANGO_ITEM_DEF_KEY}` "
    "FOREIGN KEY (item_def_id) REFERENCES `checklist_item_def` (id)",
]

FORWARD = [
    *ADD_ITEM_DEF_KEYS,
    "ALTER TABLE requirement_version ADD CONSTRAINT `cya_obl_reqversion_kitdefitem_org_fk` "
    f"FOREIGN KEY ({ORG},kit_definition_item_id) REFERENCES `kit_definition_item` ({ORG},id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    "ALTER TABLE requirement_version ADD CONSTRAINT `cya_obl_reqversion_placement_org_fk` "
    f"FOREIGN KEY ({ORG},placement_id) REFERENCES `checklist_kit_placement` ({ORG},id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    "ALTER TABLE requirement_version ADD CONSTRAINT `cya_obl_reqversion_kitdefitem_item_fk` "
    "FOREIGN KEY (kit_definition_item_id,item_id) REFERENCES `kit_definition_item` (id,item_id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    "DROP TRIGGER cya_requirement_version_bi",
    version_trigger(),
]
# Dropping a foreign key leaves the index MariaDB created for it, which would block a re-apply,
# so the reverse drops each new key's index right after the key. Nothing else depends on those
# indexes: the two new columns carry no Django key of their own (db_constraint=False), because
# these organization-pair keys already enforce their references.
NEW_KEYS = (
    "cya_obl_reqversion_kitdefitem_item_fk",
    "cya_obl_reqversion_placement_org_fk",
    "cya_obl_reqversion_kitdefitem_org_fk",
)
REVERSE = [
    "DROP TRIGGER cya_requirement_version_bi",
    previous.version_trigger(),
    *DROP_ITEM_DEF_KEYS,
    *(f"ALTER TABLE requirement_version DROP FOREIGN KEY `{name}`" for name in NEW_KEYS),
    *(f"ALTER TABLE requirement_version DROP INDEX `{name}`" for name in NEW_KEYS),
]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0004_compartment_sealable"),
        ("cya_conditions", "0003_conditions_receipt_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_kits", "0008_kit_definition_item_id_item"),
        ("cya_obligations", "0014_projection_facts_digest"),
        ("cya_schedules", "0003_schedule_command_type_guards"),
    ]

    operations = [
        migrations.RunSQL(DROP_ITEM_DEF_KEYS, reverse_sql=ADD_ITEM_DEF_KEYS),
        migrations.AddField(
            model_name="requirementversion",
            name="kit_definition_item",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.kitdefinitionitem",
                db_constraint=False,
            ),
        ),
        migrations.AddField(
            model_name="requirementversion",
            name="placement",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.checklistkitplacement",
                db_constraint=False,
            ),
        ),
        # Django's own MODIFY would drop the ascii_bin collation every id column uses, which
        # breaks the keys above, so the column is changed by hand with its collation kept.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="requirementversion",
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
                migrations.RunSQL(NULLABLE_ITEM_DEF, reverse_sql=REQUIRED_ITEM_DEF)
            ],
        ),
        migrations.AddConstraint(
            model_name="requirementversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("item_def__isnull", False),
                        ("kit_definition_item__isnull", True),
                        ("placement__isnull", True),
                    ),
                    models.Q(
                        ("item_def__isnull", True),
                        ("kit_definition_item__isnull", False),
                        ("kit_instance__isnull", False),
                        ("placement__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="reqversion_content_source",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
