"""Schema-owner guards: UUID/role checks, organization-pair keys, append-only and chain triggers.

Both tables are append-only (contracts section 2). The insert triggers repeat what the
command layer already enforces: the row must match its command receipt (organization, actor,
recorded instant), status events form one linear per-vehicle chain, and a decision must answer
a REQUEST for the same vehicle and status. Triggers are single statements or BEGIN..END blocks
in one-item lists: Django would split a plain string at the semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLES = ("vehicle_status_event", "status_request")
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'

FKS = [
    ("vehicle_status_event", "command_id", "command_receipt", "id"),
    ("vehicle_status_event", "vehicle_id", "vehicle", "id"),
    ("vehicle_status_event", "actor_user_id", "user", "id"),
    ("vehicle_status_event", "prior_event_id", "vehicle_status_event", "id"),
    ("status_request", "command_id", "command_receipt", "id"),
    ("status_request", "vehicle_id", "vehicle", "id"),
    ("status_request", "actor_user_id", "user", "id"),
    ("status_request", "request_id", "status_request", "id"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


RECEIPT_MATCH = """NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at)"""

# One linear chain per vehicle: the prior event is the immediately preceding sequence number
# on the same vehicle and not later than this one (unique prior_event_id forbids forks).
EVENT_INSERT = f"""CREATE TRIGGER cya_vehicle_status_event_bi BEFORE INSERT ON vehicle_status_event
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH} THEN {signal("status receipt attribution mismatch")}; END IF;
IF NEW.prior_event_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM vehicle_status_event e WHERE e.id=NEW.prior_event_id AND e.{ORG}=NEW.{ORG}
 AND e.vehicle_id=NEW.vehicle_id AND e.sequence=NEW.sequence-1 AND e.recorded_at<=NEW.recorded_at)
THEN {signal("status chain mismatch")}; END IF;
END"""

# A decision answers a REQUEST on the same vehicle for the same status; an approval must come
# with the status event it produced in the same command.
REQUEST_INSERT = f"""CREATE TRIGGER cya_status_request_bi BEFORE INSERT ON status_request
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH} THEN {signal("request receipt attribution mismatch")}; END IF;
IF NEW.request_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM status_request q WHERE q.id=NEW.request_id AND q.{ORG}=NEW.{ORG}
 AND q.vehicle_id=NEW.vehicle_id AND q.action='REQUEST'
 AND q.requested_status=NEW.requested_status AND q.recorded_at<=NEW.recorded_at)
THEN {signal("request decision mismatch")}; END IF;
IF NEW.action='APPROVE' AND NOT EXISTS (
 SELECT 1 FROM vehicle_status_event e WHERE e.command_id=NEW.command_id
 AND e.vehicle_id=NEW.vehicle_id AND e.status=NEW.requested_status)
THEN {signal("approval without matching status event")}; END IF;
END"""


def forward_statements():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_ops_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
        for table in TABLES
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_ops_{table}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))"
        for table in TABLES
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_ops_{table}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for table, column, parent, key in FKS
    ]
    # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
    statements += [
        f"CREATE TRIGGER cya_{table}_{suffix} BEFORE {operation} ON {table} "
        f"FOR EACH ROW {signal('append-only table')}"
        for table in TABLES
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    return [*statements, EVENT_INSERT, REQUEST_INSERT]


def reverse_statements():
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in forward_statements()
        if statement.startswith("CREATE TRIGGER ")
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_ops_{table}_{column}_org_fk`"
        for table, column, *_ in reversed(FKS)
    ]
    for table in reversed(TABLES):
        drops += [
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_ops_{table}_roles`",
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_ops_{table}_uuid`",
        ]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_operations", "0001_initial"),
        ("cya_checklists", "0002_publication_guards"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_accounts", "0002_identity_guards"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
