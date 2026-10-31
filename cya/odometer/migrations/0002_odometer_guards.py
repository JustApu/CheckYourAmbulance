"""Schema-owner guards for the odometer tables: UUID/role checks, organization-pair keys, triggers.

All four tables are append-only (contracts section 2). The insert triggers repeat what the
command layer already enforces, so a writer that bypasses the command still cannot store a row
that: does not match its command receipt; skips or misstates the vehicle's previous reading;
stores an anomalous reading (lower than the previous, or above it by more than the plausibility
threshold, SPEC 6.4 ODO-3) without the confirmation flag, or the flag without an anomaly; or links
an odometer obligation to another vehicle's entry or to an entry recorded before its window opened.
Triggers are BEGIN..END blocks in one-item lists: Django would split a plain string at the
semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
DEFAULTS = "CASE NEW.unit WHEN 'mi' THEN 1000 ELSE 1600 END"  # cya.odometer.models defaults

# (table, uuid columns, [(column, parent, parent key)])
TABLES = {
    "odometer_entry": (
        ("id", "operation_id"),
        [
            ("command_id", "command_receipt", "id"),
            ("vehicle_id", "vehicle", "id"),
            ("actor_user_id", "user", "id"),
            ("prior_entry_id", "odometer_entry", "id"),
            ("root_entry_id", "odometer_entry", "id"),
        ],
    ),
    "engine_hours_entry": (
        ("id", "operation_id"),
        [
            ("command_id", "command_receipt", "id"),
            ("vehicle_id", "vehicle", "id"),
            ("actor_user_id", "user", "id"),
            ("prior_entry_id", "engine_hours_entry", "id"),
            ("root_entry_id", "engine_hours_entry", "id"),
        ],
    ),
    "odometer_obligation_link": (
        ("id",),
        [
            ("command_id", "command_receipt", "id"),
            ("actor_user_id", "user", "id"),
            ("obligation_id", "odometer_obligation", "id"),
            ("entry_id", "odometer_entry", "id"),
        ],
    ),
    "odometer_threshold": (
        ("id", "operation_id"),
        [
            ("command_id", "command_receipt", "id"),
            ("actor_user_id", "user", "id"),
        ],
    ),
}


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def receipt_match(command_type, operation=True):
    same_operation = " AND r.operation_id=NEW.operation_id" if operation else ""
    return (
        "NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id "
        f"AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id "
        f"AND r.recorded_at=NEW.recorded_at{same_operation} "
        f"AND r.command_type='{command_type}')"
    )


def reading_trigger(table, command_type, threshold):
    """The previous reading is the vehicle's latest; an anomaly is confirmed exactly when present."""
    increase = (
        f"""SET max_up = IFNULL((SELECT t.max_increase FROM odometer_threshold t
 WHERE t.{ORG}=NEW.{ORG} AND t.unit=NEW.unit ORDER BY t.revision DESC LIMIT 1), {DEFAULTS});
SET anomalous = anomalous OR (NEW.value - prior_value > max_up);"""
        if threshold
        else ""
    )
    return f"""CREATE TRIGGER cya_{table}_bi BEFORE INSERT ON {table}
FOR EACH ROW BEGIN
DECLARE prior_value DECIMAL(10,1);
DECLARE prior_unit VARCHAR(2);
DECLARE max_up DECIMAL(10,1);
DECLARE anomalous BOOLEAN DEFAULT 0;
IF {receipt_match(command_type)} THEN {signal("reading receipt attribution mismatch")}; END IF;
IF NOT (NEW.prior_entry_id <=> (SELECT e.id FROM {table} e WHERE e.vehicle_id=NEW.vehicle_id
 ORDER BY e.recorded_at DESC, e.id DESC LIMIT 1))
THEN {signal("previous reading is not the latest for the vehicle")}; END IF;
IF NEW.prior_entry_id IS NOT NULL THEN
 SELECT e.value, e.unit INTO prior_value, prior_unit FROM {table} e WHERE e.id=NEW.prior_entry_id;
 IF prior_unit <> NEW.unit THEN {signal("reading unit differs from the previous reading")}; END IF;
 SET anomalous = NEW.value < prior_value;
 {increase}
END IF;
IF anomalous AND NOT NEW.anomaly_confirmed
THEN {signal("anomalous reading needs confirmation")}; END IF;
IF NEW.anomaly_confirmed AND NOT anomalous
THEN {signal("confirmation without an anomaly")}; END IF;
END"""


LINK_INSERT = f"""CREATE TRIGGER cya_odometer_obligation_link_bi BEFORE INSERT ON odometer_obligation_link
FOR EACH ROW BEGIN
IF {receipt_match("ODOMETER_RECORD", operation=False)}
THEN {signal("odometer link receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM odometer_entry e JOIN odometer_obligation o
  ON o.id=NEW.obligation_id AND o.{ORG}=NEW.{ORG} AND o.vehicle_id=e.vehicle_id
 WHERE e.id=NEW.entry_id AND e.{ORG}=NEW.{ORG} AND e.command_id=NEW.command_id
 AND e.recorded_at=NEW.recorded_at AND o.early_at<=e.recorded_at)
THEN {signal("odometer link does not match its entry and obligation")}; END IF;
END"""

THRESHOLD_INSERT = f"""CREATE TRIGGER cya_odometer_threshold_bi BEFORE INSERT ON odometer_threshold
FOR EACH ROW BEGIN
IF {receipt_match("ODOMETER_THRESHOLD_SET")}
THEN {signal("threshold receipt attribution mismatch")}; END IF;
IF NEW.revision <> IFNULL((SELECT MAX(t.revision) FROM odometer_threshold t
 WHERE t.{ORG}=NEW.{ORG} AND t.unit=NEW.unit), 0) + 1
THEN {signal("threshold revision is not the next one")}; END IF;
END"""


def forward_statements():
    statements = []
    for table, (uuids, fks) in TABLES.items():
        uuid_test = " AND ".join(f"BINARY {c} REGEXP '{UUID_PATTERN}'" for c in uuids)
        statements.append(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_uuid` CHECK ({uuid_test})"
        )
        statements.append(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_roles` "
            "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
            f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))"
        )
        statements += [
            f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_{column}_org_fk` "
            f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
            for column, parent, key in fks
        ]
        # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
        statements += [
            f"CREATE TRIGGER cya_{table}_{suffix} BEFORE {operation} ON {table} "
            f"FOR EACH ROW {signal('append-only table')}"
            for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
        ]
    return [
        *statements,
        reading_trigger("odometer_entry", "ODOMETER_RECORD", threshold=True),
        reading_trigger("engine_hours_entry", "ODOMETER_RECORD", threshold=False),
        LINK_INSERT,
        THRESHOLD_INSERT,
    ]


def reverse_statements():
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in forward_statements()
        if statement.startswith("CREATE TRIGGER ")
    ]
    for table, (_uuids, fks) in reversed(TABLES.items()):
        drops += [
            f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_{column}_org_fk`"
            for column, *_ in reversed(fks)
        ]
        drops += [
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_roles`",
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_uuid`",
        ]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_odometer", "0001_initial"),
        ("cya_obligations", "0003_kit_instance_anchor"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_accounts", "0002_identity_guards"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
