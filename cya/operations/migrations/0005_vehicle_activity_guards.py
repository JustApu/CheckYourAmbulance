"""Schema-owner guards for vehicle_activity_event (SPEC 4.8 CFG-1, issue #130 part 2).

Append-only, like vehicle_status_event. The insert trigger repeats what the commands enforce:
- the row matches its receipt (organization, actor, recorded instant);
- a CHANGE event comes only from VEHICLE_CREATE, VEHICLE_IMPORT, VEHICLE_ACTIVE_SET or
  VEHICLE_DUPLICATE; a BASELINE event only from VEHICLE_ACTIVITY_BASELINE, only as a vehicle's
  first event (the model's checks also require sequence 1);
- events form one linear chain per vehicle;
- the event's active flag equals the vehicle's flag when it is written, so the flag and its
  history cannot disagree (the commands update the vehicle row first).
Each statement is its own list entry, so Django runs it whole (as 0002 does).
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLE = "vehicle_activity_event"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
CHANGE_COMMANDS = "'VEHICLE_CREATE','VEHICLE_IMPORT','VEHICLE_ACTIVE_SET','VEHICLE_DUPLICATE'"
FKS = [
    ("command_id", "command_receipt", "id"),
    ("vehicle_id", "vehicle", "id"),
    ("actor_user_id", "user", "id"),
    ("prior_event_id", TABLE, "id"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


INSERT_GUARD = f"""CREATE TRIGGER cya_vehicle_activity_event_bi BEFORE INSERT ON {TABLE}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at
 AND ((NEW.basis='CHANGE' AND r.command_type IN ({CHANGE_COMMANDS}))
  OR (NEW.basis='BASELINE' AND r.command_type='VEHICLE_ACTIVITY_BASELINE')))
THEN {signal("activity receipt attribution mismatch")}; END IF;
IF NEW.basis='BASELINE' AND EXISTS (
 SELECT 1 FROM {TABLE} e WHERE e.{ORG}=NEW.{ORG} AND e.vehicle_id=NEW.vehicle_id)
THEN {signal("activity baseline after events")}; END IF;
IF NEW.prior_event_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM {TABLE} e WHERE e.id=NEW.prior_event_id AND e.{ORG}=NEW.{ORG}
 AND e.vehicle_id=NEW.vehicle_id AND e.sequence=NEW.sequence-1 AND e.recorded_at<=NEW.recorded_at)
THEN {signal("activity chain mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM vehicle v WHERE v.id=NEW.vehicle_id AND v.{ORG}=NEW.{ORG}
 AND v.active=NEW.active)
THEN {signal("activity disagrees with the vehicle")}; END IF;
END"""


def forward_statements():
    return [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_ops_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_ops_{TABLE}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
        *(
            f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_ops_{TABLE}_{column}_org_fk` "
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
        INSERT_GUARD,
    ]


def reverse_statements():
    return [
        "DROP TRIGGER cya_vehicle_activity_event_bi",
        f"DROP TRIGGER cya_{TABLE}_bd",
        f"DROP TRIGGER cya_{TABLE}_bu",
        *(
            f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_ops_{TABLE}_{column}_org_fk`"
            for column, _parent, _key in reversed(FKS)
        ),
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_ops_{TABLE}_roles`",
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_ops_{TABLE}_uuid`",
    ]


class Migration(migrations.Migration):
    dependencies = [("cya_operations", "0004_vehicle_activity")]

    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
