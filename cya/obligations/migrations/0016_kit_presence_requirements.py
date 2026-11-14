"""Kit presence requirements (SPEC 2.0.0 4.6 KDEF-10 to KDEF-12, issue #129).

A requirement version gains a third content source: a kit presence requirement names its kit list
line (kit_requirement) and the checklist placement of the line's definition, with no item
definition, no kit content entry and no kit instance. Its item is the definition's presence item.

kit_key is the internal unit key of an obligation stream. It stays the kit instance for an item
inside a kit and NO_KIT for any other item, but a presence requirement uses its line's unit key
(cya.kits.presence.line_unit_key: the vehicle, definition and compartment slot, never the count
or revision), so two lines of one definition are separate streams while a count change stays on
the same stream. The SPEC-facing kit_instance_id stays NULL. Obligation and selection rows reach
the requirement through the composite anchor keys, so their own kit_key check only requires an
instance to equal the key.

The schema owner adds the organization-pair key for kit_requirement and rewrites the
requirement insert guard (migration 0015's) with a presence branch: the line belongs to a
published kit list version of the same vehicle, the placement belongs to the pinned publication's
checklist version and places the line's definition, and the item is that definition's presence
item.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

previous = import_module("cya.obligations.migrations.0015_kit_content_requirements")
ORG = "organization_id"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


PRESENCE_CHECK = f"""IF NOT EXISTS (SELECT 1 FROM vehicle_kit_requirement l
 JOIN vehicle_kit_list_version v ON v.id=l.version_id
 JOIN vehicle_kit_list_publication lp ON lp.version_id=v.id
 JOIN checklist_kit_placement pl ON pl.id=NEW.placement_id
 JOIN checklist_publication p ON p.id=NEW.publication_id
 JOIN kit_definition d ON d.id=l.definition_id
 WHERE l.id=NEW.kit_requirement_id AND l.{ORG}=NEW.{ORG} AND v.vehicle_id=NEW.vehicle_id
 AND pl.checklist_version_id=p.checklist_version_id AND pl.definition_id=l.definition_id
 AND d.presence_item_id=NEW.item_id)
THEN {signal("requirement kit presence mismatch")}; END IF;
"""

PRESENCE_INSTANCE_CHECK = f"""IF NEW.kit_instance_id IS NOT NULL THEN
 {signal("kit presence requirement names no kit instance")};
END IF;
"""


def version_trigger():
    """Migration 0015's insert guard with a presence branch (see the module docstring)."""
    sql = previous.version_trigger()
    content = "ELSE\n" + previous.CONTENT_CHECK
    instance = "ELSE\n" + previous.KIT_CONTENT_INSTANCE_CHECK
    assert sql.count(content) == 1 and sql.count(instance) == 1
    sql = sql.replace(
        content,
        "ELSEIF NEW.kit_requirement_id IS NULL THEN\n"
        + previous.CONTENT_CHECK
        + "ELSE\n"
        + PRESENCE_CHECK,
    )
    return sql.replace(
        instance,
        "ELSEIF NEW.kit_requirement_id IS NULL THEN\n"
        + previous.KIT_CONTENT_INSTANCE_CHECK
        + "ELSE\n"
        + PRESENCE_INSTANCE_CHECK,
    )


KEY = "cya_obl_reqversion_kitreq_org_fk"
FORWARD = [
    f"ALTER TABLE requirement_version ADD CONSTRAINT `{KEY}` "
    f"FOREIGN KEY ({ORG},kit_requirement_id) REFERENCES `vehicle_kit_requirement` ({ORG},id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    "DROP TRIGGER cya_requirement_version_bi",
    version_trigger(),
]
REVERSE = [
    "DROP TRIGGER cya_requirement_version_bi",
    previous.version_trigger(),
    f"ALTER TABLE requirement_version DROP FOREIGN KEY `{KEY}`",
    f"ALTER TABLE requirement_version DROP INDEX `{KEY}`",
]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0004_compartment_sealable"),
        ("cya_conditions", "0003_conditions_receipt_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_kits", "0010_kit_presence_item_guards"),
        ("cya_obligations", "0015_kit_content_requirements"),
        ("cya_schedules", "0003_schedule_command_type_guards"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="obligation",
            name="obligation_kit_key",
        ),
        migrations.RemoveConstraint(
            model_name="obligationselection",
            name="selection_kit_key",
        ),
        migrations.RemoveConstraint(
            model_name="requirementversion",
            name="reqversion_kit_key",
        ),
        migrations.RemoveConstraint(
            model_name="requirementversion",
            name="reqversion_content_source",
        ),
        migrations.AddField(
            model_name="requirementversion",
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
            model_name="obligation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("kit_instance__isnull", True),
                    models.Q(
                        ("kit_instance", models.F("kit_key")), ("kit_instance__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="obligation_kit_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="obligationselection",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("kit_instance__isnull", True),
                    models.Q(
                        ("kit_instance", models.F("kit_key")), ("kit_instance__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="selection_kit_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="requirementversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kit_instance__isnull", True),
                        ("kit_key", "00000000-0000-0000-0000-000000000000"),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("kit_instance", models.F("kit_key")), ("kit_instance__isnull", False)
                    ),
                    models.Q(
                        ("kit_instance__isnull", True),
                        ("kit_requirement__isnull", False),
                        models.Q(
                            ("kit_key", "00000000-0000-0000-0000-000000000000"), _negated=True
                        ),
                    ),
                    _connector="OR",
                ),
                name="reqversion_kit_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="requirementversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("item_def__isnull", False),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", True),
                        ("placement__isnull", True),
                    ),
                    models.Q(
                        ("item_def__isnull", True),
                        ("kit_definition_item__isnull", False),
                        ("kit_instance__isnull", False),
                        ("kit_requirement__isnull", True),
                        ("placement__isnull", False),
                    ),
                    models.Q(
                        ("item_def__isnull", True),
                        ("kit_definition_item__isnull", True),
                        ("kit_instance__isnull", True),
                        ("kit_requirement__isnull", False),
                        ("placement__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="reqversion_content_source",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
