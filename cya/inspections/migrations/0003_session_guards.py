"""Tenant, attribution and append-only guards for session evidence (MariaDB 10.11)."""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

TABLES = ("inspection_session", "session_participant", "session_event", "session_projection")
APPEND_ONLY = ("session_participant", "session_event")
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
FKS = (
    ("inspection_session", "command_id", "command_receipt"),
    ("inspection_session", "actor_user_id", "user"),
    ("inspection_session", "vehicle_id", "vehicle"),
    ("inspection_session", "checklist_version_id", "checklist_version"),
    ("session_participant", "command_id", "command_receipt"),
    ("session_participant", "actor_user_id", "user"),
    ("session_participant", "session_id", "inspection_session"),
    ("session_participant", "user_id", "user"),
    ("session_event", "command_id", "command_receipt"),
    ("session_event", "actor_user_id", "user"),
    ("session_event", "session_id", "inspection_session"),
    ("session_event", "prior_event_id", "session_event"),
    ("session_event", "persisted_activity_command_id", "command_receipt"),
    ("session_projection", "session_id", "inspection_session"),
    ("session_projection", "last_activity_command_id", "command_receipt"),
    ("session_projection", "last_applied_event_id", "session_event"),
)


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


RECEIPT_MATCH = """NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type={command_type})"""

OPEN_RECEIPT_MATCH = RECEIPT_MATCH.format(command_type="'SESSION_OPEN_OR_JOIN'")
EVENT_RECEIPT_MATCH = RECEIPT_MATCH.format(
    command_type="CASE NEW.event_type "
    "WHEN 'OPEN' THEN 'SESSION_OPEN_OR_JOIN' "
    "WHEN 'JOIN' THEN 'SESSION_OPEN_OR_JOIN' "
    "WHEN 'REOPEN' THEN 'SESSION_REOPEN' "
    "WHEN 'END_CLOSED' THEN 'SESSION_END' "
    "WHEN 'END_INCOMPLETE' THEN 'SESSION_END' END"
)

SESSION_INSERT = f"""CREATE TRIGGER cya_inspection_session_bi BEFORE INSERT ON inspection_session
FOR EACH ROW BEGIN
IF {OPEN_RECEIPT_MATCH} THEN {signal("session receipt attribution mismatch")}; END IF;
IF NEW.opened_at > NEW.recorded_at THEN {signal("session opened after recording")}; END IF;
IF NOT EXISTS (SELECT 1 FROM checklist_publication p
 WHERE p.organization_id=NEW.organization_id AND p.checklist_version_id=NEW.checklist_version_id
 AND p.effective_at<=NEW.opened_at AND p.recorded_at<=NEW.opened_at)
THEN {signal("session version is not published at open")}; END IF;
END"""

PARTICIPANT_INSERT = f"""CREATE TRIGGER cya_session_participant_bi BEFORE INSERT ON session_participant
FOR EACH ROW BEGIN
IF {OPEN_RECEIPT_MATCH} OR NEW.user_id<>NEW.actor_user_id
THEN {signal("participant attribution mismatch")}; END IF;
END"""

EVENT_INSERT = f"""CREATE TRIGGER cya_session_event_bi BEFORE INSERT ON session_event
FOR EACH ROW BEGIN
IF {EVENT_RECEIPT_MATCH} THEN {signal("session event receipt mismatch")}; END IF;
IF NEW.effective_at > NEW.recorded_at THEN {signal("session event is future dated")}; END IF;
IF NEW.persisted_activity_command_id IS NOT NULL
 AND NEW.persisted_activity_command_id<>NEW.command_id
THEN {signal("session activity receipt mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM session_participant p
 WHERE p.organization_id=NEW.organization_id AND p.session_id=NEW.session_id
 AND p.user_id=NEW.actor_user_id)
THEN {signal("session actor is not a participant")}; END IF;
IF NEW.sequence=1 THEN
 IF NEW.event_type<>'OPEN' OR NEW.prior_event_id IS NOT NULL OR NOT EXISTS (
  SELECT 1 FROM inspection_session s WHERE s.id=NEW.session_id
  AND s.organization_id=NEW.organization_id AND s.command_id=NEW.command_id
  AND s.opened_at=NEW.effective_at)
 THEN {signal("first session event must open its header")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM session_event p WHERE p.id=NEW.prior_event_id
  AND p.organization_id=NEW.organization_id AND p.session_id=NEW.session_id
  AND p.sequence=NEW.sequence-1 AND p.effective_at<=NEW.effective_at
  AND p.recorded_at<=NEW.recorded_at
  AND ((NEW.event_type IN ('JOIN','END_CLOSED','END_INCOMPLETE')
    AND p.event_type IN ('OPEN','JOIN','REOPEN'))
   OR (NEW.event_type='REOPEN' AND p.event_type IN ('END_CLOSED','END_INCOMPLETE'))))
 THEN {signal("session event transition mismatch")}; END IF;
END IF;
END"""

HEADER_COLUMNS = (
    "id",
    "organization_id",
    "command_id",
    "recorded_at",
    "actor_user_id",
    "actor_roles_at_time",
    "vehicle_id",
    "checklist_version_id",
    "opened_at",
)
HEADER_CHANGE = " OR ".join(
    f"NOT (BINARY OLD.{column} <=> BINARY NEW.{column})" for column in HEADER_COLUMNS
)
HEADER_GUARD = (
    "CREATE TRIGGER cya_inspection_session_immutable_bu BEFORE UPDATE ON inspection_session "
    f"FOR EACH ROW BEGIN IF {HEADER_CHANGE} THEN {signal('immutable session header')}; "
    "END IF; END"
)
PROJECTION_GUARD = (
    "CREATE TRIGGER cya_session_projection_immutable_bu BEFORE UPDATE ON session_projection "
    "FOR EACH ROW BEGIN IF NOT (BINARY OLD.id <=> BINARY NEW.id) OR "
    "NOT (BINARY OLD.organization_id <=> BINARY NEW.organization_id) OR "
    "NOT (BINARY OLD.session_id <=> BINARY NEW.session_id) "
    f"THEN {signal('immutable projection anchor')}; END IF; END"
)


def forward_statements():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_sess_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
        for table in TABLES
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_sess_{table}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))"
        for table in ("inspection_session", *APPEND_ONLY)
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_sess_org_fk_{index:02d}` "
        f"FOREIGN KEY (organization_id,{column}) REFERENCES `{parent}` (organization_id,id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for index, (table, column, parent) in enumerate(FKS, start=1)
    ]
    statements += [
        "ALTER TABLE `session_event` ADD CONSTRAINT `cya_sess_prior_anchor_fk` "
        "FOREIGN KEY (prior_event_id,session_id) REFERENCES `session_event` (id,session_id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        "ALTER TABLE `session_projection` ADD CONSTRAINT `cya_sess_projection_anchor_fk` "
        "FOREIGN KEY (last_applied_event_id,session_id) "
        "REFERENCES `session_event` (id,session_id) ON DELETE RESTRICT ON UPDATE RESTRICT",
    ]
    statements += [
        f"CREATE TRIGGER cya_{table}_{suffix} BEFORE {operation} ON {table} "
        f"FOR EACH ROW {signal('append-only table')}"
        for table in APPEND_ONLY
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    return [
        *statements,
        SESSION_INSERT,
        PARTICIPANT_INSERT,
        EVENT_INSERT,
        HEADER_GUARD,
        PROJECTION_GUARD,
    ]


def reverse_statements():
    statements = forward_statements()
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in statements
        if statement.startswith("CREATE TRIGGER ")
    ]
    drops += [
        "ALTER TABLE `session_projection` DROP FOREIGN KEY `cya_sess_projection_anchor_fk`",
        "ALTER TABLE `session_event` DROP FOREIGN KEY `cya_sess_prior_anchor_fk`",
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_sess_org_fk_{index:02d}`"
        for index, (table, _, _) in reversed(tuple(enumerate(FKS, start=1)))
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_sess_{table}_roles`"
        for table in reversed(("inspection_session", *APPEND_ONLY))
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_sess_{table}_uuid`"
        for table in reversed(TABLES)
    ]
    return drops


class Migration(migrations.Migration):
    dependencies = [("cya_inspections", "0002_sessionevent_session_event_anchor")]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
