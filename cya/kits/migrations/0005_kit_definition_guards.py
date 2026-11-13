"""Schema-owner guards for kit definitions (SPEC 4.6 KDEF-1 to KDEF-8, issue #129).

As for checklist versions (cya_checklists 0002), published kit definition data is frozen by the
database as well as by the commands: a version is published exactly when a
kit_definition_publication row names it, so the freeze triggers test for that row, and a frozen
version's contents also refuse INSERT. The runtime account holds no UPDATE or DELETE on the
versioned tables and edits a draft by inserting a new revision.

kit_definition_publication and kit_instance_definition are append-only (A): update/delete
triggers, plus insert guards that repeat the command rules so a writer that bypasses the
command still cannot store a row that does not match its command receipt (organization, actor,
recorded instant, command type). A publication must name a version that has contents; a kit
instance may be given only an active definition (KDEF-2), and only by the command that created it:
the receipt must be that of the kit's first kit_assignment_event and must record this definition as
requested at creation (KDEF-3), so an existing kit can never be given a definition later, even by
reusing its creation receipt. kit_definition's key is immutable
(KDEF-1, IMP-1: files match by it). Each trigger is one list element so Django runs it whole.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers, immutable_header_guard

ORG = "organization_id"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
UUID_TABLES = (
    "kit_definition",
    "kit_definition_version",
    "kit_definition_item",
    "kit_definition_publication",
    "kit_instance_definition",
)
# (table, column, parent table, parent key)
FKS = [
    ("kit_definition_version", "definition_id", "kit_definition", "id"),
    ("kit_definition_item", "version_id", "kit_definition_version", "id"),
    ("kit_definition_item", "item_id", "item", "id"),
    ("kit_definition_publication", "command_id", "command_receipt", "id"),
    ("kit_definition_publication", "actor_user_id", "user", "id"),
    ("kit_definition_publication", "version_id", "kit_definition_version", "id"),
    ("kit_instance_definition", "command_id", "command_receipt", "id"),
    ("kit_instance_definition", "actor_user_id", "user", "id"),
    ("kit_instance_definition", "kit_id", "kit_instance", "id"),
    ("kit_instance_definition", "definition_id", "kit_definition", "id"),
]
ROLES = (
    "JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
    f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time)"
)
VALUE_CHECKS = [
    (
        "kit_definition_item",
        "outcomes",
        "JSON_TYPE(allowed_outcomes)='ARRAY' AND JSON_LENGTH(allowed_outcomes) BETWEEN 1 AND 3 "
        'AND JSON_CONTAINS(\'["PASSED","FAILED","UNABLE_TO_INSPECT"]\', allowed_outcomes) '
        "AND JSON_CONTAINS(allowed_outcomes, '\"PASSED\"')",
    ),
    ("kit_definition_publication", "roles", ROLES),
    ("kit_instance_definition", "roles", ROLES),
]
PUBLISH_COMMAND = "KIT_DEFINITION_PUBLISH"
ADD_KIT_COMMAND = "KIT_ADD"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def guard(name, timing_event, table, condition, message):
    return (
        f"CREATE TRIGGER {name} BEFORE {timing_event} ON {table} FOR EACH ROW "
        f"BEGIN IF {condition} THEN {signal(message)}; END IF; END"
    )


def published(alias, column):
    return (
        f"EXISTS (SELECT 1 FROM kit_definition_publication p WHERE p.version_id = {alias}.{column})"
    )


def freeze_triggers():
    version, item = "kit_definition_version", "kit_definition_item"
    return [
        guard(
            f"cya_{version}_frozen_bu",
            "UPDATE",
            version,
            f"{published('OLD', 'id')} OR {published('NEW', 'id')}",
            "published kit definition version is frozen",
        ),
        guard(
            f"cya_{version}_frozen_bd",
            "DELETE",
            version,
            published("OLD", "id"),
            "published kit definition version is frozen",
        ),
        guard(
            f"cya_{item}_frozen_bu",
            "UPDATE",
            item,
            f"{published('OLD', 'version_id')} OR {published('NEW', 'version_id')}",
            "published kit definition content is frozen",
        ),
        guard(
            f"cya_{item}_frozen_bd",
            "DELETE",
            item,
            published("OLD", "version_id"),
            "published kit definition content is frozen",
        ),
        guard(
            f"cya_{item}_frozen_bi",
            "INSERT",
            item,
            published("NEW", "version_id"),
            "published kit definition content is frozen",
        ),
    ]


def receipt_matches(command_type):
    return (
        "NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id "
        f"AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id "
        f"AND r.recorded_at=NEW.recorded_at AND r.command_type='{command_type}')"
    )


PUBLICATION_INSERT = f"""CREATE TRIGGER cya_kit_definition_publication_bi BEFORE INSERT ON kit_definition_publication
FOR EACH ROW BEGIN
IF {receipt_matches(PUBLISH_COMMAND)}
THEN {signal("kit definition publication receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM kit_definition_item i WHERE i.version_id=NEW.version_id)
THEN {signal("cannot publish an empty kit definition version")}; END IF;
END"""

INSTANCE_DEFINITION_INSERT = f"""CREATE TRIGGER cya_kit_instance_definition_bi BEFORE INSERT ON kit_instance_definition
FOR EACH ROW BEGIN
IF {receipt_matches(ADD_KIT_COMMAND)}
THEN {signal("kit instance definition receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM kit_assignment_event e WHERE e.kit_id=NEW.kit_id
 AND e.{ORG}=NEW.{ORG} AND e.sequence=1 AND e.command_id=NEW.command_id)
THEN {signal("kit definition must be given by the command that created the kit")}; END IF;
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND JSON_VALUE(r.response_body, '$.definition_id') = NEW.definition_id)
THEN {signal("kit definition was not requested when the kit was created")}; END IF;
IF NOT EXISTS (SELECT 1 FROM kit_definition d WHERE d.id=NEW.definition_id
 AND d.{ORG}=NEW.{ORG} AND d.active)
THEN {signal("kit definition is inactive")}; END IF;
END"""


def forward_statements():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
        for table in UUID_TABLES
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_{name}` CHECK ({condition})"
        for table, name, condition in VALUE_CHECKS
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for table, column, parent, key in FKS
    ]
    statements += freeze_triggers()
    statements.append(immutable_header_guard("kit_definition"))
    for table in ("kit_definition_publication", "kit_instance_definition"):
        statements += list(append_only_triggers(table))
    return [*statements, PUBLICATION_INSERT, INSTANCE_DEFINITION_INSERT]


def reverse_statements():
    drops = [
        "DROP TRIGGER cya_kit_instance_definition_bi",
        "DROP TRIGGER cya_kit_definition_publication_bi",
    ]
    for table in ("kit_instance_definition", "kit_definition_publication"):
        drops += [f"DROP TRIGGER cya_{table}_{suffix}" for suffix in ("bd", "bu")]
    drops.append("DROP TRIGGER cya_kit_definition_immutable_bu")
    drops += [
        f"DROP TRIGGER cya_{table}_frozen_{suffix}"
        for table, suffix in (
            ("kit_definition_item", "bi"),
            ("kit_definition_item", "bd"),
            ("kit_definition_item", "bu"),
            ("kit_definition_version", "bd"),
            ("kit_definition_version", "bu"),
        )
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_{column}_org_fk`"
        for table, column, *_ in reversed(FKS)
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_{name}`"
        for table, name, _ in reversed(VALUE_CHECKS)
    ]
    drops += [f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_uuid`" for table in UUID_TABLES]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_kits", "0004_kit_definitions"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_accounts", "0002_identity_guards"),
    ]

    operations = [
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
    ]
