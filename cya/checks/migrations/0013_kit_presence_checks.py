"""Kit presence checks (SPEC 2.0.0 4.6 KDEF-12, section 7.2; issue #129).

A check gains a third content source: a kit presence check names the kit list line it counted
(kit_requirement), with no item definition, no kit content entry and no kit. The schema owner adds
the organization-pair key and rewrites the insert guard (migration 0012's) with a presence branch:
the line is on a kit list of the check's vehicle, the item is the line definition's presence item,
the check's checklist version places that definition, and no kit is named. A correction keeps its
predecessor's line, as it keeps its other content source. The check-obligation link guard
(cya_obligations 0006's) gains the matching rule: a presence check links only to obligations whose
requirement version pins its line (KDEF-12).
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

previous = import_module("cya.checks.migrations.0012_kit_content_checks")
ORG = "organization_id"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


PRESENCE_CHECK = f"""IF NOT EXISTS (SELECT 1 FROM vehicle_kit_requirement l
 JOIN vehicle_kit_list_version v ON v.id=l.version_id
 JOIN kit_definition d ON d.id=l.definition_id
 JOIN checklist_kit_placement pl ON pl.definition_id=l.definition_id
  AND pl.checklist_version_id=NEW.checklist_version_id
 WHERE l.id=NEW.kit_requirement_id AND l.{ORG}=NEW.{ORG} AND v.vehicle_id=NEW.vehicle_id
 AND d.presence_item_id=NEW.item_id)
THEN {signal("check kit presence line does not match")}; END IF;
"""
PRESENCE_KIT_CHECK = f"""IF NEW.kit_id IS NOT NULL THEN
 {signal("a kit presence check names no kit")};
END IF;
"""
PREDECESSOR_OLD = previous.PREDECESSOR_NEW
PREDECESSOR_NEW = PREDECESSOR_OLD + " AND p.kit_requirement_id<=>NEW.kit_requirement_id"


def check_insert():
    """Migration 0012's insert guard with a presence branch (see the module docstring)."""
    sql = previous.check_insert()
    for old, alternative in (
        (previous.CONTENT_CHECK, PRESENCE_CHECK),
        (previous.KIT_CONTENT_CHECK, PRESENCE_KIT_CHECK),
    ):
        branch = "ELSE\n" + old
        assert sql.count(branch) == 1
        sql = sql.replace(
            branch,
            "ELSEIF NEW.kit_requirement_id IS NULL THEN\n" + old + "ELSE\n" + alternative,
        )
    assert sql.count(PREDECESSOR_OLD) == 1
    return sql.replace(PREDECESSOR_OLD, PREDECESSOR_NEW)


links = import_module("cya.obligations.migrations.0006_ad_hoc_obligations")
LINK_SCOPE_OLD = "AND o.kit_key=COALESCE(c.kit_id,'00000000-0000-0000-0000-000000000000')"
LINK_SCOPE_NEW = (
    "AND (c.kit_requirement_id IS NULL "
    "AND o.kit_key=COALESCE(c.kit_id,'00000000-0000-0000-0000-000000000000') "
    "OR c.kit_requirement_id IS NOT NULL AND EXISTS (SELECT 1 FROM requirement_version lv "
    "WHERE lv.id=o.requirement_version_id AND lv.kit_requirement_id=c.kit_requirement_id))"
)


def link_trigger():
    """The check-obligation link guard: a presence check links only to its line's obligations."""
    sql = links.link_trigger()
    assert sql.count(LINK_SCOPE_OLD) == 1
    return sql.replace(LINK_SCOPE_OLD, LINK_SCOPE_NEW)


KEY = "cya_check_record_kitreq_org_fk"
FORWARD = [
    f"ALTER TABLE check_record ADD CONSTRAINT `{KEY}` "
    f"FOREIGN KEY ({ORG},kit_requirement_id) REFERENCES `vehicle_kit_requirement` ({ORG},id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    "DROP TRIGGER cya_check_record_bi",
    check_insert(),
    "DROP TRIGGER cya_check_obligation_link_bi",
    link_trigger(),
]
REVERSE = [
    "DROP TRIGGER cya_check_obligation_link_bi",
    links.link_trigger(),
    "DROP TRIGGER cya_check_record_bi",
    previous.check_insert(),
    f"ALTER TABLE check_record DROP FOREIGN KEY `{KEY}`",
    f"ALTER TABLE check_record DROP INDEX `{KEY}`",
]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0004_compartment_sealable"),
        ("cya_checks", "0012_kit_content_checks"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0020_kit_content_scope"),
        ("cya_inspections", "0005_auto_close_guards"),
        ("cya_kits", "0010_kit_presence_item_guards"),
        ("cya_obligations", "0016_kit_presence_requirements"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="checkrecord",
            name="check_content_source",
        ),
        migrations.AddField(
            model_name="checkrecord",
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
            model_name="checkrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("item_def__isnull", False),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("item_def__isnull", True),
                        ("kit__isnull", False),
                        ("kit_definition_item__isnull", False),
                        ("kit_requirement__isnull", True),
                    ),
                    models.Q(
                        ("item_def__isnull", True),
                        ("kit__isnull", True),
                        ("kit_definition_item__isnull", True),
                        ("kit_requirement__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="check_content_source",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
