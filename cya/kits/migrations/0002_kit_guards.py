"""Schema-owner guards for kit_assignment_event: UUID/role checks, organization-pair keys, triggers.

The table is append-only (SPEC 18, OQ-54). The insert trigger repeats what the command layer
already enforces, so a writer that bypasses the command still cannot store a row that: does not
match its command receipt (organization, actor, recorded instant and a kit command type); is not
the next event of its kit (consecutive sequence, strictly later effective instant); or places a
kit in a compartment that is not the named vehicle's. Triggers are BEGIN..END blocks in one-item
lists: Django would split a plain string at the semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLE = "kit_assignment_event"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
COMMAND_TYPES = "('KIT_ADD','KIT_ASSIGNMENT_RECORD')"

FKS = [
    ("command_id", "command_receipt", "id"),
    ("actor_user_id", "user", "id"),
    ("kit_id", "kit_instance", "id"),
    ("vehicle_id", "vehicle", "id"),
    ("compartment_id", "compartment", "id"),
    ("prior_event_id", "kit_assignment_event", "id"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


RECEIPT_MATCH = f"""NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type IN {COMMAND_TYPES})"""

EVENT_INSERT = f"""CREATE TRIGGER cya_kit_assignment_event_bi BEFORE INSERT ON kit_assignment_event
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH} THEN {signal("kit assignment receipt attribution mismatch")}; END IF;
IF NEW.prior_event_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM kit_assignment_event p
 WHERE p.id=NEW.prior_event_id AND p.{ORG}=NEW.{ORG} AND p.kit_id=NEW.kit_id
 AND p.sequence=NEW.sequence-1 AND p.effective_at < NEW.effective_at)
THEN {signal("kit assignment chain mismatch")}; END IF;
IF NEW.compartment_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM compartment c
 WHERE c.id=NEW.compartment_id AND c.{ORG}=NEW.{ORG} AND c.vehicle_id=NEW.vehicle_id)
THEN {signal("kit compartment is not on the vehicle")}; END IF;
END"""


def forward_statements():
    statements = [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
    ]
    statements += [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for column, parent, key in FKS
    ]
    # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
    statements += [
        f"CREATE TRIGGER cya_{TABLE}_{suffix} BEFORE {operation} ON {TABLE} "
        f"FOR EACH ROW {signal('append-only table')}"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    return [*statements, EVENT_INSERT]


def reverse_statements():
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in forward_statements()
        if statement.startswith("CREATE TRIGGER ")
    ]
    drops += [
        f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_{column}_org_fk`"
        for column, *_ in reversed(FKS)
    ]
    drops += [
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_roles`",
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`",
    ]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_kits", "0001_initial"),
        ("cya_checklists", "0002_publication_guards"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_accounts", "0002_identity_guards"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
