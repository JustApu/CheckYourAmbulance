"""Enforce command types in obligations insert triggers.

Extends cya_requirement_binding_bi to require command_type = 'REQUIREMENT_BIND',
and cya_requirement_version_bi, cya_obligation_bi, cya_obligation_selection_bi,
and cya_odometer_obligation_bi to require command_type = 'OBLIGATIONS_RECONCILE'.
"""

from django.db import migrations

ORG = "organization_id"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def receipt_predicate(command_type):
    return (
        "NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id\n"
        " AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id\n"
        " AND r.service_identity <=> NEW.service_identity AND r.job_key <=> NEW.job_key\n"
        f" AND r.recorded_at=NEW.recorded_at AND r.command_type = '{command_type}')"
    )


RECEIPT_MATCH_OLD = """NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.service_identity <=> NEW.service_identity AND r.job_key <=> NEW.job_key
 AND r.recorded_at=NEW.recorded_at)"""

# --- NEW TRIGGERS ---

BINDING_INSERT_NEW = f"""CREATE TRIGGER cya_requirement_binding_bi BEFORE INSERT ON requirement_binding
FOR EACH ROW BEGIN
IF {receipt_predicate("REQUIREMENT_BIND")} THEN {signal("binding receipt attribution mismatch")}; END IF;
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

OBLIGATION_INSERT_NEW = f"""CREATE TRIGGER cya_obligation_bi BEFORE INSERT ON obligation
FOR EACH ROW BEGIN
IF {receipt_predicate("OBLIGATIONS_RECONCILE")} THEN {signal("obligation receipt attribution mismatch")}; END IF;
END"""

ODOMETER_INSERT_NEW = f"""CREATE TRIGGER cya_odometer_obligation_bi BEFORE INSERT ON odometer_obligation
FOR EACH ROW BEGIN
IF {receipt_predicate("OBLIGATIONS_RECONCILE")} THEN {signal("odometer receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM schedule_rule s WHERE s.id=NEW.schedule_rule_id
 AND s.organization_id=NEW.organization_id
 AND NEW.scheduled_inspection_key = CONCAT(s.slot, '@',
  DATE_FORMAT(NEW.due_at, '%Y-%m-%dT%H:%i:%s.%fZ'), '#', NEW.schedule_rule_id))
THEN {signal("odometer inspection key mismatch")}; END IF;
END"""

VERSION_INSERT_NEW = f"""CREATE TRIGGER cya_requirement_version_bi BEFORE INSERT ON requirement_version
FOR EACH ROW BEGIN
IF {receipt_predicate("OBLIGATIONS_RECONCILE")} THEN {signal("requirement receipt attribution mismatch")}; END IF;
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
IF NEW.kit_instance_id IS NULL THEN
 IF EXISTS (SELECT 1 FROM checklist_item_def d WHERE d.id=NEW.item_def_id AND d.kit_id IS NOT NULL)
 THEN {signal("requirement for a kit item must name a kit instance")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM kit_assignment_event e
  JOIN compartment ec ON ec.id=e.compartment_id
  JOIN checklist_item_def d ON d.id=NEW.item_def_id
  JOIN kit_instance rk ON rk.id=d.kit_id
  LEFT JOIN kit_assignment_event rf ON rf.kit_id=rk.id AND rf.sequence=1
  JOIN compartment rc ON rc.id=IF(rf.id IS NOT NULL, rf.compartment_id, rk.compartment_id)
  WHERE e.id=NEW.kit_event_id AND e.{ORG}=NEW.{ORG} AND e.kit_id=NEW.kit_instance_id
  AND e.vehicle_id=NEW.vehicle_id AND e.active=1 AND ec.stable_slot_id=rc.stable_slot_id)
 THEN {signal("requirement kit instance is not active on the vehicle in the item slot")}; END IF;
END IF;
END"""

SELECTION_INSERT_NEW = f"""CREATE TRIGGER cya_obligation_selection_bi BEFORE INSERT ON obligation_selection
FOR EACH ROW BEGIN
DECLARE prior_selected CHAR(36) CHARACTER SET ascii;
IF {receipt_predicate("OBLIGATIONS_RECONCILE")} THEN {signal("selection receipt attribution mismatch")}; END IF;
IF NEW.sequence > 1 THEN
 IF NOT EXISTS (SELECT 1 FROM obligation_selection s WHERE s.id=NEW.previous_selection_id
  AND s.organization_id=NEW.organization_id AND s.vehicle_id=NEW.vehicle_id
  AND s.item_id=NEW.item_id AND s.kit_key=NEW.kit_key AND s.due_at=NEW.due_at
  AND s.sequence=NEW.sequence-1)
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
  AND s.vehicle_id=NEW.vehicle_id AND s.item_id=NEW.item_id AND s.kit_key=NEW.kit_key
  AND s.due_at=NEW.due_at
  AND s.selected_obligation_id=NEW.selected_obligation_id)
 THEN {signal("obligation already selected")}; END IF;
END IF;
END"""

# --- OLD TRIGGERS (from 0002 and 0003) ---

BINDING_INSERT_OLD = f"""CREATE TRIGGER cya_requirement_binding_bi BEFORE INSERT ON requirement_binding
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH_OLD} THEN {signal("binding receipt attribution mismatch")}; END IF;
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

OBLIGATION_INSERT_OLD = f"""CREATE TRIGGER cya_obligation_bi BEFORE INSERT ON obligation
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH_OLD} THEN {signal("obligation receipt attribution mismatch")}; END IF;
END"""

ODOMETER_INSERT_OLD = f"""CREATE TRIGGER cya_odometer_obligation_bi BEFORE INSERT ON odometer_obligation
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH_OLD} THEN {signal("odometer receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM schedule_rule s WHERE s.id=NEW.schedule_rule_id
 AND s.organization_id=NEW.organization_id
 AND NEW.scheduled_inspection_key = CONCAT(s.slot, '@',
  DATE_FORMAT(NEW.due_at, '%Y-%m-%dT%H:%i:%s.%fZ'), '#', NEW.schedule_rule_id))
THEN {signal("odometer inspection key mismatch")}; END IF;
END"""

VERSION_INSERT_OLD = f"""CREATE TRIGGER cya_requirement_version_bi BEFORE INSERT ON requirement_version
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH_OLD} THEN {signal("requirement receipt attribution mismatch")}; END IF;
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
IF NEW.kit_instance_id IS NULL THEN
 IF EXISTS (SELECT 1 FROM checklist_item_def d WHERE d.id=NEW.item_def_id AND d.kit_id IS NOT NULL)
 THEN {signal("requirement for a kit item must name a kit instance")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM kit_assignment_event e
  JOIN compartment ec ON ec.id=e.compartment_id
  JOIN checklist_item_def d ON d.id=NEW.item_def_id
  JOIN kit_instance rk ON rk.id=d.kit_id
  LEFT JOIN kit_assignment_event rf ON rf.kit_id=rk.id AND rf.sequence=1
  JOIN compartment rc ON rc.id=IF(rf.id IS NOT NULL, rf.compartment_id, rk.compartment_id)
  WHERE e.id=NEW.kit_event_id AND e.{ORG}=NEW.{ORG} AND e.kit_id=NEW.kit_instance_id
  AND e.vehicle_id=NEW.vehicle_id AND e.active=1 AND ec.stable_slot_id=rc.stable_slot_id)
 THEN {signal("requirement kit instance is not active on the vehicle in the item slot")}; END IF;
END IF;
END"""

SELECTION_INSERT_OLD = f"""CREATE TRIGGER cya_obligation_selection_bi BEFORE INSERT ON obligation_selection
FOR EACH ROW BEGIN
DECLARE prior_selected CHAR(36) CHARACTER SET ascii;
IF {RECEIPT_MATCH_OLD} THEN {signal("selection receipt attribution mismatch")}; END IF;
IF NEW.sequence > 1 THEN
 IF NOT EXISTS (SELECT 1 FROM obligation_selection s WHERE s.id=NEW.previous_selection_id
  AND s.organization_id=NEW.organization_id AND s.vehicle_id=NEW.vehicle_id
  AND s.item_id=NEW.item_id AND s.kit_key=NEW.kit_key AND s.due_at=NEW.due_at
  AND s.sequence=NEW.sequence-1)
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
  AND s.vehicle_id=NEW.vehicle_id AND s.item_id=NEW.item_id AND s.kit_key=NEW.kit_key
  AND s.due_at=NEW.due_at
  AND s.selected_obligation_id=NEW.selected_obligation_id)
 THEN {signal("obligation already selected")}; END IF;
END IF;
END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_obligations", "0004_shared_satisfaction_rule"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TRIGGER IF EXISTS cya_requirement_binding_bi",
                "DROP TRIGGER IF EXISTS cya_requirement_version_bi",
                "DROP TRIGGER IF EXISTS cya_obligation_bi",
                "DROP TRIGGER IF EXISTS cya_obligation_selection_bi",
                "DROP TRIGGER IF EXISTS cya_odometer_obligation_bi",
                BINDING_INSERT_NEW,
                VERSION_INSERT_NEW,
                OBLIGATION_INSERT_NEW,
                SELECTION_INSERT_NEW,
                ODOMETER_INSERT_NEW,
            ],
            reverse_sql=[
                "DROP TRIGGER IF EXISTS cya_requirement_binding_bi",
                "DROP TRIGGER IF EXISTS cya_requirement_version_bi",
                "DROP TRIGGER IF EXISTS cya_obligation_bi",
                "DROP TRIGGER IF EXISTS cya_obligation_selection_bi",
                "DROP TRIGGER IF EXISTS cya_odometer_obligation_bi",
                BINDING_INSERT_OLD,
                VERSION_INSERT_OLD,
                OBLIGATION_INSERT_OLD,
                SELECTION_INSERT_OLD,
                ODOMETER_INSERT_OLD,
            ],
        ),
    ]
