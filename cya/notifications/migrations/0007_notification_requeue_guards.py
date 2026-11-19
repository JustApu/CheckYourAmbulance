"""Schema-owner guards for notification_requeue (SPEC 13 NOT-4; issue #224).

The table is append-only. Its insert trigger repeats what cya.notifications.commands.requeue_email
enforces, so a row written any other way is refused:
- the row matches its receipt: organization, the administrator who sent it, the recorded instant
  and command type NOTIFICATION_REQUEUE;
- it reopens an EMAIL outbox row of the same organization at that row's latest attempt, and that
  attempt gave up (the unique key allows one reopening per given-up attempt).
Each statement is its own list entry, so Django runs it whole.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLE = "notification_requeue"
FKS = [
    ("command_id", "command_receipt", "id"),
    ("outbox_id", "notification_outbox", "id"),
    ("actor_user_id", "user", "id"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


GUARD = f"""CREATE TRIGGER cya_notification_requeue_bi BEFORE INSERT ON {TABLE}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='NOTIFICATION_REQUEUE')
THEN {signal("requeue receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM notification_outbox o WHERE o.id=NEW.outbox_id
 AND o.{ORG}=NEW.{ORG} AND o.channel='EMAIL' AND o.recorded_at<=NEW.recorded_at)
THEN {signal("requeue outbox mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM notification_delivery_event d WHERE d.outbox_id=NEW.outbox_id
 AND d.{ORG}=NEW.{ORG} AND d.attempt_number=NEW.after_attempt AND d.outcome='GAVE_UP')
 OR EXISTS (SELECT 1 FROM notification_delivery_event d WHERE d.outbox_id=NEW.outbox_id
 AND d.attempt_number>NEW.after_attempt)
THEN {signal("requeue of an email that did not give up")}; END IF;
END"""


def forward_statements():
    return [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_notif_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        *(
            f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_notif_{TABLE}_{column}_org_fk` "
            f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
            for column, parent, key in FKS
        ),
        # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
        *(
            f"CREATE TRIGGER cya_{TABLE}_{suffix} BEFORE {operation} ON {TABLE} "
            f"FOR EACH ROW {signal('append-only table')}"
            for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
        ),
        GUARD,
    ]


def reverse_statements():
    return [
        "DROP TRIGGER cya_notification_requeue_bi",
        f"DROP TRIGGER cya_{TABLE}_bd",
        f"DROP TRIGGER cya_{TABLE}_bu",
        *(
            f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_notif_{TABLE}_{column}_org_fk`"
            for column, _parent, _key in reversed(FKS)
        ),
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_notif_{TABLE}_uuid`",
    ]


class Migration(migrations.Migration):
    dependencies = [("cya_notifications", "0006_notification_requeue")]

    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
