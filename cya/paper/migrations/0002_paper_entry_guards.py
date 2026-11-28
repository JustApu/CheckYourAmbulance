"""Guards for the paper-entry batch header tables (SPEC 8.0.0 LATE-2 to LATE-4, §18.4; issue #9).

paper_entry_batch, paper_entry_performer and paper_entry_kit_version are append-only (A). As
for kit_seal_designation (cya_kits 0003), the schema owner adds UUID and role checks,
organization-pair foreign keys, the append-only update/delete triggers the audit schema
inspector requires on A tables, and an insert trigger per table that repeats the command's
rules, so a writer that bypasses cya.paper.commands still cannot store:

- a batch that does not match its PAPER_ENTRY_BATCH_RECORD receipt (organization, actor,
  recorded instant, command type), or whose checklist version is not a version of that
  vehicle's own checklist published by the entry instant and in force by the performance
  instant (LATE-4);
- a performer or kit-version row written by another command than its batch's;
- a performer other than the entry actor unless the actor's recorded roles include Supervisor or
  Administrator, or a self-attribution by an actor holding none of the entering roles (§3.3,
  LATE-3);
- a kit-definition version that was not published by the entry instant, was not in force at
  the performance instant, or whose definition the batch's checklist version does not place.

Each trigger is one list element, so Django runs it whole; Django would split a single SQL
string at the semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
COMMAND_TYPE = "PAPER_ENTRY_BATCH_RECORD"

BATCH = "paper_entry_batch"
PERFORMER = "paper_entry_performer"
KIT_VERSION = "paper_entry_kit_version"

FKS = {
    BATCH: [
        ("command_id", "command_receipt", "id"),
        ("actor_user_id", "user", "id"),
        ("vehicle_id", "vehicle", "id"),
        ("checklist_version_id", "checklist_version", "id"),
    ],
    PERFORMER: [
        ("command_id", "command_receipt", "id"),
        ("actor_user_id", "user", "id"),
        ("batch_id", BATCH, "id"),
        ("performer_id", "user", "id"),
    ],
    KIT_VERSION: [
        ("command_id", "command_receipt", "id"),
        ("actor_user_id", "user", "id"),
        ("batch_id", BATCH, "id"),
        ("kit_definition_version_id", "kit_definition_version", "id"),
    ],
}


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


RECEIPT = f"""IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='{COMMAND_TYPE}')
THEN {signal("paper entry receipt attribution mismatch")}; END IF;"""

# The child row belongs to its batch's own command: same receipt, instant and actor.
SAME_COMMAND = f"""{RECEIPT}
IF NOT EXISTS (SELECT 1 FROM {BATCH} b WHERE b.id=NEW.batch_id
 AND b.{ORG}=NEW.{ORG} AND b.command_id=NEW.command_id AND b.recorded_at=NEW.recorded_at
 AND b.actor_user_id=NEW.actor_user_id AND b.actor_roles_at_time=NEW.actor_roles_at_time)
THEN {signal("paper entry row is not of its batch command")}; END IF;"""

GUARDS = {
    BATCH: f"""CREATE TRIGGER cya_{BATCH}_bi BEFORE INSERT ON {BATCH}
FOR EACH ROW BEGIN
{RECEIPT}
IF NOT EXISTS (SELECT 1 FROM checklist_version v
 JOIN checklist_template t ON t.id=v.template_id AND t.{ORG}=v.{ORG}
 JOIN checklist_publication p ON p.checklist_version_id=v.id AND p.{ORG}=v.{ORG}
 WHERE v.id=NEW.checklist_version_id AND v.{ORG}=NEW.{ORG} AND t.vehicle_id=NEW.vehicle_id
 AND p.recorded_at<=NEW.recorded_at AND p.effective_at<=NEW.performed_at)
THEN {signal("paper entry batch checklist version not in force for the vehicle")}; END IF;
END""",
    PERFORMER: f"""CREATE TRIGGER cya_{PERFORMER}_bi BEFORE INSERT ON {PERFORMER}
FOR EACH ROW BEGIN
{SAME_COMMAND}
IF NOT (JSON_CONTAINS(NEW.actor_roles_at_time, '"SUPERVISOR"')
 OR JSON_CONTAINS(NEW.actor_roles_at_time, '"ADMINISTRATOR"')
 OR (NEW.performer_id=NEW.actor_user_id AND (JSON_CONTAINS(NEW.actor_roles_at_time, '"CREW"')
  OR JSON_CONTAINS(NEW.actor_roles_at_time, '"EMT"'))))
THEN {signal("paper entry performer not permitted for the entry actor")}; END IF;
END""",
    KIT_VERSION: f"""CREATE TRIGGER cya_{KIT_VERSION}_bi BEFORE INSERT ON {KIT_VERSION}
FOR EACH ROW BEGIN
{SAME_COMMAND}
IF NOT EXISTS (SELECT 1 FROM {BATCH} b
 JOIN kit_definition_version kv ON kv.id=NEW.kit_definition_version_id AND kv.{ORG}=b.{ORG}
 JOIN kit_definition_publication kp ON kp.version_id=kv.id AND kp.{ORG}=kv.{ORG}
 JOIN checklist_kit_placement cp ON cp.checklist_version_id=b.checklist_version_id
  AND cp.definition_id=kv.definition_id
 WHERE b.id=NEW.batch_id AND kp.recorded_at<=NEW.recorded_at AND kp.effective_at<=b.performed_at)
THEN {signal("paper entry kit version not in force for the batch checklist")}; END IF;
END""",
}


def _table_statements(table):
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for column, parent, key in FKS[table]
    ]
    # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
    statements += [
        f"CREATE TRIGGER cya_{table}_{suffix} BEFORE {operation} ON {table} "
        f"FOR EACH ROW {signal('append-only table')}"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    return [*statements, GUARDS[table]]


def _table_reverse(table):
    drops = [f"DROP TRIGGER cya_{table}_bi"]
    drops += [f"DROP TRIGGER cya_{table}_{suffix}" for suffix in ("bd", "bu")]
    drops += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_{column}_org_fk`"
        for column, *_ in reversed(FKS[table])
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_roles`",
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_uuid`",
    ]
    return drops


TABLES = (BATCH, PERFORMER, KIT_VERSION)


def forward_statements():
    return [statement for table in TABLES for statement in _table_statements(table)]


def reverse_statements():
    return [statement for table in reversed(TABLES) for statement in _table_reverse(table)]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_paper", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
    ]
