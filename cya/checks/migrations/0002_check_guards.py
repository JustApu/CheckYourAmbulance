"""Schema-owner guards for check_record: UUID/role checks, organization-pair keys, triggers.

check_record is append-only (contracts section 2). The insert trigger repeats what the command
layer already enforces, so a writer that bypasses the command still cannot store a row that:
does not match its command receipt (organization, actor, recorded instant, operation id and
command type); names a checklist version no one has published by then; has an item definition
that does not belong to that version (or to an override of it, published by then, for this very
vehicle) and item; or names a kit that is not the vehicle's own in the definition's slot.
Triggers are BEGIN..END blocks in one-item lists: Django would split a plain string at the
semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLE = "check_record"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'

FKS = [
    ("command_id", "command_receipt", "id"),
    ("vehicle_id", "vehicle", "id"),
    ("actor_user_id", "user", "id"),
    ("item_id", "item", "id"),
    ("item_def_id", "checklist_item_def", "id"),
    ("checklist_version_id", "checklist_version", "id"),
    ("kit_id", "kit_instance", "id"),
    ("root_check_id", "check_record", "id"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


RECEIPT_MATCH = """NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.operation_id=NEW.operation_id
 AND r.command_type='CHECK_RECORD')"""

# A check names a version that was already effective when it was recorded; its definition is
# that version's own, or belongs to an override of exactly that version for this vehicle.
CHECK_INSERT = f"""CREATE TRIGGER cya_check_record_bi BEFORE INSERT ON check_record
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH} THEN {signal("check receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM checklist_publication p WHERE p.{ORG}=NEW.{ORG}
 AND p.checklist_version_id=NEW.checklist_version_id AND p.effective_at<=NEW.recorded_at)
THEN {signal("check names an unpublished checklist version")}; END IF;
IF NOT EXISTS (SELECT 1 FROM checklist_item_def d WHERE d.id=NEW.item_def_id
 AND d.{ORG}=NEW.{ORG} AND (d.checklist_version_id=NEW.checklist_version_id
  OR EXISTS (SELECT 1 FROM vehicle_override_version o WHERE o.id=d.override_version_id
   AND o.{ORG}=NEW.{ORG} AND o.vehicle_id=NEW.vehicle_id
   AND o.template_version_id=NEW.checklist_version_id
   AND EXISTS (SELECT 1 FROM checklist_publication op WHERE op.{ORG}=NEW.{ORG}
    AND op.override_version_id=o.id AND op.effective_at<=NEW.recorded_at))))
THEN {signal("check definition does not belong to the version")}; END IF;
IF NEW.kit_id IS NULL AND EXISTS (SELECT 1 FROM checklist_item_def d
 WHERE d.id=NEW.item_def_id AND d.kit_id IS NOT NULL)
THEN {signal("check on a kit item must name the kit")}; END IF;
IF NEW.kit_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM kit_instance k
 JOIN compartment kc ON kc.id=k.compartment_id
 JOIN checklist_item_def d ON d.id=NEW.item_def_id
 JOIN kit_instance dk ON dk.id=d.kit_id
 JOIN compartment dc ON dc.id=dk.compartment_id
 WHERE k.id=NEW.kit_id AND k.{ORG}=NEW.{ORG} AND k.vehicle_id=NEW.vehicle_id
 AND kc.stable_slot_id=dc.stable_slot_id)
THEN {signal("check kit is not a kit of the vehicle for the item")}; END IF;
END"""


def forward_statements():
    statements = [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}' AND BINARY operation_id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
    ]
    statements += [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for column, parent, key in FKS
    ]
    # The definition must be the one for this very item (checklist_item_def unique (id, item_id)).
    statements.append(
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_item_def_pair_fk` "
        "FOREIGN KEY (item_def_id,item_id) REFERENCES `checklist_item_def` (id,item_id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
    )
    # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
    statements += [
        f"CREATE TRIGGER cya_{TABLE}_{suffix} BEFORE {operation} ON {TABLE} "
        f"FOR EACH ROW {signal('append-only table')}"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    return [*statements, CHECK_INSERT]


def reverse_statements():
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in forward_statements()
        if statement.startswith("CREATE TRIGGER ")
    ]
    drops.append(f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_item_def_pair_fk`")
    drops += [
        f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_{column}_org_fk`"
        for column, *_ in reversed(FKS)
    ]
    drops += [
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_roles`",
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`",
    ]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0001_initial"),
        ("cya_checklists", "0002_publication_guards"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_accounts", "0002_identity_guards"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
