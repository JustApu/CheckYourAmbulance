"""Protect links from a full seal inspection to item obligations."""

from importlib import import_module

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

guards = import_module("cya.seals.migrations.0002_evidence_guards")
TABLE = "seal_inspection_obligation"
FKS = (
    ("command_id", "command_receipt"),
    ("actor_user_id", "user"),
    ("inspection_item_id", "seal_inspection_item"),
    ("obligation_id", "obligation"),
    ("shared_rule_id", "shared_satisfaction_rule"),
)

LINK_GUARD = """IF NOT EXISTS (SELECT 1 FROM seal_inspection_item item
 JOIN seal_inspection inspection ON inspection.id=item.inspection_id
 AND inspection.organization_id=item.organization_id
 JOIN seal_event ev ON ev.id=inspection.reseal_id
 AND ev.organization_id=inspection.organization_id
 JOIN obligation ob ON ob.id=NEW.obligation_id
 AND ob.organization_id=item.organization_id
 WHERE item.id=NEW.inspection_item_id AND item.organization_id=NEW.organization_id
 AND item.command_id=NEW.command_id AND item.recorded_at=NEW.recorded_at
 AND ob.vehicle_id=ev.vehicle_id AND ob.item_id=item.item_id
 AND ob.kit_key=COALESCE(item.kit_id,'00000000-0000-0000-0000-000000000000')
 AND (ob.routine=0 AND ob.recorded_at<=NEW.recorded_at OR EXISTS
  (SELECT 1 FROM obligation_selection s WHERE s.organization_id=ob.organization_id
   AND s.vehicle_id=ob.vehicle_id AND s.item_id=ob.item_id
   AND s.kit_key=ob.kit_key AND s.due_at=ob.due_at
   AND s.selected_obligation_id=ob.id AND s.recorded_at<=NEW.recorded_at
   AND s.sequence=(SELECT MAX(x.sequence) FROM obligation_selection x
    WHERE x.organization_id=s.organization_id AND x.vehicle_id=s.vehicle_id
    AND x.item_id=s.item_id AND x.kit_key=s.kit_key AND x.due_at=s.due_at
    AND x.recorded_at<=NEW.recorded_at))))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal inspection obligation scope mismatch'; END IF;
IF NEW.ordinal=0 AND EXISTS (SELECT 1 FROM seal_inspection_obligation prior
 WHERE prior.inspection_item_id=NEW.inspection_item_id)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'primary inspection link must be first'; END IF;
IF NEW.ordinal>0 AND NOT EXISTS (SELECT 1 FROM seal_inspection_obligation primary_link
 JOIN obligation primary_ob ON primary_ob.id=primary_link.obligation_id
 JOIN obligation target_ob ON target_ob.id=NEW.obligation_id
 JOIN shared_satisfaction_rule rule ON rule.id=NEW.shared_rule_id
 JOIN shared_satisfaction_rule_member member ON member.rule_id=rule.id
 AND member.organization_id=NEW.organization_id
 AND member.source_requirement_version_id=primary_ob.requirement_version_id
 AND member.additional_requirement_version_id=target_ob.requirement_version_id
 WHERE primary_link.inspection_item_id=NEW.inspection_item_id AND primary_link.ordinal=0
 AND primary_link.organization_id=NEW.organization_id AND rule.organization_id=NEW.organization_id
 AND rule.effective_at<=NEW.recorded_at AND rule.recorded_at<=NEW.recorded_at
 AND rule.id=(SELECT r.id FROM shared_satisfaction_rule r
  WHERE r.organization_id=NEW.organization_id AND r.effective_at<=NEW.recorded_at
  AND r.recorded_at<=NEW.recorded_at ORDER BY r.effective_at DESC,r.revision DESC,r.id DESC LIMIT 1))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal inspection shared rule mismatch'; END IF;"""


def forward():
    statements = [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{guards.ROLE_CODES}', actor_roles_at_time))",
    ]
    for column, parent in FKS:
        statements.append(
            f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_{column}_org_fk` "
            f"FOREIGN KEY (organization_id,{column}) REFERENCES `{parent}` (organization_id,id) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
        )
    statements.extend(
        f"CREATE TRIGGER cya_{TABLE}_{suffix} BEFORE {operation} ON `{TABLE}` "
        f"FOR EACH ROW {guards.signal('append-only table')}"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    )
    statements.append(guards.insert_guard(TABLE, "'SEAL_RESEAL'", LINK_GUARD))
    return statements


def reverse():
    statements = [f"DROP TRIGGER cya_{TABLE}_{suffix}" for suffix in ("bi", "bd", "bu")]
    statements.extend(
        f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_{column}_org_fk`"
        for column, _ in reversed(FKS)
    )
    statements.extend(
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_{name}`" for name in ("roles", "uuid")
    )
    return statements


class Migration(migrations.Migration):
    dependencies = [("cya_seals", "0003_inspection_obligation")]
    operations = [migrations.RunSQL(forward(), reverse_sql=reverse())]
