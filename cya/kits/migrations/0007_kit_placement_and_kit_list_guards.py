"""Schema-owner guards for kit placements and vehicle kit lists (SPEC 4.6 KDEF-4, KDEF-9).

As for checklist versions (cya_checklists 0002) and kit definition versions (0005), published
data is frozen by the database as well as by the commands:

- checklist_kit_placement is frozen with its checklist version, which is published when a
  checklist_publication row names it.
- vehicle_kit_list_version and vehicle_kit_requirement are frozen once a
  vehicle_kit_list_publication row names the version; a frozen version's lines also refuse
  INSERT.

A kit list line's compartment must be one of the list's own vehicle's rows.
vehicle_kit_list_publication is append-only (A), and its insert guard repeats the command rule so
a writer that bypasses the command still cannot store a row that does not match its command
receipt. Each trigger is one list element so Django runs it whole.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers

ORG = "organization_id"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
PLACEMENT = "checklist_kit_placement"
VERSION = "vehicle_kit_list_version"
LINE = "vehicle_kit_requirement"
PUBLICATION = "vehicle_kit_list_publication"
UUID_TABLES = (PLACEMENT, VERSION, LINE, PUBLICATION)
# (table, column, parent table)
FKS = [
    (PLACEMENT, "checklist_version_id", "checklist_version"),
    (PLACEMENT, "definition_id", "kit_definition"),
    (VERSION, "vehicle_id", "vehicle"),
    (LINE, "version_id", VERSION),
    (LINE, "definition_id", "kit_definition"),
    (LINE, "compartment_id", "compartment"),
    (PUBLICATION, "command_id", "command_receipt"),
    (PUBLICATION, "actor_user_id", "user"),
    (PUBLICATION, "version_id", VERSION),
]
ROLES = (
    "JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
    f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time)"
)
PUBLISH_COMMAND = "VEHICLE_KIT_LIST_PUBLISH"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def guard(name, timing_event, table, condition, message):
    return (
        f"CREATE TRIGGER {name} BEFORE {timing_event} ON {table} FOR EACH ROW "
        f"BEGIN IF {condition} THEN {signal(message)}; END IF; END"
    )


def checklist_published(alias):
    return (
        "EXISTS (SELECT 1 FROM checklist_publication p "
        f"WHERE p.checklist_version_id = {alias}.checklist_version_id)"
    )


def list_published(alias, column):
    return f"EXISTS (SELECT 1 FROM {PUBLICATION} p WHERE p.version_id = {alias}.{column})"


def freeze_triggers():
    placement_message = "published checklist kit placement is frozen"
    line_message = "published vehicle kit list is frozen"
    return [
        guard(
            f"cya_{PLACEMENT}_frozen_bu",
            "UPDATE",
            PLACEMENT,
            f"{checklist_published('OLD')} OR {checklist_published('NEW')}",
            placement_message,
        ),
        guard(
            f"cya_{PLACEMENT}_frozen_bd",
            "DELETE",
            PLACEMENT,
            checklist_published("OLD"),
            placement_message,
        ),
        guard(
            f"cya_{PLACEMENT}_frozen_bi",
            "INSERT",
            PLACEMENT,
            checklist_published("NEW"),
            placement_message,
        ),
        guard(
            f"cya_{VERSION}_frozen_bu",
            "UPDATE",
            VERSION,
            f"{list_published('OLD', 'id')} OR {list_published('NEW', 'id')}",
            line_message,
        ),
        guard(
            f"cya_{VERSION}_frozen_bd",
            "DELETE",
            VERSION,
            list_published("OLD", "id"),
            line_message,
        ),
        guard(
            f"cya_{LINE}_frozen_bu",
            "UPDATE",
            LINE,
            f"{list_published('OLD', 'version_id')} OR {list_published('NEW', 'version_id')}",
            line_message,
        ),
        guard(
            f"cya_{LINE}_frozen_bd",
            "DELETE",
            LINE,
            list_published("OLD", "version_id"),
            line_message,
        ),
    ]


LINE_INSERT = f"""CREATE TRIGGER cya_{LINE}_bi BEFORE INSERT ON {LINE}
FOR EACH ROW BEGIN
IF {list_published("NEW", "version_id")}
THEN {signal("published vehicle kit list is frozen")}; END IF;
IF NOT EXISTS (SELECT 1 FROM {VERSION} v JOIN compartment c ON c.id=NEW.compartment_id
 WHERE v.id=NEW.version_id AND v.{ORG}=NEW.{ORG} AND c.{ORG}=NEW.{ORG}
 AND c.vehicle_id=v.vehicle_id)
THEN {signal("kit list compartment is not on the list vehicle")}; END IF;
END"""

PUBLICATION_INSERT = f"""CREATE TRIGGER cya_{PUBLICATION}_bi BEFORE INSERT ON {PUBLICATION}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='{PUBLISH_COMMAND}')
THEN {signal("vehicle kit list publication receipt attribution mismatch")}; END IF;
END"""


def forward_statements():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
        for table in UUID_TABLES
    ]
    statements.append(
        f"ALTER TABLE `{PUBLICATION}` ADD CONSTRAINT `cya_{PUBLICATION}_roles` CHECK ({ROLES})"
    )
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for table, column, parent in FKS
    ]
    statements += freeze_triggers()
    statements += list(append_only_triggers(PUBLICATION))
    return [*statements, LINE_INSERT, PUBLICATION_INSERT]


def reverse_statements():
    drops = [f"DROP TRIGGER cya_{PUBLICATION}_bi", f"DROP TRIGGER cya_{LINE}_bi"]
    drops += [f"DROP TRIGGER cya_{PUBLICATION}_{suffix}" for suffix in ("bd", "bu")]
    drops += [
        f"DROP TRIGGER cya_{table}_frozen_{suffix}"
        for table, suffix in (
            (LINE, "bd"),
            (LINE, "bu"),
            (VERSION, "bd"),
            (VERSION, "bu"),
            (PLACEMENT, "bi"),
            (PLACEMENT, "bd"),
            (PLACEMENT, "bu"),
        )
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_{column}_org_fk`"
        for table, column, _ in reversed(FKS)
    ]
    drops.append(f"ALTER TABLE `{PUBLICATION}` DROP CONSTRAINT `cya_{PUBLICATION}_roles`")
    drops += [f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_uuid`" for table in UUID_TABLES]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_kits", "0006_kit_placements_and_vehicle_kit_lists"),
        ("cya_checklists", "0004_compartment_sealable"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_accounts", "0002_identity_guards"),
    ]

    operations = [
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
    ]
