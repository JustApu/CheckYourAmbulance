"""Schema-owner guards for the 5.0.0 migration maps (SPEC 18.2, issue #220 part 2).

Each map table is append-only (A). Its rows are written only by the migration command, so the
insert guard requires a CHECKLIST_CUTOVER receipt of the same organization, actor and instant.
Organization-pair keys cover every reference; legacy_checklist_map also names a checklist owned
by its vehicle and a legacy template (no owner). Each statement is its own list entry, so Django
runs it whole.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers

ORG = "organization_id"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
TABLES = {
    "legacy_checklist_map": [
        ("vehicle_id", "vehicle"),
        ("legacy_template_id", "checklist_template"),
        ("checklist_id", "checklist_template"),
    ],
    "legacy_version_map": [
        ("checklist_version_id", "checklist_version"),
        ("legacy_template_version_id", "checklist_version"),
        ("legacy_override_version_id", "vehicle_override_version"),
    ],
    "legacy_item_def_map": [
        ("item_def_id", "checklist_item_def"),
        ("legacy_item_def_id", "checklist_item_def"),
    ],
    "legacy_position_map": [("vehicle_id", "vehicle")],
}
COMMON_FKS = [("command_id", "command_receipt"), ("actor_user_id", "user")]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def receipt_guard(table, extra=""):
    return f"""CREATE TRIGGER cya_{table}_bi BEFORE INSERT ON {table}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='CHECKLIST_CUTOVER')
THEN {signal("migration map receipt attribution mismatch")}; END IF;{extra}
END"""


CHECKLIST_MAP_EXTRA = f"""
IF NOT EXISTS (SELECT 1 FROM checklist_template t WHERE t.id=NEW.legacy_template_id
 AND t.vehicle_id IS NULL)
THEN {signal("a legacy map names a legacy template")}; END IF;"""

POSITION_MAP_EXTRA = f"""
IF (NEW.kind='COMPARTMENT' AND NOT EXISTS (SELECT 1 FROM compartment c
  WHERE c.vehicle_id=NEW.vehicle_id AND c.stable_slot_id=NEW.stable_slot_id))
 OR (NEW.kind='SYSTEM' AND NOT EXISTS (SELECT 1 FROM vehicle_system s
  WHERE s.vehicle_id=NEW.vehicle_id AND s.stable_slot_id=NEW.stable_slot_id))
THEN {signal("a position map names no position of its vehicle")}; END IF;"""

EXTRAS = {"legacy_checklist_map": CHECKLIST_MAP_EXTRA, "legacy_position_map": POSITION_MAP_EXTRA}


def _table_statements(table):
    fks = COMMON_FKS + TABLES[table]
    return [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
        *(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_{column}_org_fk` "
            f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
            for column, parent in fks
        ),
        *append_only_triggers(table),
        receipt_guard(table, EXTRAS.get(table, "")),
    ]


# The migrated checklist belongs to the vehicle the map names (template_vehicle_id key).
OWNER_FK = (
    "ALTER TABLE `legacy_checklist_map` ADD CONSTRAINT `cya_legacy_checklist_map_owner_fk` "
    "FOREIGN KEY (vehicle_id,checklist_id) REFERENCES `checklist_template` (vehicle_id,id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT"
)


def forward_statements():
    statements = []
    for table in TABLES:
        statements += _table_statements(table)
    return [*statements, OWNER_FK]


def reverse_statements():
    drops = [
        "ALTER TABLE `legacy_checklist_map` DROP FOREIGN KEY `cya_legacy_checklist_map_owner_fk`"
    ]
    for table in reversed(list(TABLES)):
        fks = COMMON_FKS + TABLES[table]
        drops += [
            f"DROP TRIGGER cya_{table}_bi",
            f"DROP TRIGGER cya_{table}_bd",
            f"DROP TRIGGER cya_{table}_bu",
            *(
                f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_{column}_org_fk`"
                for column, _parent in reversed(fks)
            ),
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_roles`",
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_uuid`",
        ]
    return drops


class Migration(migrations.Migration):
    dependencies = [("cya_checklists", "0010_legacy_maps")]

    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
