"""Schema-owner guards for vehicle-local position keys (SPEC 5.0.0 IMP-6, CFG-2; issue #220).

vehicle_position_key is append-only (A). The insert guard requires a receipt of the same
organization, actor and instant whose command may set a key: CHECKLIST_CUTOVER, which copies
each vehicle's legacy organization-wide key at the cutover. The key's position must exist on
the key's own vehicle. Uniqueness within the vehicle is a unique key.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers

ORG = "organization_id"
TABLE = "vehicle_position_key"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
FKS = [("command_id", "command_receipt"), ("actor_user_id", "user"), ("vehicle_id", "vehicle")]
COMMAND_TYPES = ("CHECKLIST_CUTOVER",)


def insert_guard(command_types=COMMAND_TYPES):
    allowed = ",".join(f"'{t}'" for t in command_types)
    return f"""CREATE TRIGGER cya_vehicle_position_key_bi BEFORE INSERT ON {TABLE}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type IN ({allowed}))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'position key receipt attribution mismatch'; END IF;
IF (NEW.kind='COMPARTMENT' AND NOT EXISTS (SELECT 1 FROM compartment c
  WHERE c.vehicle_id=NEW.vehicle_id AND c.stable_slot_id=NEW.stable_slot_id))
 OR (NEW.kind='SYSTEM' AND NOT EXISTS (SELECT 1 FROM vehicle_system s
  WHERE s.vehicle_id=NEW.vehicle_id AND s.stable_slot_id=NEW.stable_slot_id))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'position key names no position of its vehicle'; END IF;
END"""


FORWARD = [
    f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
    f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_roles` CHECK ("
    "JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
    f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
    *(
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for column, parent in FKS
    ),
    *append_only_triggers(TABLE),
    insert_guard(),
]
REVERSE = [
    "DROP TRIGGER cya_vehicle_position_key_bi",
    *(f"DROP TRIGGER cya_{TABLE}_{suffix}" for suffix in ("bd", "bu")),
    *(
        f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_{column}_org_fk`"
        for column, _ in reversed(FKS)
    ),
    f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_roles`",
    f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`",
]


class Migration(migrations.Migration):
    dependencies = [("cya_kits", "0015_vehicle_position_keys")]

    operations = [migrations.RunSQL(FORWARD, reverse_sql=REVERSE)]
