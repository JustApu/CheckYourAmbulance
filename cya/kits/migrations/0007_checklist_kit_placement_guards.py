"""Schema-owner guards for checklist_kit_placement (SPEC 4.6 KDEF-4, issue #129).

A placement belongs to a template or override version and is frozen with it, exactly as
checklist_item_def is (cya_checklists 0002): a version is published when a
checklist_publication row names it, so UPDATE and DELETE test the old and new owner and INSERT
the new one, and nothing can be added to, changed in or removed from a published version. Each
trigger is one list element so Django runs it whole.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLE = "checklist_kit_placement"
FKS = [
    ("checklist_version_id", "checklist_version"),
    ("override_version_id", "vehicle_override_version"),
    ("definition_id", "kit_definition"),
    ("compartment_id", "compartment"),
]
OWNERS = [
    ("checklist_version_id", "checklist_version_id"),
    ("override_version_id", "override_version_id"),
]
MESSAGE = "published checklist kit placement is frozen"


def published(alias):
    return " OR ".join(
        f"({alias}.{column} IS NOT NULL AND EXISTS (SELECT 1 FROM checklist_publication p "
        f"WHERE p.{target} = {alias}.{column}))"
        for column, target in OWNERS
    )


def guard(suffix, event, condition):
    return (
        f"CREATE TRIGGER cya_{TABLE}_frozen_{suffix} BEFORE {event} ON {TABLE} FOR EACH ROW "
        f"BEGIN IF {condition} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{MESSAGE}'; "
        "END IF; END"
    )


def forward_statements():
    statements = [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
    ]
    statements += [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for column, parent in FKS
    ]
    statements += [
        guard("bu", "UPDATE", f"{published('OLD')} OR {published('NEW')}"),
        guard("bd", "DELETE", published("OLD")),
        guard("bi", "INSERT", published("NEW")),
    ]
    return statements


def reverse_statements():
    drops = [f"DROP TRIGGER cya_{TABLE}_frozen_{suffix}" for suffix in ("bi", "bd", "bu")]
    drops += [
        f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_{column}_org_fk`"
        for column, _ in reversed(FKS)
    ]
    drops.append(f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`")
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_kits", "0006_checklist_kit_placement"),
        ("cya_checklists", "0004_compartment_sealable"),
    ]

    operations = [
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
    ]
