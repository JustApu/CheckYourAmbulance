"""Schema-owner guards for vehicle item requirements (SPEC 6.0.0 CHK-11, 18.1 item 8, 18.3).

The four tables are append-only (A) and attributed to a command receipt of the same
organization, actor and instant:

- requirements, their versions and the item-row map are written when a vehicle checklist
  version is published (CHECKLIST_PUBLISH) or by the migration (ITEM_REQUIREMENT_CUTOVER);
- the cutover record only by the migration.

Each version repeats its requirement's vehicle and item (a composite key holds them equal),
takes the next revision, starts after its requirement's latest version, names a position of
its vehicle whose stable slot is its place, and is refused when another requirement of the
same vehicle and item is in force at the same place at any instant of its interval (18.1 item
8, Not placed compared as a value). A version is in force from its effective instant until its
requirement's next version.

A map row links an item row of a vehicle's checklist version to a requirement version of the
same vehicle, item and place with equal requirements (AC-42 (1)), or records it as kit content
exactly when the row is in a kit. Item rows are versioned and never updated, so the link is
this append-only row. After the organization's cutover, a vehicle checklist version is published
only when each of its rows has a map row.
"""

from hashlib import sha1

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers

ORG = "organization_id"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
CUTOVER = "'ITEM_REQUIREMENT_CUTOVER'"
PUBLISH_OR_CUTOVER = "'CHECKLIST_PUBLISH','ITEM_REQUIREMENT_CUTOVER'"
TABLES = {
    "vehicle_item_requirement": (
        [("vehicle_id", "vehicle"), ("item_id", "item")],
        PUBLISH_OR_CUTOVER,
    ),
    "vehicle_item_requirement_version": (
        [
            ("requirement_id", "vehicle_item_requirement"),
            ("vehicle_id", "vehicle"),
            ("item_id", "item"),
            ("compartment_id", "compartment"),
            ("system_id", "vehicle_system"),
        ],
        PUBLISH_OR_CUTOVER,
    ),
    "item_requirement_cutover": ([], CUTOVER),
    "item_requirement_map": (
        [
            ("vehicle_id", "vehicle"),
            ("checklist_version_id", "checklist_version"),
            ("item_def_id", "checklist_item_def"),
            ("requirement_id", "vehicle_item_requirement"),
            ("requirement_version_id", "vehicle_item_requirement_version"),
        ],
        PUBLISH_OR_CUTOVER,
    ),
}
COMMON_FKS = [("command_id", "command_receipt"), ("actor_user_id", "user")]


def key_index(table, name, columns):
    """An index for a composite foreign key, kept when the key is dropped (see module notes)."""
    index = "cya_ix_" + sha1(name.encode()).hexdigest()[:20]
    return f"CREATE INDEX IF NOT EXISTS `{index}` ON `{table}` ({columns})"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def receipt_guard(table, command_types, extra=""):
    return f"""CREATE TRIGGER cya_{table}_bi BEFORE INSERT ON {table}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type IN ({command_types}))
THEN {signal("item requirement receipt attribution mismatch")}; END IF;{extra}
END"""


# The place of a version is a position of its own vehicle, compared by stable slot.
VERSION_EXTRA = f"""
IF (NEW.compartment_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM compartment c
  WHERE c.id=NEW.compartment_id AND c.vehicle_id=NEW.vehicle_id
  AND c.stable_slot_id=NEW.place_slot_id))
 OR (NEW.system_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM vehicle_system s
  WHERE s.id=NEW.system_id AND s.vehicle_id=NEW.vehicle_id
  AND s.stable_slot_id=NEW.place_slot_id))
THEN {signal("an item requirement version names a position of another vehicle")}; END IF;
IF NEW.revision <> COALESCE((SELECT MAX(v.revision)+1 FROM vehicle_item_requirement_version v
  WHERE v.requirement_id=NEW.requirement_id),1)
 OR NEW.effective_at <= COALESCE((SELECT MAX(v.effective_at) FROM vehicle_item_requirement_version v
  WHERE v.requirement_id=NEW.requirement_id),'1000-01-01 00:00:00.000000')
THEN {signal("item requirement version out of order")}; END IF;
IF EXISTS (SELECT 1 FROM vehicle_item_requirement_version o
  WHERE o.vehicle_id=NEW.vehicle_id AND o.item_id=NEW.item_id
  AND o.requirement_id<>NEW.requirement_id
  AND o.place_kind=NEW.place_kind AND o.place_slot_id <=> NEW.place_slot_id
  AND NOT EXISTS (SELECT 1 FROM vehicle_item_requirement_version n
   WHERE n.requirement_id=o.requirement_id AND n.effective_at>o.effective_at
   AND n.effective_at<=NEW.effective_at))
THEN {signal("another item requirement is in force for this item and place")}; END IF;"""

MAP_EXTRA = f"""
IF NOT EXISTS (SELECT 1 FROM checklist_item_def d JOIN checklist_version cv
  ON cv.id=d.checklist_version_id JOIN checklist_template t ON t.id=cv.template_id
  WHERE d.id=NEW.item_def_id AND d.checklist_version_id=NEW.checklist_version_id
  AND t.vehicle_id=NEW.vehicle_id AND (d.kit_id IS NOT NULL)=(NEW.kit_content=1))
THEN {signal("a map row names another vehicle or version, or misstates kit content")}; END IF;
IF NEW.requirement_version_id IS NOT NULL AND NOT EXISTS (SELECT 1
  FROM vehicle_item_requirement_version v JOIN checklist_item_def d ON d.id=NEW.item_def_id
  WHERE v.id=NEW.requirement_version_id AND v.requirement_id=NEW.requirement_id
  AND v.vehicle_id=NEW.vehicle_id AND v.item_id=d.item_id
  AND v.place_kind=(CASE WHEN d.compartment_id IS NOT NULL THEN 'COMPARTMENT'
   WHEN d.system_id IS NOT NULL THEN 'SYSTEM' ELSE 'NOT_PLACED' END)
  AND v.place_slot_id <=> COALESCE(
   (SELECT c.stable_slot_id FROM compartment c WHERE c.id=d.compartment_id),
   (SELECT s.stable_slot_id FROM vehicle_system s WHERE s.id=d.system_id))
  AND v.kind=d.kind AND v.expected_quantity <=> d.expected_quantity
  AND v.unit <=> d.unit AND JSON_EQUALS(v.allowed_outcomes, d.allowed_outcomes)
  AND v.expiration_tracked=d.expiration_tracked AND v.instructions=d.instructions)
THEN {signal("an item row names another requirement than its item and place")}; END IF;"""

EXTRAS = {
    "vehicle_item_requirement_version": VERSION_EXTRA,
    "item_requirement_map": MAP_EXTRA,
}


def _table_statements(table):
    columns, command_types = TABLES[table]
    return [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
        *(
            statement
            for column, parent in COMMON_FKS + columns
            for statement in (
                key_index(table, f"cya_{table}_{column}_org_fk", f"{ORG},{column}"),
                f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_{column}_org_fk` "
                f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
                "ON DELETE RESTRICT ON UPDATE RESTRICT",
            )
        ),
        *append_only_triggers(table),
        receipt_guard(table, command_types, EXTRAS.get(table, "")),
    ]


SAME_REQUIREMENT_FK = (
    "ALTER TABLE `vehicle_item_requirement_version` "
    "ADD CONSTRAINT `cya_vehicle_item_requirement_version_same_fk` "
    "FOREIGN KEY (vehicle_id,item_id,requirement_id) "
    "REFERENCES `vehicle_item_requirement` (vehicle_id,item_id,id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT"
)
VALUE_CHECK = (
    "ALTER TABLE `vehicle_item_requirement_version` "
    "ADD CONSTRAINT `cya_vehicle_item_requirement_version_outcomes` CHECK ("
    "JSON_TYPE(allowed_outcomes)='ARRAY' AND JSON_LENGTH(allowed_outcomes) BETWEEN 1 AND 3 "
    'AND JSON_CONTAINS(\'["PASSED","FAILED","UNABLE_TO_INSPECT"]\', allowed_outcomes) '
    "AND JSON_CONTAINS(allowed_outcomes, '\"PASSED\"'))"
)
PUBLICATION_GUARD = f"""CREATE TRIGGER cya_checklist_publication_itemreq_bi BEFORE INSERT
ON checklist_publication FOR EACH ROW BEGIN
IF NEW.checklist_version_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM item_requirement_cutover c WHERE c.{ORG}=NEW.{ORG})
 AND EXISTS (SELECT 1 FROM checklist_item_def d JOIN checklist_version cv
  ON cv.id=d.checklist_version_id JOIN checklist_template t ON t.id=cv.template_id
  WHERE d.checklist_version_id=NEW.checklist_version_id AND t.vehicle_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM item_requirement_map m WHERE m.item_def_id=d.id))
THEN {signal("every item row names its item requirement after the cutover")}; END IF;
END"""


def forward_statements():
    statements = []
    for table in TABLES:
        statements += _table_statements(table)
    return [
        *statements,
        key_index(
            "vehicle_item_requirement_version",
            "cya_vehicle_item_requirement_version_same_fk",
            "vehicle_id,item_id,requirement_id",
        ),
        SAME_REQUIREMENT_FK,
        VALUE_CHECK,
        PUBLICATION_GUARD,
    ]


def reverse_statements():
    drops = [
        "DROP TRIGGER cya_checklist_publication_itemreq_bi",
        "ALTER TABLE `vehicle_item_requirement_version` "
        "DROP CONSTRAINT `cya_vehicle_item_requirement_version_outcomes`",
        "ALTER TABLE `vehicle_item_requirement_version` "
        "DROP FOREIGN KEY `cya_vehicle_item_requirement_version_same_fk`",
    ]
    for table in reversed(list(TABLES)):
        columns, _types = TABLES[table]
        drops += [
            f"DROP TRIGGER cya_{table}_bi",
            f"DROP TRIGGER cya_{table}_bd",
            f"DROP TRIGGER cya_{table}_bu",
            *(
                f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_{column}_org_fk`"
                for column, _parent in reversed(COMMON_FKS + columns)
            ),
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_roles`",
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_uuid`",
        ]
    return drops


class Migration(migrations.Migration):
    dependencies = [("cya_checklists", "0013_item_requirements")]

    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
