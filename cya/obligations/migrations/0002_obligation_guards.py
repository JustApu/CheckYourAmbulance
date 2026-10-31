"""Schema-owner guards: UUID/role checks, organization-pair keys, append-only and stream triggers.

All five tables are append-only (contracts section 2). The insert triggers repeat what the
command layer already enforces, so that a compromised or buggy writer still cannot break the
invariants: every row matches its command receipt (organization, actor, worker identity, job
key and recorded instant); a requirement version's pinned inputs belong together; and the
obligation_selection stream is gapless per anchor, names the tail, carries the SUPERSEDED
determination of the same command exactly when it supersedes a selected obligation, and never
selects an obligation twice. Each trigger is one item in the RunSQL list: Django would split a
plain string at the semicolons inside a trigger body.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLES = (
    "requirement_binding",
    "requirement_version",
    "obligation",
    "obligation_selection",
    "odometer_obligation",
)
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'

# (table, column, parent table, parent key): organization-pair keys, so a row can never name
# another organization's receipt, user, vehicle, item, rule or publication.
FKS = [
    ("requirement_binding", "command_id", "command_receipt", "id"),
    ("requirement_binding", "actor_user_id", "user", "id"),
    ("requirement_binding", "vehicle_id", "vehicle", "id"),
    ("requirement_binding", "template_id", "checklist_template", "id"),
    ("requirement_version", "command_id", "command_receipt", "id"),
    ("requirement_version", "actor_user_id", "user", "id"),
    ("requirement_version", "vehicle_id", "vehicle", "id"),
    ("requirement_version", "item_id", "item", "id"),
    ("requirement_version", "item_def_id", "checklist_item_def", "id"),
    ("requirement_version", "publication_id", "checklist_publication", "id"),
    ("requirement_version", "override_version_id", "vehicle_override_version", "id"),
    ("requirement_version", "binding_id", "requirement_binding", "id"),
    ("requirement_version", "schedule_rule_id", "schedule_rule", "id"),
    ("requirement_version", "conditional_rule_id", "conditional_rule", "id"),
    ("obligation", "command_id", "command_receipt", "id"),
    ("obligation", "actor_user_id", "user", "id"),
    ("obligation", "vehicle_id", "vehicle", "id"),
    ("obligation", "item_id", "item", "id"),
    ("obligation", "requirement_version_id", "requirement_version", "id"),
    ("obligation_selection", "command_id", "command_receipt", "id"),
    ("obligation_selection", "actor_user_id", "user", "id"),
    ("obligation_selection", "vehicle_id", "vehicle", "id"),
    ("obligation_selection", "item_id", "item", "id"),
    ("obligation_selection", "selected_obligation_id", "obligation", "id"),
    ("obligation_selection", "previous_selection_id", "obligation_selection", "id"),
    ("obligation_selection", "supersession_determination_id", "obligation_determination", "id"),
    ("odometer_obligation", "command_id", "command_receipt", "id"),
    ("odometer_obligation", "actor_user_id", "user", "id"),
    ("odometer_obligation", "vehicle_id", "vehicle", "id"),
    ("odometer_obligation", "schedule_rule_id", "schedule_rule", "id"),
]

# (name, table, child columns, parent table, parent columns): the row's own anchor must agree
# with what it references, which the organization-pair keys above cannot say.
ANCHOR_FKS = [
    (
        "item_def_item",
        "requirement_version",
        "item_def_id, item_id",
        "checklist_item_def",
        "id, item_id",
    ),
    (
        "version_anchor",
        "obligation",
        "requirement_version_id, vehicle_id, item_id",
        "requirement_version",
        "id, vehicle_id, item_id",
    ),
    (
        "selected_anchor",
        "obligation_selection",
        "selected_obligation_id, vehicle_id, item_id, due_at",
        "obligation",
        "id, vehicle_id, item_id, due_at",
    ),
    (
        "previous_anchor",
        "obligation_selection",
        "previous_selection_id, vehicle_id, item_id, due_at",
        "obligation_selection",
        "id, vehicle_id, item_id, due_at",
    ),
]


def fk_name(number):
    # MariaDB identifiers are limited to 64 characters; the column names here would exceed it.
    return f"cya_obl_org_fk_{number:02d}"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


RECEIPT_MATCH = """NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.service_identity <=> NEW.service_identity AND r.job_key <=> NEW.job_key
 AND r.recorded_at=NEW.recorded_at)"""


def attribution_trigger(table, message):
    return f"""CREATE TRIGGER cya_{table}_bi BEFORE INSERT ON {table}
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH} THEN {signal(message)}; END IF;
END"""


# The key names the inspection exactly: the schedule row's slot, the due instant and the row.
ODOMETER_INSERT = f"""CREATE TRIGGER cya_odometer_obligation_bi BEFORE INSERT ON odometer_obligation
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH} THEN {signal("odometer receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM schedule_rule s WHERE s.id=NEW.schedule_rule_id
 AND s.organization_id=NEW.organization_id
 AND NEW.scheduled_inspection_key = CONCAT(s.slot, '@',
  DATE_FORMAT(NEW.due_at, '%Y-%m-%dT%H:%i:%s.%fZ'), '#', NEW.schedule_rule_id))
THEN {signal("odometer inspection key mismatch")}; END IF;
END"""

# A bound template is always active. The active flag has no history, so the generator cannot
# read it reproducibly; instead the database refuses to deactivate a template while any vehicle
# is still (or will be) bound to it, so deactivation can only follow a recorded, dated unbind,
# which is what actually stops obligations. Re-activating is always allowed.
TEMPLATE_DEACTIVATE = f"""CREATE TRIGGER cya_checklist_template_deactivate_bu
BEFORE UPDATE ON checklist_template FOR EACH ROW BEGIN
IF OLD.active <> 0 AND NEW.active = 0 AND EXISTS (
 SELECT 1 FROM requirement_binding b
 WHERE b.organization_id=OLD.organization_id AND b.template_id=OLD.id AND b.action='BIND'
 AND NOT EXISTS (SELECT 1 FROM requirement_binding n
  WHERE n.vehicle_id=b.vehicle_id AND n.template_id=b.template_id AND n.slot=b.slot
  AND n.revision > b.revision AND n.effective_at <= UTC_TIMESTAMP(6)))
THEN {signal("template is still bound to a vehicle; unbind it first")}; END IF;
END"""

# One revision chain per (vehicle, template, slot): consecutive revisions, strictly later
# effective instants, never before the record itself (no backfill, OQ-31), and a real slot.
BINDING_INSERT = f"""CREATE TRIGGER cya_requirement_binding_bi BEFORE INSERT ON requirement_binding
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH} THEN {signal("binding receipt attribution mismatch")}; END IF;
IF NEW.effective_at < NEW.recorded_at THEN {signal("binding is retroactive")}; END IF;
IF NEW.revision = 1 AND EXISTS (SELECT 1 FROM requirement_binding b
 WHERE b.vehicle_id=NEW.vehicle_id AND b.template_id=NEW.template_id AND b.slot=NEW.slot)
THEN {signal("binding revision chain mismatch")}; END IF;
IF NEW.revision > 1 AND NOT EXISTS (SELECT 1 FROM requirement_binding b
 WHERE b.organization_id=NEW.organization_id AND b.vehicle_id=NEW.vehicle_id
 AND b.template_id=NEW.template_id AND b.slot=NEW.slot AND b.revision=NEW.revision-1
 AND b.effective_at < NEW.effective_at)
THEN {signal("binding revision chain mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM schedule_rule s
 WHERE s.organization_id=NEW.organization_id AND s.slot=NEW.slot)
THEN {signal("binding names an unknown schedule slot")}; END IF;
END"""

# The pinned inputs must belong together: the publication governs the binding's template, the
# definition is the publication's or the override's, the override overlays that very version
# for this vehicle, and the schedule row is the binding's slot.
VERSION_INSERT = f"""CREATE TRIGGER cya_requirement_version_bi BEFORE INSERT ON requirement_version
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH} THEN {signal("requirement receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM requirement_binding b
 JOIN checklist_publication p ON p.organization_id=b.organization_id
 JOIN checklist_version v ON v.id=p.checklist_version_id
 WHERE b.id=NEW.binding_id AND b.organization_id=NEW.organization_id
 AND b.vehicle_id=NEW.vehicle_id AND b.action='BIND'
 AND p.id=NEW.publication_id AND v.template_id=b.template_id)
THEN {signal("requirement binding or publication mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM checklist_item_def d
 JOIN checklist_publication p ON p.id=NEW.publication_id
 WHERE d.id=NEW.item_def_id AND d.organization_id=NEW.organization_id
 AND (d.checklist_version_id=p.checklist_version_id
  OR (NEW.override_version_id IS NOT NULL AND d.override_version_id=NEW.override_version_id)))
THEN {signal("requirement definition mismatch")}; END IF;
IF NEW.override_version_id IS NOT NULL AND NOT EXISTS (SELECT 1
 FROM vehicle_override_version o JOIN checklist_publication p ON p.id=NEW.publication_id
 WHERE o.id=NEW.override_version_id AND o.vehicle_id=NEW.vehicle_id
 AND o.template_version_id=p.checklist_version_id)
THEN {signal("requirement override mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM schedule_rule s JOIN requirement_binding b ON b.id=NEW.binding_id
 WHERE s.id=NEW.schedule_rule_id AND s.organization_id=NEW.organization_id AND s.slot=b.slot)
THEN {signal("requirement schedule mismatch")}; END IF;
IF NEW.conditional_rule_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM conditional_rule c
 WHERE c.id=NEW.conditional_rule_id AND c.organization_id=NEW.organization_id
 AND c.rule_type='CARRY_OVER_FROM_PRIOR_SHIFT'
 AND JSON_VALUE(c.typed_inputs, '$.item_id')=NEW.item_id)
THEN {signal("requirement conditional rule mismatch")}; END IF;
END"""

# The stream: first event is SELECT; each later event follows the tail of the same anchor by
# exactly one; replacing or retiring a selected obligation carries that obligation's SUPERSEDED
# determination from this same command; after a RETIRE only a REPLACE without one may follow;
# the selected obligation was created by this command and was never selected before.
SELECTION_INSERT = f"""CREATE TRIGGER cya_obligation_selection_bi BEFORE INSERT ON obligation_selection
FOR EACH ROW BEGIN
DECLARE prior_selected CHAR(36) CHARACTER SET ascii;
IF {RECEIPT_MATCH} THEN {signal("selection receipt attribution mismatch")}; END IF;
IF NEW.sequence > 1 THEN
 IF NOT EXISTS (SELECT 1 FROM obligation_selection s WHERE s.id=NEW.previous_selection_id
  AND s.organization_id=NEW.organization_id AND s.vehicle_id=NEW.vehicle_id
  AND s.item_id=NEW.item_id AND s.due_at=NEW.due_at AND s.sequence=NEW.sequence-1)
 THEN {signal("selection predecessor mismatch")}; END IF;
 SELECT s.selected_obligation_id INTO prior_selected FROM obligation_selection s
  WHERE s.id=NEW.previous_selection_id;
 IF prior_selected IS NOT NULL THEN
  IF NEW.supersession_determination_id IS NULL OR NOT EXISTS (SELECT 1
   FROM obligation_determination d WHERE d.id=NEW.supersession_determination_id
   AND d.organization_id=NEW.organization_id AND d.command_id=NEW.command_id
   AND d.determination_type='SUPERSEDED' AND d.obligation_id=prior_selected
   AND d.vehicle_id=NEW.vehicle_id AND d.due_at=NEW.due_at)
  THEN {signal("selection supersession mismatch")}; END IF;
 ELSE
  IF NEW.selection_type <> 'REPLACE' OR NEW.supersession_determination_id IS NOT NULL
  THEN {signal("selection after retirement mismatch")}; END IF;
 END IF;
END IF;
IF NEW.selected_obligation_id IS NOT NULL THEN
 IF NOT EXISTS (SELECT 1 FROM obligation o WHERE o.id=NEW.selected_obligation_id
  AND o.organization_id=NEW.organization_id AND o.command_id=NEW.command_id)
 THEN {signal("selected obligation was not created by this command")}; END IF;
 IF EXISTS (SELECT 1 FROM obligation_selection s WHERE s.organization_id=NEW.organization_id
  AND s.vehicle_id=NEW.vehicle_id AND s.item_id=NEW.item_id AND s.due_at=NEW.due_at
  AND s.selected_obligation_id=NEW.selected_obligation_id)
 THEN {signal("obligation already selected")}; END IF;
END IF;
END"""


def forward_statements():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_obl_{table}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')"
        for table in TABLES
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_obl_{table}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))"
        for table in TABLES
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `{fk_name(number)}` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},{key}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for number, (table, column, parent, key) in enumerate(FKS)
    ]
    statements += [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_obl_{name}_fk` "
        f"FOREIGN KEY ({columns}) REFERENCES `{parent}` ({parent_columns}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for name, table, columns, parent, parent_columns in ANCHOR_FKS
    ]
    # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
    statements += [
        f"CREATE TRIGGER cya_{table}_{suffix} BEFORE {operation} ON {table} "
        f"FOR EACH ROW {signal('append-only table')}"
        for table in TABLES
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    return [
        *statements,
        BINDING_INSERT,
        VERSION_INSERT,
        attribution_trigger("obligation", "obligation receipt attribution mismatch"),
        SELECTION_INSERT,
        ODOMETER_INSERT,
        TEMPLATE_DEACTIVATE,
    ]


def reverse_statements():
    drops = [
        f"DROP TRIGGER {statement.split()[2]}"
        for statement in forward_statements()
        if statement.startswith("CREATE TRIGGER ")
    ]
    drops += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_obl_{name}_fk`"
        for name, table, *_ in reversed(ANCHOR_FKS)
    ]
    drops += [
        f"ALTER TABLE `{FKS[number][0]}` DROP FOREIGN KEY `{fk_name(number)}`"
        for number in reversed(range(len(FKS)))
    ]
    for table in reversed(TABLES):
        drops += [
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_obl_{table}_roles`",
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_obl_{table}_uuid`",
        ]
    return drops


class Migration(migrations.Migration):
    dependencies = [
        ("cya_obligations", "0001_initial"),
        ("cya_checklists", "0002_publication_guards"),
        ("cya_schedules", "0002_schedule_guards"),
        ("cya_conditions", "0002_conditions_guards"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_accounts", "0002_identity_guards"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
