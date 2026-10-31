"""Schema-owner guards: UUID/value checks, organization-pair keys, freeze triggers.

Published checklist data is frozen by the database in addition to command
validation (contracts section 2, SPEC AC-25). A template version or override
version is published exactly when a checklist_publication row names it, so the
freeze triggers test for that row. A frozen definition or override entry also
rejects INSERT, otherwise a row could be added to a published version.
Draft rows stay changeable only by the schema owner; the runtime account holds
no UPDATE or DELETE on versioned tables (SPEC 17.7 item 1) and edits a draft by
inserting a new revision (CHK-2).

Triggers are single statements or BEGIN..END blocks in one-item lists: Django
would split a plain string at the semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

UUID_TABLES = (
    "vehicle",
    "compartment",
    "vehicle_system",
    "item",
    "kit_instance",
    "checklist_template",
    "checklist_version",
    "vehicle_override_version",
    "checklist_publication",
    "checklist_item_def",
    "vehicle_override_item",
    "stocking_deviation",
)
ORG = "organization_id"


def org_fk(name, table, column, parent, parent_key="id"):
    return (
        table,
        f"cya_chk_{name}_org_fk",
        f"{ORG},{column}",
        parent,
        f"{ORG},{parent_key}",
    )


FKS = [
    org_fk("compartment_vehicle", "compartment", "vehicle_id", "vehicle"),
    org_fk("system_vehicle", "vehicle_system", "vehicle_id", "vehicle"),
    org_fk("kit_vehicle", "kit_instance", "vehicle_id", "vehicle"),
    org_fk("kit_compartment", "kit_instance", "compartment_id", "compartment"),
    org_fk("version_template", "checklist_version", "template_id", "checklist_template"),
    org_fk("override_vehicle", "vehicle_override_version", "vehicle_id", "vehicle"),
    org_fk(
        "override_template_version",
        "vehicle_override_version",
        "template_version_id",
        "checklist_version",
    ),
    org_fk("publication_command", "checklist_publication", "command_id", "command_receipt"),
    org_fk("publication_actor", "checklist_publication", "actor_user_id", "user"),
    org_fk(
        "publication_version", "checklist_publication", "checklist_version_id", "checklist_version"
    ),
    org_fk(
        "publication_override",
        "checklist_publication",
        "override_version_id",
        "vehicle_override_version",
    ),
    org_fk("itemdef_version", "checklist_item_def", "checklist_version_id", "checklist_version"),
    org_fk(
        "itemdef_override", "checklist_item_def", "override_version_id", "vehicle_override_version"
    ),
    org_fk("itemdef_item", "checklist_item_def", "item_id", "item"),
    org_fk("itemdef_compartment", "checklist_item_def", "compartment_id", "compartment"),
    org_fk("itemdef_system", "checklist_item_def", "system_id", "vehicle_system"),
    org_fk("itemdef_kit", "checklist_item_def", "kit_id", "kit_instance"),
    org_fk(
        "overrideitem_version",
        "vehicle_override_item",
        "override_version_id",
        "vehicle_override_version",
    ),
    org_fk("overrideitem_item", "vehicle_override_item", "item_id", "item"),
    org_fk(
        "overrideitem_definition", "vehicle_override_item", "definition_id", "checklist_item_def"
    ),
    org_fk("deviation_command", "stocking_deviation", "command_id", "command_receipt"),
    org_fk("deviation_vehicle", "stocking_deviation", "vehicle_id", "vehicle"),
    org_fk("deviation_item", "stocking_deviation", "item_id", "item"),
    org_fk("deviation_approver", "stocking_deviation", "approver_id", "user"),
    org_fk("deviation_prior", "stocking_deviation", "prior_revision_id", "stocking_deviation"),
    # A kit's compartment must be on the kit's vehicle.
    (
        "kit_instance",
        "cya_chk_kit_slot_vehicle_fk",
        "vehicle_id,compartment_id",
        "compartment",
        "vehicle_id,id",
    ),
    # An override entry's definition must belong to the same override and name the same item.
    (
        "vehicle_override_item",
        "cya_chk_overrideitem_owner_fk",
        "override_version_id,definition_id",
        "checklist_item_def",
        "override_version_id,id",
    ),
    (
        "vehicle_override_item",
        "cya_chk_overrideitem_same_item_fk",
        "definition_id,item_id",
        "checklist_item_def",
        "id,item_id",
    ),
]

ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
VALUE_CHECKS = [
    (
        "checklist_item_def",
        "cya_chk_itemdef_outcomes",
        "JSON_TYPE(allowed_outcomes)='ARRAY' AND JSON_LENGTH(allowed_outcomes) BETWEEN 1 AND 3 "
        'AND JSON_CONTAINS(\'["PASSED","FAILED","UNABLE_TO_INSPECT"]\', allowed_outcomes) '
        "AND JSON_CONTAINS(allowed_outcomes, '\"PASSED\"')",
    ),
    (
        "checklist_publication",
        "cya_chk_publication_roles",
        f"JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time)",
    ),
]


def published(alias, columns):
    """SQL true when any of the alias's version columns names a published version."""
    return " OR ".join(
        f"({alias}.{column} IS NOT NULL AND EXISTS (SELECT 1 FROM checklist_publication p "
        f"WHERE p.{target} = {alias}.{column}))"
        for column, target in columns
    )


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def guard(name, timing_event, table, condition, message):
    """A BEFORE trigger raising `message` when `condition` holds."""
    return (
        f"CREATE TRIGGER {name} BEFORE {timing_event} ON {table} FOR EACH ROW "
        f"BEGIN IF {condition} THEN {signal(message)}; END IF; END"
    )


def frozen_triggers(table, columns, message):
    """UPDATE/DELETE test the old row, INSERT/UPDATE the new one, so nothing moves into a frozen owner."""
    return [
        guard(
            f"cya_{table}_frozen_bu",
            "UPDATE",
            table,
            f"{published('OLD', columns)} OR {published('NEW', columns)}",
            message,
        ),
        guard(f"cya_{table}_frozen_bd", "DELETE", table, published("OLD", columns), message),
    ]


VERSION = [("id", "checklist_version_id")]
OVERRIDE = [("id", "override_version_id")]
DEF_OWNER = [
    ("checklist_version_id", "checklist_version_id"),
    ("override_version_id", "override_version_id"),
]
ITEM_OWNER = [("override_version_id", "override_version_id")]


def version_triggers():
    return [
        *frozen_triggers("checklist_version", VERSION, "published checklist version is frozen"),
        *frozen_triggers(
            "vehicle_override_version", OVERRIDE, "published override version is frozen"
        ),
        *frozen_triggers(
            "checklist_item_def", DEF_OWNER, "published checklist definition is frozen"
        ),
        guard(
            "cya_checklist_item_def_frozen_bi",
            "INSERT",
            "checklist_item_def",
            published("NEW", DEF_OWNER),
            "published checklist definition is frozen",
        ),
        *frozen_triggers("vehicle_override_item", ITEM_OWNER, "published override entry is frozen"),
        guard(
            "cya_vehicle_override_item_frozen_bi",
            "INSERT",
            "vehicle_override_item",
            published("NEW", ITEM_OWNER),
            "published override entry is frozen",
        ),
    ]


def immutable_triggers(table):
    return [
        guard(f"cya_{table}_immutable_bu", "UPDATE", table, "TRUE", "versioned row is immutable"),
        guard(f"cya_{table}_immutable_bd", "DELETE", table, "TRUE", "versioned row is immutable"),
    ]


def identity_trigger(table):
    return guard(
        f"cya_{table}_identity_bu",
        "UPDATE",
        table,
        "NOT (BINARY OLD.id <=> BINARY NEW.id) OR NOT (BINARY OLD.organization_id <=> BINARY NEW.organization_id)",
        "row identity is immutable",
    )


# Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
APPEND_ONLY_TRIGGERS = [
    f"CREATE TRIGGER cya_checklist_publication_{suffix} BEFORE {operation} ON checklist_publication "
    f"FOR EACH ROW {signal('append-only table')}"
    for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
]

# The publication row must match its command receipt and name a version that
# already has content; "publish validates the complete resolved item set" is a
# command rule, but an empty publication is rejected here as well.
PUBLICATION_INSERT = """CREATE TRIGGER cya_checklist_publication_bi BEFORE INSERT ON checklist_publication
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.actor_user_id)
 AND r.recorded_at=NEW.recorded_at) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'publication receipt attribution mismatch';
END IF;
IF NEW.checklist_version_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM checklist_item_def d WHERE d.checklist_version_id=NEW.checklist_version_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'cannot publish an empty checklist version';
END IF;
IF NEW.override_version_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM vehicle_override_item i WHERE i.override_version_id=NEW.override_version_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'cannot publish an empty override version';
END IF;
END"""

# Revisions form one chain per vehicle and item (a unique prior_revision stops forks).
DEVIATION_INSERT = """CREATE TRIGGER cya_stocking_deviation_bi BEFORE INSERT ON stocking_deviation
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.approver_id)
 AND r.recorded_at=NEW.recorded_at) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'deviation receipt attribution mismatch';
END IF;
IF NEW.prior_revision_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM stocking_deviation d WHERE d.id=NEW.prior_revision_id
 AND d.vehicle_id=NEW.vehicle_id AND d.item_id=NEW.item_id AND d.revision=NEW.revision-1) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'deviation revision chain mismatch';
END IF;
END"""


def forward_statements():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_chk_{table}_uuid` CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
        for table in UUID_TABLES
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` CHECK ({condition})"
        for table, name, condition in VALUE_CHECKS
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` FOREIGN KEY ({columns}) "
        f"REFERENCES `{parent}` ({keys}) ON DELETE RESTRICT ON UPDATE RESTRICT"
        for table, name, columns, parent, keys in FKS
    ]
    statements += APPEND_ONLY_TRIGGERS
    statements += [PUBLICATION_INSERT, DEVIATION_INSERT, *version_triggers()]
    for table in ("compartment", "vehicle_system", "stocking_deviation"):
        statements += immutable_triggers(table)
    statements += [
        identity_trigger(table)
        for table in ("vehicle", "item", "kit_instance", "checklist_template")
    ]
    return statements


def reverse_statements():
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in forward_statements()
        if statement.startswith("CREATE TRIGGER ")
    ]
    drops += [f"ALTER TABLE `{t}` DROP FOREIGN KEY `{n}`" for t, n, *_ in reversed(FKS)]
    drops += [f"ALTER TABLE `{t}` DROP CONSTRAINT `{n}`" for t, n, _ in reversed(VALUE_CHECKS)]
    drops += [
        f"ALTER TABLE `{t}` DROP CONSTRAINT `cya_chk_{t}_uuid`" for t in reversed(UUID_TABLES)
    ]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checklists", "0001_initial"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_accounts", "0002_identity_guards"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
