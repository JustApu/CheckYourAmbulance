"""Schema-owner guards for vehicle crew assignment (SPEC 15.4 ASG-1 to ASG-8, issue #202).

Both tables are append-only. The vehicle_assignment_event insert trigger repeats what the
commands in cya.operations.assignments enforce, so a row written any other way is refused:
- the row matches its receipt (organization, actor, recorded instant), and its action matches
  the command type: ASSIGN and MOVE from VEHICLE_ASSIGN, EXTEND from VEHICLE_ASSIGNMENT_EXTEND,
  an END by a person from VEHICLE_ASSIGNMENT_END, and an END because the vehicle was
  deactivated from VEHICLE_ACTIVE_SET (ASG-3);
- source SELF exactly when the actor is the person;
- events form one linear chain per person;
- ASSIGN only when the person has no current assignment; MOVE, EXTEND and END only on a
  current one (not ended, end time still ahead); MOVE leaves the vehicle the person was on;
  EXTEND and END stay on it; EXTEND only to a later end time (ASG-3 to ASG-5);
- ASSIGN, MOVE and EXTEND need an active vehicle; a deactivation END a deactivated one.
The maximum length (ASG-4, OQ-79) is a setting with revisions, so the command checks it.
Each statement is its own list entry, so Django runs it whole (as 0005 does).
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
EVENTS = "vehicle_assignment_event"
LIMITS = "vehicle_assignment_limit"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
FKS = {
    EVENTS: [
        ("command_id", "command_receipt", "id"),
        ("person_id", "user", "id"),
        ("vehicle_id", "vehicle", "id"),
        ("from_vehicle_id", "vehicle", "id"),
        ("actor_user_id", "user", "id"),
        ("prior_event_id", EVENTS, "id"),
    ],
    LIMITS: [
        ("command_id", "command_receipt", "id"),
        ("actor_user_id", "user", "id"),
    ],
}


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


EVENT_GUARD = f"""CREATE TRIGGER cya_vehicle_assignment_event_bi BEFORE INSERT ON {EVENTS}
FOR EACH ROW BEGIN
DECLARE p_action VARCHAR(10);
DECLARE p_vehicle CHAR(36) CHARACTER SET ascii COLLATE ascii_bin;
DECLARE p_ends DATETIME(6);
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at
 AND ((NEW.action IN ('ASSIGN','MOVE') AND r.command_type='VEHICLE_ASSIGN')
  OR (NEW.action='EXTEND' AND r.command_type='VEHICLE_ASSIGNMENT_EXTEND')
  OR (NEW.action='END' AND NEW.end_reason='ENDED' AND r.command_type='VEHICLE_ASSIGNMENT_END')
  OR (NEW.action='END' AND NEW.end_reason='VEHICLE_INACTIVE'
   AND r.command_type='VEHICLE_ACTIVE_SET')))
THEN {signal("assignment receipt attribution mismatch")}; END IF;
IF (NEW.source='SELF') <> (NEW.actor_user_id=NEW.person_id)
THEN {signal("assignment source mismatch")}; END IF;
IF NEW.prior_event_id IS NULL THEN
 IF EXISTS (SELECT 1 FROM {EVENTS} e WHERE e.person_id=NEW.person_id)
 THEN {signal("assignment chain mismatch")}; END IF;
ELSE
 SELECT e.action, e.vehicle_id, e.ends_at INTO p_action, p_vehicle, p_ends FROM {EVENTS} e
  WHERE e.id=NEW.prior_event_id AND e.{ORG}=NEW.{ORG} AND e.person_id=NEW.person_id
  AND e.sequence=NEW.sequence-1 AND e.recorded_at<=NEW.recorded_at;
 IF p_action IS NULL THEN {signal("assignment chain mismatch")}; END IF;
END IF;
IF NEW.action='ASSIGN' AND p_action IS NOT NULL AND p_action<>'END'
 AND p_ends>NEW.recorded_at
THEN {signal("person already has a current assignment")}; END IF;
IF NEW.action<>'ASSIGN' AND (p_action IS NULL OR p_action='END' OR p_ends<=NEW.recorded_at)
THEN {signal("person has no current assignment")}; END IF;
IF NEW.action='MOVE' AND NOT (NEW.from_vehicle_id <=> p_vehicle)
THEN {signal("move does not leave the current vehicle")}; END IF;
IF NEW.action IN ('EXTEND','END') AND NOT (NEW.vehicle_id <=> p_vehicle)
THEN {signal("assignment vehicle mismatch")}; END IF;
IF NEW.action='EXTEND' AND NEW.ends_at<=p_ends
THEN {signal("extension does not end later")}; END IF;
IF NEW.action<>'END' AND NOT EXISTS (SELECT 1 FROM vehicle v WHERE v.id=NEW.vehicle_id
 AND v.{ORG}=NEW.{ORG} AND v.active)
THEN {signal("assignment to an inactive vehicle")}; END IF;
IF NEW.end_reason='VEHICLE_INACTIVE' AND NOT EXISTS (SELECT 1 FROM vehicle v
 WHERE v.id=NEW.vehicle_id AND v.{ORG}=NEW.{ORG} AND NOT v.active)
THEN {signal("deactivation end on an active vehicle")}; END IF;
END"""

LIMIT_GUARD = f"""CREATE TRIGGER cya_vehicle_assignment_limit_bi BEFORE INSERT ON {LIMITS}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='VEHICLE_ASSIGNMENT_LIMIT_SET')
THEN {signal("assignment limit receipt attribution mismatch")}; END IF;
IF NEW.revision <> 1 + (SELECT COUNT(*) FROM {LIMITS} l WHERE l.{ORG}=NEW.{ORG})
THEN {signal("assignment limit revision mismatch")}; END IF;
END"""


def _table_statements(table):
    return [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_ops_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_ops_{table}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
        *(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_ops_{table}_{column}_org_fk` "
            f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
            for column, parent, key in FKS[table]
        ),
        # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
        *(
            f"CREATE TRIGGER cya_{table}_{suffix} BEFORE {operation} ON {table} "
            f"FOR EACH ROW {signal('append-only table')}"
            for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
        ),
    ]


def _reverse_table(table):
    return [
        f"DROP TRIGGER cya_{table}_bd",
        f"DROP TRIGGER cya_{table}_bu",
        *(
            f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_ops_{table}_{column}_org_fk`"
            for column, _parent, _key in reversed(FKS[table])
        ),
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_ops_{table}_roles`",
        f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_ops_{table}_uuid`",
    ]


def forward_statements():
    return [*_table_statements(EVENTS), EVENT_GUARD, *_table_statements(LIMITS), LIMIT_GUARD]


def reverse_statements():
    return [
        "DROP TRIGGER cya_vehicle_assignment_limit_bi",
        *_reverse_table(LIMITS),
        "DROP TRIGGER cya_vehicle_assignment_event_bi",
        *_reverse_table(EVENTS),
    ]


class Migration(migrations.Migration):
    dependencies = [("cya_operations", "0006_vehicle_assignment")]

    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
