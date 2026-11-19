"""Schema-owner guards for vehicle-owned checklists (SPEC 5.0.0 CHK-1, CHK-8, CHK-9, 18.2).

The commands in cya.checklists enforce these rules; the database repeats them so a row written
any other way is refused:
- a vehicle checklist names its owner by an organization-pair key, and its owner and precedence
  never change;
- vehicle checklists exist only once the organization's cutover is recorded, and from then on
  nothing new is written to a legacy template, its versions, its publications or an override;
- an item row of a vehicle checklist version is placed on that same vehicle or not placed at all
  (CHK-8, CHK-9); a legacy template or override row keeps its placement. A kit_id stays what it
  always was, a reference naming a compartment slot (cya.checklists.validation): resolution
  finds the vehicle's own kits in that slot, so the row's compartment is what must be its own;
- an override never overlays a vehicle checklist (CHK-6 withdrawn);
- checklist_cutover is append-only, one per organization, and matches a CHECKLIST_CUTOVER receipt.
Each statement is its own list entry, so Django runs it whole (as 0002 does).
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
CUTOVER = "checklist_cutover"
FKS = [
    ("checklist_template", "cya_chk_template_vehicle_org_fk", "vehicle_id", "vehicle"),
    (CUTOVER, "cya_chk_cutover_command_org_fk", "command_id", "command_receipt"),
    (CUTOVER, "cya_chk_cutover_actor_org_fk", "actor_user_id", "user"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def has_cutover(org_expression):
    return f"EXISTS (SELECT 1 FROM {CUTOVER} c WHERE c.{ORG}={org_expression})"


TEMPLATE_INSERT = f"""CREATE TRIGGER cya_checklist_template_owner_bi BEFORE INSERT ON checklist_template
FOR EACH ROW BEGIN
IF NEW.vehicle_id IS NOT NULL AND NOT {has_cutover("NEW." + ORG)}
THEN {signal("vehicle checklists start at the cutover")}; END IF;
IF NEW.vehicle_id IS NULL AND {has_cutover("NEW." + ORG)}
THEN {signal("legacy checklists are read-only after the cutover")}; END IF;
END"""

TEMPLATE_UPDATE = f"""CREATE TRIGGER cya_checklist_template_owner_bu BEFORE UPDATE ON checklist_template
FOR EACH ROW BEGIN
IF NOT (BINARY OLD.vehicle_id <=> BINARY NEW.vehicle_id)
 OR NOT (OLD.precedence <=> NEW.precedence)
THEN {signal("a checklist owner and precedence never change")}; END IF;
IF OLD.vehicle_id IS NULL AND {has_cutover("OLD." + ORG)}
THEN {signal("legacy checklists are read-only after the cutover")}; END IF;
END"""

VERSION_INSERT = f"""CREATE TRIGGER cya_checklist_version_owner_bi BEFORE INSERT ON checklist_version
FOR EACH ROW BEGIN
IF {has_cutover("NEW." + ORG)} AND EXISTS (SELECT 1 FROM checklist_template t
 WHERE t.id=NEW.template_id AND t.vehicle_id IS NULL)
THEN {signal("legacy checklists are read-only after the cutover")}; END IF;
END"""

OVERRIDE_INSERT = f"""CREATE TRIGGER cya_vehicle_override_version_owner_bi BEFORE INSERT ON vehicle_override_version
FOR EACH ROW BEGIN
IF {has_cutover("NEW." + ORG)}
THEN {signal("legacy checklists are read-only after the cutover")}; END IF;
IF EXISTS (SELECT 1 FROM checklist_version v JOIN checklist_template t ON t.id=v.template_id
 WHERE v.id=NEW.template_version_id AND t.vehicle_id IS NOT NULL)
THEN {signal("a vehicle checklist has no overrides")}; END IF;
END"""

ITEMDEF_INSERT = f"""CREATE TRIGGER cya_checklist_item_def_owner_bi BEFORE INSERT ON checklist_item_def
FOR EACH ROW BEGIN
DECLARE owner CHAR(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL;
IF NEW.checklist_version_id IS NOT NULL THEN
 SELECT t.vehicle_id INTO owner FROM checklist_version v JOIN checklist_template t
  ON t.id=v.template_id WHERE v.id=NEW.checklist_version_id;
END IF;
IF owner IS NULL AND NEW.compartment_id IS NULL AND NEW.system_id IS NULL
THEN {signal("only a vehicle checklist item may be not placed")}; END IF;
IF owner IS NOT NULL AND NEW.compartment_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM compartment p
 WHERE p.id=NEW.compartment_id AND p.vehicle_id=owner)
THEN {signal("a vehicle checklist places items only on its own vehicle")}; END IF;
IF owner IS NOT NULL AND NEW.system_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM vehicle_system p
 WHERE p.id=NEW.system_id AND p.vehicle_id=owner)
THEN {signal("a vehicle checklist places items only on its own vehicle")}; END IF;
END"""

PUBLICATION_INSERT = f"""CREATE TRIGGER cya_checklist_publication_owner_bi BEFORE INSERT ON checklist_publication
FOR EACH ROW BEGIN
IF {has_cutover("NEW." + ORG)} AND (NEW.override_version_id IS NOT NULL
 OR EXISTS (SELECT 1 FROM checklist_version v JOIN checklist_template t ON t.id=v.template_id
  WHERE v.id=NEW.checklist_version_id AND t.vehicle_id IS NULL))
THEN {signal("legacy checklists are read-only after the cutover")}; END IF;
END"""

CUTOVER_INSERT = f"""CREATE TRIGGER cya_checklist_cutover_bi BEFORE INSERT ON {CUTOVER}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='CHECKLIST_CUTOVER')
THEN {signal("cutover receipt attribution mismatch")}; END IF;
END"""

TRIGGERS = [
    TEMPLATE_INSERT,
    TEMPLATE_UPDATE,
    VERSION_INSERT,
    OVERRIDE_INSERT,
    ITEMDEF_INSERT,
    PUBLICATION_INSERT,
    CUTOVER_INSERT,
    # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
    *(
        f"CREATE TRIGGER cya_{CUTOVER}_{suffix} BEFORE {operation} ON {CUTOVER} "
        f"FOR EACH ROW {signal('append-only table')}"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ),
]


def forward_statements():
    return [
        f"ALTER TABLE `{CUTOVER}` ADD CONSTRAINT `cya_chk_{CUTOVER}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{CUTOVER}` ADD CONSTRAINT `cya_chk_{CUTOVER}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
        *(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` FOREIGN KEY ({ORG},{column}) "
            f"REFERENCES `{parent}` ({ORG},id) ON DELETE RESTRICT ON UPDATE RESTRICT"
            for table, name, column, parent in FKS
        ),
        *TRIGGERS,
    ]


def reverse_statements():
    return [
        *(f"DROP TRIGGER {statement.split()[2]}" for statement in reversed(TRIGGERS)),
        *(
            f"ALTER TABLE `{table}` DROP FOREIGN KEY `{name}`"
            for table, name, _column, _parent in reversed(FKS)
        ),
        f"ALTER TABLE `{CUTOVER}` DROP CONSTRAINT `cya_chk_{CUTOVER}_roles`",
        f"ALTER TABLE `{CUTOVER}` DROP CONSTRAINT `cya_chk_{CUTOVER}_uuid`",
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checklists", "0008_vehicle_checklists"),
        ("cya_kits", "0014_vehicle_duplicate_binding_guard"),
    ]

    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
