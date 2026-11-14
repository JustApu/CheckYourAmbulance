"""Deficiency scope over kit presence lines (SPEC 2.0.0 4.6 KDEF-12, issue #129).

deficiency_affected_item, verification_scope_row, deficiency_scope_expansion and check_suspension
gain a third content source: a kit list line (kit_requirement), for a deficiency a failed kit
presence check opens. Exactly one of definition, kit_definition_item and kit_requirement is set
(the *_content_source checks). The schema owner adds organization-pair keys and rewrites the three
insert guards of migration 0020: every "same row" comparison also compares the line null-safely,
and a presence row must name no kit instance, a line on a kit list of the deficiency's vehicle,
and that line definition's presence item.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

previous = import_module("cya.deficiencies.migrations.0020_kit_content_scope")
ORG = "organization_id"
TABLES = (
    "deficiency_affected_item",
    "verification_scope_row",
    "deficiency_scope_expansion",
    "check_suspension",
)


def presence_fit(message, vehicle, has_kit=True):
    kit = "NEW.kit_instance_id IS NOT NULL OR " if has_kit else ""
    return f"""IF NEW.kit_requirement_id IS NOT NULL AND ({kit}NOT EXISTS (
 SELECT 1 FROM vehicle_kit_requirement l
 JOIN vehicle_kit_list_version v ON v.id=l.version_id
 JOIN kit_definition d ON d.id=l.definition_id
 WHERE l.id=NEW.kit_requirement_id AND l.{ORG}=NEW.{ORG} AND v.vehicle_id={vehicle}
 AND d.presence_item_id=NEW.item_id))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'; END IF;
"""


def _same_line(sql, alias, count=1):
    before = f"AND {alias}.kit_definition_item_id<=>NEW.kit_definition_item_id"
    after = before + f" AND {alias}.kit_requirement_id<=>NEW.kit_requirement_id"
    return previous._replace(sql, before, after, count)


def _append(sql, block):
    assert sql.endswith("END")
    return sql[:-3] + block + "END"


def scope_insert():
    sql = _same_line(previous.scope_insert(), "rp", count=2)
    sql = _same_line(sql, "a")
    return _append(
        sql,
        presence_fit(
            "verification presence line does not fit the item", previous.DEFICIENCY_VEHICLE
        ),
    )


def expansion_insert():
    return _append(
        previous.expansion_insert(),
        presence_fit(
            "scope expansion presence line does not fit the item", previous.DEFICIENCY_VEHICLE
        ),
    )


def suspension_insert():
    sql = _same_line(previous.suspension_insert(), "s")
    return _append(
        sql, presence_fit("check suspension presence line does not fit the item", "NEW.vehicle_id")
    )


TRIGGERS = [
    ("verification_scope_row", scope_insert, previous.scope_insert),
    ("deficiency_scope_expansion", expansion_insert, previous.expansion_insert),
    ("check_suspension", suspension_insert, previous.suspension_insert),
]


def key(table):
    return f"cya_{table}_kitreq_org_fk"


FORWARD = [
    f"ALTER TABLE `{table}` ADD CONSTRAINT `{key(table)}` FOREIGN KEY ({ORG},kit_requirement_id) "
    f"REFERENCES `vehicle_kit_requirement` ({ORG},id) ON DELETE RESTRICT ON UPDATE RESTRICT"
    for table in TABLES
]
REVERSE = []
for table, new, old in TRIGGERS:
    FORWARD += [f"DROP TRIGGER cya_{table}_bi", new()]
    REVERSE += [f"DROP TRIGGER cya_{table}_bi", old()]
for table in TABLES:
    REVERSE += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `{key(table)}`",
        f"ALTER TABLE `{table}` DROP INDEX `{key(table)}`",
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0004_compartment_sealable"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0020_kit_content_scope"),
        ("cya_kits", "0010_kit_presence_item_guards"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="checksuspension",
            name="checksusp_content_source",
        ),
        migrations.RemoveConstraint(
            model_name="deficiencyaffecteditem",
            name="defaffect_content_source",
        ),
        migrations.RemoveConstraint(
            model_name="deficiencyscopeexpansion",
            name="defexp_content_source",
        ),
        migrations.RemoveConstraint(
            model_name="verificationscoperow",
            name="verscope_content_source",
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="kit_requirement",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.vehiclekitrequirement",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyaffecteditem",
            name="kit_requirement",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.vehiclekitrequirement",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="kit_requirement",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.vehiclekitrequirement",
            ),
        ),
        migrations.AddField(
            model_name="verificationscoperow",
            name="kit_requirement",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.vehiclekitrequirement",
            ),
        ),
        migrations.AddConstraint(
            model_name="checksuspension",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("definition__isnull", False),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("definition__isnull", True),
                        ("kit_definition_item__isnull", False),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("definition__isnull", True),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="checksusp_content_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyaffecteditem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("definition__isnull", False),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("definition__isnull", True),
                        ("kit_definition_item__isnull", False),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("definition__isnull", True),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="defaffect_content_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyscopeexpansion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("definition__isnull", False),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("definition__isnull", True),
                        ("kit_definition_item__isnull", False),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("definition__isnull", True),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="defexp_content_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="verificationscoperow",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("definition__isnull", False),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("definition__isnull", True),
                        ("kit_definition_item__isnull", False),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("definition__isnull", True),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="verscope_content_source",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
