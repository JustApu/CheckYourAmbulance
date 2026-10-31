"""Schema-owner guards: UUID checks, organization-pair keys, append-only and attribution triggers.

The outbox, delivery-event and read tables are append-only (contracts section 2). The insert
triggers repeat what the command layer already enforces: each row must match its command receipt
(organization and recorded instant); a delivery attempt must come from a worker receipt of type
NOTIFICATION_DELIVERY_ATTEMPT and answer an EMAIL outbox row of the same organization; a read
mark must come from the recipient's own NOTIFICATION_MARK_READ receipt. Triggers are single
statements or BEGIN..END blocks in one-item lists: Django would split a plain string at the
semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLES = ("notification_outbox", "notification_delivery_event", "notification_read")
OTHER_TABLES = ("notification_pref", "notification_policy")

FKS = [
    ("notification_outbox", "command_id", "command_receipt", "id"),
    ("notification_outbox", "recipient_user_id", "user", "id"),
    ("notification_delivery_event", "command_id", "command_receipt", "id"),
    ("notification_delivery_event", "outbox_id", "notification_outbox", "id"),
    ("notification_read", "command_id", "command_receipt", "id"),
    ("notification_read", "outbox_id", "notification_outbox", "id"),
    ("notification_read", "actor_user_id", "user", "id"),
    ("notification_pref", "user_id", "user", "id"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def receipt_match(extra=""):
    return (
        "NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id\n"
        f" AND r.{ORG}=NEW.{ORG} AND r.recorded_at=NEW.recorded_at{extra})"
    )


OUTBOX_INSERT = f"""CREATE TRIGGER cya_notification_outbox_bi BEFORE INSERT ON notification_outbox
FOR EACH ROW BEGIN
IF {receipt_match()} THEN {signal("outbox receipt attribution mismatch")}; END IF;
END"""

# A worker (no human actor, a service identity and job key) records every attempt.
DELIVERY_INSERT = f"""CREATE TRIGGER cya_notification_delivery_bi BEFORE INSERT ON notification_delivery_event
FOR EACH ROW BEGIN
IF {
    receipt_match(
        " AND r.command_type='NOTIFICATION_DELIVERY_ATTEMPT' AND r.actor_user_id IS NULL"
        " AND r.service_identity IS NOT NULL AND r.job_key IS NOT NULL"
    )
} THEN {signal("delivery receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM notification_outbox o WHERE o.id=NEW.outbox_id
 AND o.{ORG}=NEW.{ORG} AND o.channel='EMAIL' AND o.recorded_at<=NEW.recorded_at)
THEN {signal("delivery outbox mismatch")}; END IF;
END"""

READ_INSERT = f"""CREATE TRIGGER cya_notification_read_bi BEFORE INSERT ON notification_read
FOR EACH ROW BEGIN
IF {
    receipt_match(
        " AND r.command_type='NOTIFICATION_MARK_READ' AND r.actor_user_id <=> NEW.actor_user_id"
    )
} THEN {signal("read receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM notification_outbox o WHERE o.id=NEW.outbox_id
 AND o.{ORG}=NEW.{ORG} AND o.channel='IN_APP' AND o.recipient_user_id=NEW.actor_user_id
 AND o.recorded_at<=NEW.recorded_at)
THEN {signal("read outbox mismatch")}; END IF;
END"""

INSERT_TRIGGERS = (OUTBOX_INSERT, DELIVERY_INSERT, READ_INSERT)


def forward_statements():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_notif_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
        for table in (*TABLES, *OTHER_TABLES)
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_notif_{table}_{column}_org_fk` "
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
    return [*statements, *INSERT_TRIGGERS]


def reverse_statements():
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in forward_statements()
        if statement.startswith("CREATE TRIGGER ")
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_notif_{table}_{column}_org_fk`"
        for table, column, *_ in reversed(FKS)
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_notif_{table}_uuid`"
        for table in reversed((*TABLES, *OTHER_TABLES))
    ]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_notifications", "0001_initial"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_accounts", "0002_identity_guards"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
