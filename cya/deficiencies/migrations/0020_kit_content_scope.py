"""Deficiency scope over placed kit content (SPEC 4.6 KDEF-7, issue #129 part 4c).

deficiency_affected_item, verification_scope_row, deficiency_scope_expansion and
check_suspension name either a checklist_item_def (as before) or the kit_definition_item of a
placed kit definition: exactly one (the *_content_source checks). The schema owner adds
organization-pair and item-pair keys for the new column and rewrites the three insert guards that
read the definition: their checklist-definition kit checks apply only when definition_id is set;
for kit content the row's kit must have the content's definition (kit_instance_definition) and be
active on the deficiency's vehicle; and every "same definition" comparison also compares the
content column, null-safely. Rows written before this migration all name a definition (KDEF-8).

As in cya_obligations 0015 and cya_checks 0012, definition_id's keys are dropped around its
nullability change (made by hand to keep the ascii_bin collation), and the reverse drops the new
keys' indexes, which MariaDB otherwise leaves behind.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

expansion = import_module("cya.deficiencies.migrations.0016_scope_expansion")
suspension = import_module("cya.deficiencies.migrations.0019_check_suspension_end")
ORG = "organization_id"
ZERO = "00000000-0000-0000-0000-000000000000"

# (table, existing key name, key body) on definition_id, added by migrations 0002-0017.
DEFINITION_KEYS = [
    (
        "deficiency_affected_item",
        "cya_def_fk_15",
        f"FOREIGN KEY ({ORG},definition_id) REFERENCES `checklist_item_def` ({ORG},id)",
    ),
    (
        "deficiency_affected_item",
        "cya_def_affected_pair_fk",
        "FOREIGN KEY (definition_id,item_id) REFERENCES `checklist_item_def` (id,item_id)",
    ),
    (
        "verification_scope_row",
        "cya_verscope_definition_item_fk",
        "FOREIGN KEY (definition_id,item_id) REFERENCES `checklist_item_def` (id,item_id)",
    ),
    (
        "deficiency_scope_expansion",
        "cya_defexp_definition_item_fk",
        "FOREIGN KEY (definition_id,item_id) REFERENCES `checklist_item_def` (id,item_id)",
    ),
    (
        "check_suspension",
        "cya_checksusp_definition_item_fk",
        "FOREIGN KEY (definition_id,item_id) REFERENCES `checklist_item_def` (id,item_id)",
    ),
]
DROP_DEFINITION_KEYS = [
    f"ALTER TABLE `{table}` DROP FOREIGN KEY `{name}`" for table, name, _ in DEFINITION_KEYS
]
ADD_DEFINITION_KEYS = [
    f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` {body} ON DELETE RESTRICT ON UPDATE RESTRICT"
    for table, name, body in DEFINITION_KEYS
]
DJANGO_KEYS = {
    "deficiency_affected_item": "deficiency_affected__definition_id_1cf5943f_fk_checklist",
    "verification_scope_row": "verification_scope_r_definition_id_d74d6fd5_fk_checklist",
    "deficiency_scope_expansion": "deficiency_scope_exp_definition_id_2ac6727a_fk_checklist",
    "check_suspension": "check_suspension_definition_id_0ad390fe_fk_checklist_item_def_id",
}


def nullable_definition(table, null):
    key = DJANGO_KEYS[table]
    return [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `{key}`",
        f"ALTER TABLE `{table}` MODIFY definition_id char(36) CHARACTER SET ascii "
        f"COLLATE ascii_bin {'NULL' if null else 'NOT NULL'}",
        f"ALTER TABLE `{table}` ADD CONSTRAINT `{key}` "
        "FOREIGN KEY (definition_id) REFERENCES `checklist_item_def` (id)",
    ]


def new_keys(table):
    short = {
        "deficiency_affected_item": "defaffect",
        "verification_scope_row": "verscope",
        "deficiency_scope_expansion": "defexp",
        "check_suspension": "checksusp",
    }[table]
    return [
        (
            table,
            f"cya_{short}_kitdefitem_org_fk",
            f"FOREIGN KEY ({ORG},kit_definition_item_id) REFERENCES `kit_definition_item` ({ORG},id)",
        ),
        (
            table,
            f"cya_{short}_kitdefitem_pair_fk",
            "FOREIGN KEY (kit_definition_item_id,item_id) REFERENCES `kit_definition_item` (id,item_id)",
        ),
    ]


NEW_KEYS = [key for table in DJANGO_KEYS for key in new_keys(table)]


def _replace(text, before, after, count=1):
    assert text.count(before) == count, (before, text.count(before))
    return text.replace(before, after)


OLD_KIT_FIT = "(NEW.kit_instance_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM checklist_item_def cd"
NEW_KIT_FIT = (
    "(NEW.definition_id IS NOT NULL AND NEW.kit_instance_id IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM checklist_item_def cd"
)


def kit_content_fit(message, vehicle):
    """For kit content: the kit has the content's definition and is active on the vehicle."""
    return f"""IF NEW.kit_definition_item_id IS NOT NULL AND (NEW.kit_instance_id IS NULL OR NOT EXISTS (
 SELECT 1 FROM kit_definition_item i
 JOIN kit_definition_version kv ON kv.id=i.version_id
 JOIN kit_instance actual ON actual.id=NEW.kit_instance_id
 JOIN kit_instance_definition kd ON kd.kit_id=actual.id AND kd.definition_id=kv.definition_id
 WHERE i.id=NEW.kit_definition_item_id AND i.item_id=NEW.item_id
 AND actual.{ORG}=NEW.{ORG} AND actual.vehicle_id={vehicle} AND actual.active))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'; END IF;
"""


SAME_SCOPE_ROW = "AND rp.definition_id=NEW.definition_id"
SAME_SCOPE_ROW_NEW = (
    "AND rp.definition_id<=>NEW.definition_id "
    "AND rp.kit_definition_item_id<=>NEW.kit_definition_item_id"
)
DEFICIENCY_VEHICLE = "(SELECT d.vehicle_id FROM deficiency d WHERE d.id=NEW.deficiency_id)"


def scope_insert():
    sql = expansion.scope_insert()
    sql = _replace(
        sql,
        "IF NOT EXISTS (SELECT 1 FROM checklist_item_def cd WHERE cd.id=NEW.definition_id\n AND cd.item_id=NEW.item_id)",
        "IF NEW.definition_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM checklist_item_def cd WHERE cd.id=NEW.definition_id\n AND cd.item_id=NEW.item_id)",
    )
    sql = _replace(
        sql,
        "(NEW.kit_instance_id IS NOT NULL AND NOT EXISTS (\n  SELECT 1 FROM checklist_item_def cd",
        "(NEW.definition_id IS NOT NULL AND NEW.kit_instance_id IS NOT NULL AND NOT EXISTS (\n"
        "  SELECT 1 FROM checklist_item_def cd",
    )
    sql = _replace(sql, SAME_SCOPE_ROW, SAME_SCOPE_ROW_NEW, count=2)
    sql = _replace(
        sql,
        "AND a.item_id=NEW.item_id AND a.definition_id=NEW.definition_id",
        "AND a.item_id=NEW.item_id AND a.definition_id<=>NEW.definition_id "
        "AND a.kit_definition_item_id<=>NEW.kit_definition_item_id",
    )
    sql = _replace(
        sql,
        "JOIN checklist_item_def cd ON cd.id=NEW.definition_id\n   LEFT JOIN compartment def_slot",
        "LEFT JOIN checklist_item_def cd ON cd.id=NEW.definition_id\n   LEFT JOIN compartment def_slot",
    )
    assert sql.endswith("END")
    return (
        sql[:-3]
        + kit_content_fit(
            "verification kit instance does not have the content definition", DEFICIENCY_VEHICLE
        )
        + "END"
    )


def expansion_insert():
    sql = _replace(expansion.expansion_insert(), OLD_KIT_FIT, NEW_KIT_FIT)
    assert sql.endswith("END")
    return (
        sql[:-3]
        + kit_content_fit(
            "scope expansion kit instance does not have the content definition", DEFICIENCY_VEHICLE
        )
        + "END"
    )


def suspension_insert():
    sql = _replace(suspension.suspension_insert(), OLD_KIT_FIT, NEW_KIT_FIT)
    sql = _replace(
        sql,
        "AND s.item_id=NEW.item_id AND s.definition_id=NEW.definition_id",
        "AND s.item_id=NEW.item_id AND s.definition_id<=>NEW.definition_id "
        "AND s.kit_definition_item_id<=>NEW.kit_definition_item_id",
    )
    marker = "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check suspension kit instance does not fit the item'; END IF;\n"
    fit = kit_content_fit(
        "check suspension kit instance does not have the content definition", "NEW.vehicle_id"
    )
    return _replace(sql, marker, marker + " " + fit.replace("\n", "\n "))


TRIGGERS = [
    ("verification_scope_row", scope_insert, expansion.scope_insert),
    ("deficiency_scope_expansion", expansion_insert, expansion.expansion_insert),
    ("check_suspension", suspension_insert, suspension.suspension_insert),
]

FORWARD = [
    *ADD_DEFINITION_KEYS,
    *(
        f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` {body} ON DELETE RESTRICT ON UPDATE RESTRICT"
        for table, name, body in NEW_KEYS
    ),
]
REVERSE = [*DROP_DEFINITION_KEYS]
for table, new, old in TRIGGERS:
    FORWARD += [f"DROP TRIGGER cya_{table}_bi", new()]
    REVERSE += [f"DROP TRIGGER cya_{table}_bi", old()]
REVERSE += [f"ALTER TABLE `{table}` DROP FOREIGN KEY `{name}`" for table, name, _ in NEW_KEYS]
REVERSE += [f"ALTER TABLE `{table}` DROP INDEX `{name}`" for table, name, _ in NEW_KEYS]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0004_compartment_sealable"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0019_check_suspension_end"),
        ("cya_kits", "0008_kit_definition_item_id_item"),
    ]

    operations = [
        migrations.RunSQL(DROP_DEFINITION_KEYS, reverse_sql=ADD_DEFINITION_KEYS),
        migrations.AddField(
            model_name="checksuspension",
            name="kit_definition_item",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.kitdefinitionitem",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyaffecteditem",
            name="kit_definition_item",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.kitdefinitionitem",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="kit_definition_item",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.kitdefinitionitem",
            ),
        ),
        migrations.AddField(
            model_name="verificationscoperow",
            name="kit_definition_item",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.kitdefinitionitem",
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="checksuspension",
                    name="definition",
                    field=models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.checklistitemdef",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    nullable_definition("check_suspension", True),
                    reverse_sql=nullable_definition("check_suspension", False),
                )
            ],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="deficiencyaffecteditem",
                    name="definition",
                    field=models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.checklistitemdef",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    nullable_definition("deficiency_affected_item", True),
                    reverse_sql=nullable_definition("deficiency_affected_item", False),
                )
            ],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="deficiencyscopeexpansion",
                    name="definition",
                    field=models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.checklistitemdef",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    nullable_definition("deficiency_scope_expansion", True),
                    reverse_sql=nullable_definition("deficiency_scope_expansion", False),
                )
            ],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="verificationscoperow",
                    name="definition",
                    field=models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.checklistitemdef",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    nullable_definition("verification_scope_row", True),
                    reverse_sql=nullable_definition("verification_scope_row", False),
                )
            ],
        ),
        migrations.AddConstraint(
            model_name="checksuspension",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("definition__isnull", False), ("kit_definition_item__isnull", True)),
                    models.Q(("definition__isnull", True), ("kit_definition_item__isnull", False)),
                    _connector="OR",
                ),
                name="checksusp_content_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyaffecteditem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("definition__isnull", False), ("kit_definition_item__isnull", True)),
                    models.Q(("definition__isnull", True), ("kit_definition_item__isnull", False)),
                    _connector="OR",
                ),
                name="defaffect_content_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyaffecteditem",
            constraint=models.UniqueConstraint(
                fields=("deficiency", "item", "kit_definition_item"), name="defaffect_kit_scope"
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyscopeexpansion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("definition__isnull", False), ("kit_definition_item__isnull", True)),
                    models.Q(("definition__isnull", True), ("kit_definition_item__isnull", False)),
                    _connector="OR",
                ),
                name="defexp_content_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="verificationscoperow",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("definition__isnull", False), ("kit_definition_item__isnull", True)),
                    models.Q(("definition__isnull", True), ("kit_definition_item__isnull", False)),
                    _connector="OR",
                ),
                name="verscope_content_source",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
