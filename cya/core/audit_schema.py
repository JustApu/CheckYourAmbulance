"""Verify physical table classifications and append-only triggers in MariaDB."""

import re

from tools.audit_enforcement import APPEND_ONLY, MUTABLE, VERSIONED

# Framework tables are not part of the domain's A/V/M contract.
FRAMEWORK_TABLES = frozenset({"django_migrations"})


def inspect_audit_guards(cursor):
    """Fail closed on unclassified domain tables or missing A-table guards."""
    cursor.execute(
        "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE()"
    )
    objects = dict(cursor.fetchall())
    if any(kind != "BASE TABLE" for kind in objects.values()):
        raise ValueError("unclassified or unsupported non-table schema object")
    physical = set(objects)
    if not {"command_receipt", "audit_event"} <= physical:
        raise ValueError("missing receipt/audit infrastructure tables")
    declared = APPEND_ONLY | MUTABLE | VERSIONED
    unknown = physical - declared - FRAMEWORK_TABLES
    if unknown:
        raise ValueError("unclassified domain tables: " + ", ".join(sorted(unknown)))

    present_append_only = physical & APPEND_ONLY
    cursor.execute(
        "SELECT EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING, ACTION_STATEMENT "
        "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
    )
    # A no-op trigger with the right timing is not append-only enforcement.
    expected = "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'"
    installed = {
        (table, operation, timing)
        for table, operation, timing, statement in cursor.fetchall()
        if " ".join(statement.split()).rstrip(";") == expected
    }
    missing = {
        (table, operation, "BEFORE")
        for table in present_append_only
        for operation in ("UPDATE", "DELETE")
    } - installed
    if missing:
        raise ValueError("missing append-only triggers: " + repr(sorted(missing)))
    if "schedule_rule" in physical:
        cursor.execute(
            "SELECT EVENT_MANIPULATION, ACTION_TIMING, ACTION_STATEMENT FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='schedule_rule'"
        )
        frozen = {
            operation
            for operation, timing, statement in cursor.fetchall()
            if timing == "BEFORE"
            and " ".join(statement.split()).rstrip(";")
            == "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'immutable schedule revision'"
        }
        if frozen != {"UPDATE", "DELETE"}:
            raise ValueError("missing immutable schedule guards")
    return present_append_only


# Deliberately exempt tables from the standard command-receipt insert guard check.
# Each entry maps table_name to a documented reason explaining why it is exempt or has a custom guard.
# Adding to or modifying this allow-list requires review.
COMMAND_RECEIPT_GUARD_EXEMPTIONS = {
    "audit_event": (
        "audit_event has its own specialized attribution guard (cya_audit_attribution_bi) "
        "allowing nullable command_id for non-command events and independent timestamp recording"
    ),
    "notification_outbox": (
        "the causing command's type is open-ended (each later issue adds event commands) and the "
        "row has no actor; cya_notification_outbox_bi binds organization and recorded instant"
    ),
    "notification_delivery_event": (
        "written only by the notification worker, which has no user actor; "
        "cya_notification_delivery_bi binds organization, instant, command type, service "
        "identity and job key"
    ),
}


def inspect_command_receipt_guards(cursor, exemptions=COMMAND_RECEIPT_GUARD_EXEMPTIONS):
    """Fail closed on tables with command_id lacking receipt-attribution BEFORE INSERT triggers.

    Enumerates every base table with a command_id column from information_schema.
    Asserts every such table has at least one BEFORE INSERT trigger containing an active
    rejection guard block (`IF ... THEN ... SIGNAL SQLSTATE '45000' ... END IF`) whose
    condition queries `command_receipt` on `NEW.command_id`.

    For non-exempt tables, additionally asserts that the guarding condition which triggers
    the rejection signal strictly enforces the standard receipt-attribution predicates:
    - organization_id
    - actor attribution (actor_user_id or approver_id)
    - recorded_at
    - command_type
    - worker attribution (service_identity and job_key) when those columns exist on the table.

    Tables in exemptions are permitted specialized guard structures; all entries in
    exemptions must provide a non-empty explanation reason string and must still possess
    a rejecting BEFORE INSERT receipt attribution guard.
    """
    for table, reason in exemptions.items():
        if not reason or not reason.strip():
            raise ValueError(f"exemption for table '{table}' must provide an explanatory reason")

    cursor.execute(
        "SELECT c.TABLE_NAME, c.COLUMN_NAME "
        "FROM information_schema.COLUMNS c "
        "JOIN information_schema.TABLES t "
        "  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
        "WHERE c.TABLE_SCHEMA = DATABASE() AND t.TABLE_TYPE = 'BASE TABLE'"
    )
    table_columns = {}
    for table_name, column_name in cursor.fetchall():
        if table_name not in FRAMEWORK_TABLES:
            table_columns.setdefault(table_name, set()).add(column_name)

    command_tables = {
        table_name: cols for table_name, cols in table_columns.items() if "command_id" in cols
    }

    cursor.execute(
        "SELECT EVENT_OBJECT_TABLE, TRIGGER_NAME, ACTION_STATEMENT "
        "FROM information_schema.TRIGGERS "
        "WHERE TRIGGER_SCHEMA = DATABASE() AND ACTION_TIMING = 'BEFORE' AND EVENT_MANIPULATION = 'INSERT'"
    )
    triggers_by_table = {}
    for table_name, trigger_name, statement in cursor.fetchall():
        triggers_by_table.setdefault(table_name, []).append((trigger_name, statement))

    if_pattern = re.compile(
        r"\bIF\b\s+(?P<condition>.+?)\s+\bTHEN\b\s+(?P<body>.+?)\s+\bEND\s+IF\b",
        re.DOTALL | re.IGNORECASE,
    )

    missing_guards = []
    missing_predicates = {}

    for table_name, cols in sorted(command_tables.items()):
        triggers = triggers_by_table.get(table_name, [])

        guard_conditions = []
        for _, statement in triggers:
            for match in if_pattern.finditer(statement):
                condition = match.group("condition").strip()
                body = match.group("body")

                # The block must execute SIGNAL SQLSTATE '45000'
                if not re.search(r"SIGNAL\s+SQLSTATE\s+['\"]45000['\"]", body, re.IGNORECASE):
                    continue

                # The condition must evaluate command_receipt, bind NEW.command_id, and
                # check existence/absence as an active guard
                has_receipt_binding = bool(
                    re.search(r"\bcommand_receipt\b", condition, re.IGNORECASE)
                    and re.search(
                        r"(\.id\s*=\s*NEW\.command_id\b|NEW\.command_id\s*=\s*[a-zA-Z0-9_]+\.id\b)",
                        condition,
                        re.IGNORECASE,
                    )
                    and re.match(
                        r"^\s*(?:NOT\s+EXISTS\b|NEW\.command_id\s+IS\s+NOT\s+NULL\s+AND\s+EXISTS\b)",
                        condition,
                        re.IGNORECASE,
                    )
                )

                if has_receipt_binding:
                    guard_conditions.append(condition)

        if not guard_conditions:
            missing_guards.append(table_name)
            continue

        # Tables in exemptions are exempt only from the uniform predicate suite below,
        # but they must still have their valid rejecting receipt guard (verified above).
        if table_name in exemptions:
            continue

        combined_condition = " ".join(guard_conditions)
        missing = []
        if not re.search(
            r"(\borganization_id\s*=\s*NEW\.organization_id\b|NEW\.organization_id\s*=\s*[a-zA-Z0-9_]+\.organization_id\b)",
            combined_condition,
        ):
            missing.append("organization_id")
        if not re.search(
            r"(\bactor_user_id\s*(=|<=>)\s*NEW\.(actor_user_id|approver_id)\b|NEW\.(actor_user_id|approver_id)\s*(=|<=>)\s*[a-zA-Z0-9_]+\.actor_user_id\b)",
            combined_condition,
        ):
            missing.append("actor")
        if not re.search(
            r"(\brecorded_at\s*=\s*NEW\.recorded_at\b|NEW\.recorded_at\s*=\s*[a-zA-Z0-9_]+\.recorded_at\b)",
            combined_condition,
        ):
            missing.append("recorded_at")
        if not re.search(r"\bcommand_type\s*(=|IN\b)", combined_condition):
            missing.append("command_type")
        if "service_identity" in cols and not (
            re.search(
                r"service_identity\s*<=>\s*(?:BINARY\s+)?NEW\.service_identity\b|NEW\.service_identity\s+IS\s+(?:NULL|NOT NULL)\b",
                combined_condition,
                re.IGNORECASE,
            )
        ):
            missing.append("service_identity")
        if "job_key" in cols and not (
            re.search(
                r"job_key\s*<=>\s*(?:BINARY\s+)?NEW\.job_key\b|NEW\.job_key\s+IS\s+(?:NULL|NOT NULL)\b",
                combined_condition,
                re.IGNORECASE,
            )
        ):
            missing.append("job_key")

        if missing:
            missing_predicates[table_name] = missing

    if missing_guards:
        raise ValueError(f"missing command receipt insert guards: {sorted(missing_guards)}")

    if missing_predicates:
        raise ValueError(f"missing command receipt trigger predicates: {missing_predicates}")

    return set(command_tables.keys())
