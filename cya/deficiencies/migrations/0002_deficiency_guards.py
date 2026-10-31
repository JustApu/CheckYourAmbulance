"""Tenant, attribution, freeze and append-only guards for deficiencies (MariaDB 10.11).

The insert triggers repeat what the command layer already enforces, so a writer that bypasses it
still cannot store a row that: does not match its command receipt (organization, actor,
recorded instant, command type and, for the header, operation id); names a category or
severity revision that is not the one in force (not yet effective, superseded, or a retired level); puts a kit on another vehicle; claims evidence from a check that is
not a Failed or Unable check of that deficiency's vehicle recorded by the same command; or forks
or skips an event chain or a link history. Triggers are BEGIN..END blocks in one-item lists:
Django would split a plain string at the semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import immutable_header_guard

ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
APPEND_ONLY = (
    "deficiency_affected_item",
    "deficiency_event",
    "deficiency_evidence",
    "deficiency_link",
)
EVIDENCE_TABLES = ("deficiency", *APPEND_ONLY)
CREATION_COMMANDS = "'CHECK_RECORD','DEFICIENCY_REPORT'"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def receipt_mismatch(command_types, *, operation=False):
    """The shared guard condition; the audit schema inspector requires this exact shape."""
    return (
        "NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id "
        "AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id "
        "AND r.recorded_at=NEW.recorded_at "
        + ("AND r.operation_id=NEW.operation_id " if operation else "")
        + f"AND r.command_type IN ({command_types}))"
    )


DEFICIENCY_INSERT = f"""CREATE TRIGGER cya_deficiency_bi BEFORE INSERT ON deficiency
FOR EACH ROW BEGIN
IF {receipt_mismatch(CREATION_COMMANDS, operation=True)}
THEN {signal("deficiency receipt attribution mismatch")}; END IF;
IF NEW.reporter_id<>NEW.actor_user_id OR NEW.reported_at<>NEW.recorded_at
THEN {signal("deficiency reporter mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM deficiency_category c WHERE c.id=NEW.category_version_id
 AND c.organization_id=NEW.organization_id AND c.effective_at<=NEW.recorded_at
 AND NOT EXISTS (SELECT 1 FROM deficiency_category n WHERE n.organization_id=c.organization_id
  AND n.stable_category_id=c.stable_category_id AND n.revision>c.revision
  AND n.effective_at<=NEW.recorded_at))
THEN {signal("deficiency category is not the revision in force")}; END IF;
IF NOT EXISTS (SELECT 1 FROM deficiency_severity v WHERE v.id=NEW.severity_version_id
 AND v.organization_id=NEW.organization_id AND v.active AND v.effective_at<=NEW.recorded_at
 AND NOT EXISTS (SELECT 1 FROM deficiency_severity n WHERE n.organization_id=v.organization_id
  AND n.code=v.code AND n.revision>v.revision AND n.effective_at<=NEW.recorded_at))
THEN {signal("deficiency severity is not an active level in force")}; END IF;
IF NEW.kit_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM kit_instance k WHERE k.id=NEW.kit_id
 AND k.organization_id=NEW.organization_id AND k.vehicle_id=NEW.vehicle_id)
THEN {signal("deficiency kit is not a kit of the vehicle")}; END IF;
END"""

AFFECTED_INSERT = f"""CREATE TRIGGER cya_deficiency_affected_item_bi BEFORE INSERT ON deficiency_affected_item
FOR EACH ROW BEGIN
IF {receipt_mismatch(CREATION_COMMANDS)}
THEN {signal("affected item receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM deficiency d WHERE d.id=NEW.deficiency_id
 AND d.organization_id=NEW.organization_id AND d.command_id=NEW.command_id)
THEN {signal("affected scope is fixed by the creating command")}; END IF;
END"""

EVENT_INSERT = f"""CREATE TRIGGER cya_deficiency_event_bi BEFORE INSERT ON deficiency_event
FOR EACH ROW BEGIN
IF {receipt_mismatch(CREATION_COMMANDS)}
THEN {signal("deficiency event receipt attribution mismatch")}; END IF;
IF NEW.sequence=1 THEN
 IF NEW.event_type<>'REPORTED' OR NOT EXISTS (SELECT 1 FROM deficiency d
  WHERE d.id=NEW.deficiency_id AND d.organization_id=NEW.organization_id
  AND d.command_id=NEW.command_id)
 THEN {signal("first deficiency event must report its header")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM deficiency_event p WHERE p.id=NEW.predecessor_event_id
  AND p.organization_id=NEW.organization_id AND p.deficiency_id=NEW.deficiency_id
  AND p.sequence=NEW.sequence-1 AND p.recorded_at<=NEW.recorded_at
  AND p.to_status<=>NEW.from_status)
 THEN {signal("deficiency event transition mismatch")}; END IF;
END IF;
END"""

EVIDENCE_INSERT = f"""CREATE TRIGGER cya_deficiency_evidence_bi BEFORE INSERT ON deficiency_evidence
FOR EACH ROW BEGIN
IF {receipt_mismatch(CREATION_COMMANDS)}
THEN {signal("deficiency evidence receipt attribution mismatch")}; END IF;
IF NEW.check_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM check_record c
 JOIN deficiency d ON d.id=NEW.deficiency_id AND d.organization_id=NEW.organization_id
 WHERE c.id=NEW.check_id AND c.organization_id=NEW.organization_id
 AND c.command_id=NEW.command_id AND c.vehicle_id=d.vehicle_id
 AND c.outcome IN ('FAILED','UNABLE_TO_INSPECT'))
THEN {signal("evidence check does not support the deficiency")}; END IF;
END"""

LINK_INSERT = f"""CREATE TRIGGER cya_deficiency_link_bi BEFORE INSERT ON deficiency_link
FOR EACH ROW BEGIN
IF {receipt_mismatch("'DEFICIENCY_REPORT','DEFICIENCY_LINK_ADD','DEFICIENCY_LINK_REMOVE'")}
THEN {signal("deficiency link receipt attribution mismatch")}; END IF;
IF NEW.link_type='ADD' AND NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.command_type IN ('DEFICIENCY_REPORT','DEFICIENCY_LINK_ADD'))
THEN {signal("link added by a removal command")}; END IF;
IF NEW.link_type='REMOVE' AND NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.command_type='DEFICIENCY_LINK_REMOVE')
THEN {signal("link removed by an adding command")}; END IF;
IF NEW.prior_link_event_id IS NULL THEN
 IF NEW.link_type<>'ADD' OR EXISTS (SELECT 1 FROM deficiency_link x
  WHERE x.organization_id=NEW.organization_id AND x.lower_id=NEW.lower_id
  AND x.upper_id=NEW.upper_id)
 THEN {signal("first link event must add an unlinked pair")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM deficiency_link p WHERE p.id=NEW.prior_link_event_id
  AND p.organization_id=NEW.organization_id AND p.lower_id=NEW.lower_id
  AND p.upper_id=NEW.upper_id AND p.link_type<>NEW.link_type)
 THEN {signal("link event must alternate on its own pair")}; END IF;
END IF;
END"""

INSERT_TRIGGERS = (
    DEFICIENCY_INSERT,
    AFFECTED_INSERT,
    EVENT_INSERT,
    EVIDENCE_INSERT,
    LINK_INSERT,
)

# (table, column, parent): each is an organization-pair foreign key (organization_id, column).
FKS = (
    ("deficiency", "command_id", "command_receipt"),
    ("deficiency", "actor_user_id", "user"),
    ("deficiency", "reporter_id", "user"),
    ("deficiency", "category_version_id", "deficiency_category"),
    ("deficiency", "severity_version_id", "deficiency_severity"),
    ("deficiency", "vehicle_id", "vehicle"),
    ("deficiency", "item_id", "item"),
    ("deficiency", "kit_id", "kit_instance"),
    ("deficiency", "compartment_id", "compartment"),
    ("deficiency_lock", "deficiency_id", "deficiency"),
    ("deficiency_affected_item", "command_id", "command_receipt"),
    ("deficiency_affected_item", "actor_user_id", "user"),
    ("deficiency_affected_item", "deficiency_id", "deficiency"),
    ("deficiency_affected_item", "item_id", "item"),
    ("deficiency_affected_item", "definition_id", "checklist_item_def"),
    ("deficiency_event", "command_id", "command_receipt"),
    ("deficiency_event", "actor_user_id", "user"),
    ("deficiency_event", "deficiency_id", "deficiency"),
    ("deficiency_event", "assignee_id", "user"),
    ("deficiency_evidence", "command_id", "command_receipt"),
    ("deficiency_evidence", "actor_user_id", "user"),
    ("deficiency_evidence", "deficiency_id", "deficiency"),
    ("deficiency_evidence", "check_id", "check_record"),
    ("deficiency_link", "command_id", "command_receipt"),
    ("deficiency_link", "actor_user_id", "user"),
    ("deficiency_link", "lower_id", "deficiency"),
    ("deficiency_link", "upper_id", "deficiency"),
    ("deficiency_link", "prior_link_event_id", "deficiency_link"),
    ("deficiency_projection", "deficiency_id", "deficiency"),
    ("deficiency_projection", "assignee_id", "user"),
    ("deficiency_projection", "last_applied_event_id", "deficiency_event"),
)
UUID_TABLES = (
    *EVIDENCE_TABLES,
    "deficiency_category",
    "deficiency_severity",
    "deficiency_lock",
    "deficiency_projection",
)

HEADER_COLUMNS = (
    "id",
    "organization_id",
    "command_id",
    "recorded_at",
    "actor_user_id",
    "actor_roles_at_time",
    "operation_id",
    "reported_at",
    "reporter_id",
    "category_version_id",
    "severity_version_id",
    "summary",
    "description",
    "vehicle_id",
    "item_id",
    "kit_id",
    "compartment_id",
)
HEADER_GUARD = (
    "CREATE TRIGGER cya_deficiency_immutable_bu BEFORE UPDATE ON deficiency FOR EACH ROW BEGIN IF "
    + " OR ".join(f"NOT (BINARY OLD.{c} <=> BINARY NEW.{c})" for c in HEADER_COLUMNS)
    + f" THEN {signal('immutable deficiency header')}; END IF; END"
)
PROJECTION_GUARD = (
    "CREATE TRIGGER cya_deficiency_projection_immutable_bu BEFORE UPDATE ON deficiency_projection "
    "FOR EACH ROW BEGIN IF NOT (BINARY OLD.id <=> BINARY NEW.id) OR "
    "NOT (BINARY OLD.organization_id <=> BINARY NEW.organization_id) OR "
    "NOT (BINARY OLD.deficiency_id <=> BINARY NEW.deficiency_id) "
    f"THEN {signal('immutable projection anchor')}; END IF; END"
)


def forward_statements():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_def_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
        for table in UUID_TABLES
    ]
    statements.append(
        "ALTER TABLE `deficiency` ADD CONSTRAINT `cya_def_operation_uuid` "
        f"CHECK (BINARY operation_id REGEXP '{UUID_PATTERN}')"
    )
    statements.append(
        "ALTER TABLE `deficiency_category` ADD CONSTRAINT `cya_def_category_stable_uuid` "
        f"CHECK (BINARY stable_category_id REGEXP '{UUID_PATTERN}')"
    )
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_def_{table}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))"
        for table in EVIDENCE_TABLES
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_def_fk_{index:02d}` "
        f"FOREIGN KEY (organization_id,{column}) REFERENCES `{parent}` (organization_id,id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for index, (table, column, parent) in enumerate(FKS, start=1)
    ]
    statements += [
        # The compartment must be one of the deficiency's own vehicle (compartment unique key).
        "ALTER TABLE `deficiency` ADD CONSTRAINT `cya_def_compartment_vehicle_fk` "
        "FOREIGN KEY (vehicle_id,compartment_id) REFERENCES `compartment` (vehicle_id,id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        # The scope definition must be the one for that very item.
        "ALTER TABLE `deficiency_affected_item` ADD CONSTRAINT `cya_def_affected_pair_fk` "
        "FOREIGN KEY (definition_id,item_id) REFERENCES `checklist_item_def` (id,item_id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        "ALTER TABLE `deficiency_event` ADD CONSTRAINT `cya_def_predecessor_anchor_fk` "
        "FOREIGN KEY (predecessor_event_id,deficiency_id) REFERENCES `deficiency_event` "
        "(id,deficiency_id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "ALTER TABLE `deficiency_projection` ADD CONSTRAINT `cya_def_projection_anchor_fk` "
        "FOREIGN KEY (last_applied_event_id,deficiency_id) REFERENCES `deficiency_event` "
        "(id,deficiency_id) ON DELETE RESTRICT ON UPDATE RESTRICT",
    ]
    # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
    statements += [
        f"CREATE TRIGGER cya_{table}_{suffix} BEFORE {operation} ON {table} "
        f"FOR EACH ROW {signal('append-only table')}"
        for table in APPEND_ONLY
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    statements += [
        f"CREATE TRIGGER cya_deficiency_category_{suffix} BEFORE {operation} ON deficiency_category "
        f"FOR EACH ROW {signal('immutable category revision')}"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    statements += [
        f"CREATE TRIGGER cya_deficiency_severity_{suffix} BEFORE {operation} ON deficiency_severity "
        f"FOR EACH ROW {signal('immutable severity revision')}"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    statements += [HEADER_GUARD, immutable_header_guard("deficiency_lock"), PROJECTION_GUARD]
    return [*statements, *INSERT_TRIGGERS]


def reverse_statements():
    """Drop what forward_statements added; a key must go before the unique key it points at."""
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in forward_statements()
        if statement.startswith("CREATE TRIGGER ")
    ]
    drops += [
        "ALTER TABLE `deficiency_projection` DROP FOREIGN KEY `cya_def_projection_anchor_fk`",
        "ALTER TABLE `deficiency_event` DROP FOREIGN KEY `cya_def_predecessor_anchor_fk`",
        "ALTER TABLE `deficiency_affected_item` DROP FOREIGN KEY `cya_def_affected_pair_fk`",
        "ALTER TABLE `deficiency` DROP FOREIGN KEY `cya_def_compartment_vehicle_fk`",
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_def_fk_{index:02d}`"
        for index, (table, _column, _parent) in reversed(list(enumerate(FKS, start=1)))
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_def_{table}_roles`"
        for table in EVIDENCE_TABLES
    ]
    drops += [
        "ALTER TABLE `deficiency_category` DROP CONSTRAINT `cya_def_category_stable_uuid`",
        "ALTER TABLE `deficiency` DROP CONSTRAINT `cya_def_operation_uuid`",
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_def_{table}_uuid`" for table in UUID_TABLES
    ]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_deficiencies", "0001_initial"),
        ("cya_core", "0006_audit_action_command_type_guard"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
