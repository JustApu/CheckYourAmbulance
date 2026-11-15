"""Organization and append-only guards for shared seal evidence."""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

TABLES = (
    "seal_event",
    "seal_substitution",
    "seal_substitution_obligation",
    "seal_inspection",
    "seal_inspection_item",
    "seal_inspection_kit_count",
    "seal_inspection_kit_instance",
    "seal_inspection_kit_verification",
)
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
FKS = {
    "seal_event": (
        ("command_id", "command_receipt"),
        ("actor_user_id", "user"),
        ("vehicle_id", "vehicle"),
        ("kit_id", "kit_instance"),
        ("prior_event_id", "seal_event"),
        ("audit_request_id", "seal_event"),
    ),
    "seal_substitution": (
        ("command_id", "command_receipt"),
        ("actor_user_id", "user"),
        ("vehicle_id", "vehicle"),
        ("seal_event_id", "seal_event"),
    ),
    "seal_substitution_obligation": (
        ("command_id", "command_receipt"),
        ("actor_user_id", "user"),
        ("substitution_id", "seal_substitution"),
        ("obligation_id", "obligation"),
        ("item_id", "item"),
        ("shared_rule_id", "shared_satisfaction_rule"),
        ("conditional_rule_id", "conditional_rule"),
    ),
    "seal_inspection": (
        ("command_id", "command_receipt"),
        ("actor_user_id", "user"),
        ("reseal_id", "seal_event"),
        ("break_event_id", "seal_event"),
    ),
    "seal_inspection_item": (
        ("command_id", "command_receipt"),
        ("actor_user_id", "user"),
        ("inspection_id", "seal_inspection"),
        ("item_id", "item"),
        ("kit_id", "kit_instance"),
        ("item_def_id", "checklist_item_def"),
        ("kit_definition_item_id", "kit_definition_item"),
    ),
    "seal_inspection_kit_count": (
        ("command_id", "command_receipt"),
        ("actor_user_id", "user"),
        ("inspection_id", "seal_inspection"),
        ("kit_requirement_id", "vehicle_kit_requirement"),
    ),
    "seal_inspection_kit_instance": (
        ("command_id", "command_receipt"),
        ("actor_user_id", "user"),
        ("kit_count_id", "seal_inspection_kit_count"),
        ("kit_id", "kit_instance"),
        ("assignment_id", "kit_assignment_event"),
    ),
    "seal_inspection_kit_verification": (
        ("command_id", "command_receipt"),
        ("actor_user_id", "user"),
        ("inspection_id", "seal_inspection"),
        ("kit_id", "kit_instance"),
        ("substitution_id", "seal_substitution"),
    ),
}


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def insert_guard(table, types, extra):
    return f"""CREATE TRIGGER cya_{table}_bi BEFORE INSERT ON `{table}`
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type IN ({types}))
THEN {signal("seal evidence receipt attribution mismatch")}; END IF;
{extra}
END"""


EVENT_GUARD = """IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id AND
 ((NEW.event_type='BREAK' AND r.command_type='SEAL_BREAK') OR
  (NEW.event_type IN ('RESEAL','AUDIT_FULFILLED') AND r.command_type='SEAL_RESEAL') OR
  (NEW.event_type='AUDIT_REQUEST' AND r.command_type='SEAL_AUDIT_REQUEST')))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal event command mismatch'; END IF;
IF NEW.kit_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM kit_instance k WHERE k.id=NEW.kit_id
 AND k.organization_id=NEW.organization_id AND k.vehicle_id=NEW.vehicle_id AND k.active=1)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal kit vehicle mismatch'; END IF;
IF NEW.kit_id IS NOT NULL AND NEW.event_type<>'BREAK' AND NOT EXISTS
 (SELECT 1 FROM kit_seal_designation d WHERE d.kit_id=NEW.kit_id
 AND d.organization_id=NEW.organization_id AND d.recorded_at<=NEW.recorded_at AND d.sealable=1
 AND NOT EXISTS (SELECT 1 FROM kit_seal_designation n WHERE n.kit_id=d.kit_id
 AND n.organization_id=d.organization_id AND n.sequence>d.sequence AND n.recorded_at<=NEW.recorded_at))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal kit not sealable'; END IF;
IF NEW.compartment_slot_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM compartment c
 WHERE c.organization_id=NEW.organization_id AND c.vehicle_id=NEW.vehicle_id
 AND c.stable_slot_id=NEW.compartment_slot_id AND c.effective_at<=NEW.recorded_at
 AND (c.sealable=1 OR NEW.event_type='BREAK') AND NOT EXISTS (SELECT 1 FROM compartment n
 WHERE n.organization_id=c.organization_id AND n.vehicle_id=c.vehicle_id
 AND n.stable_slot_id=c.stable_slot_id AND n.revision>c.revision
 AND n.effective_at<=NEW.recorded_at))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal compartment not sealable'; END IF;
IF NEW.prior_event_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM seal_event p WHERE p.id=NEW.prior_event_id
 AND p.organization_id=NEW.organization_id AND p.sequence=NEW.sequence-1
 AND (p.recorded_at < NEW.recorded_at OR (p.recorded_at=NEW.recorded_at
 AND p.command_id=NEW.command_id AND p.event_type='RESEAL' AND NEW.event_type='AUDIT_FULFILLED'))
 AND p.kit_id <=> NEW.kit_id
 AND (NEW.kit_id IS NOT NULL OR (p.vehicle_id=NEW.vehicle_id
 AND p.compartment_slot_id=NEW.compartment_slot_id)))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal event chain mismatch'; END IF;
IF NEW.audit_request_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM seal_event a WHERE a.id=NEW.audit_request_id
 AND a.organization_id=NEW.organization_id AND a.event_type='AUDIT_REQUEST'
 AND a.sequence < NEW.sequence AND a.kit_id <=> NEW.kit_id
 AND (NEW.kit_id IS NOT NULL OR (a.vehicle_id=NEW.vehicle_id
 AND a.compartment_slot_id=NEW.compartment_slot_id)))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal audit request mismatch'; END IF;"""

SUBSTITUTION_GUARD = """IF NOT EXISTS (SELECT 1 FROM seal_event e WHERE e.id=NEW.seal_event_id
 AND e.organization_id=NEW.organization_id AND e.event_type='RESEAL'
 AND e.recorded_at <= NEW.recorded_at AND BINARY e.marking=BINARY NEW.entered_marking)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal substitution requires reseal'; END IF;"""

LINK_GUARD = """IF NOT EXISTS (SELECT 1 FROM seal_substitution s JOIN obligation o
 ON o.organization_id=s.organization_id AND o.vehicle_id=s.vehicle_id JOIN seal_event e
 ON e.id=s.seal_event_id AND e.organization_id=s.organization_id
 WHERE s.id=NEW.substitution_id AND o.id=NEW.obligation_id
 AND (NEW.conditional_rule_id IS NULL OR EXISTS (SELECT 1 FROM conditional_rule r
  WHERE r.id=NEW.conditional_rule_id AND r.organization_id=NEW.organization_id
  AND r.rule_type='SEAL_SUBSTITUTION' AND r.effective_at<=NEW.recorded_at
  AND r.recorded_at<=NEW.recorded_at)) AND s.organization_id=NEW.organization_id
 AND (e.kit_id IS NULL OR o.kit_instance_id=e.kit_id)
 AND o.item_id=NEW.item_id AND o.kit_key=NEW.kit_key
 AND s.command_id=NEW.command_id AND s.recorded_at=NEW.recorded_at)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal substitution link mismatch'; END IF;
IF NEW.ordinal>0 AND NOT EXISTS (SELECT 1 FROM seal_substitution_obligation primary_link
 JOIN obligation primary_ob ON primary_ob.id=primary_link.obligation_id
 JOIN obligation target_ob ON target_ob.id=NEW.obligation_id
 JOIN shared_satisfaction_rule_member member ON member.rule_id=NEW.shared_rule_id
 AND member.organization_id=NEW.organization_id
 AND member.source_requirement_version_id=primary_ob.requirement_version_id
 AND member.additional_requirement_version_id=target_ob.requirement_version_id
 WHERE primary_link.substitution_id=NEW.substitution_id AND primary_link.item_id=NEW.item_id
 AND primary_link.kit_key=NEW.kit_key AND primary_link.ordinal=0
 AND primary_link.organization_id=NEW.organization_id)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal shared rule mismatch'; END IF;"""

INSPECTION_GUARD = """IF NOT EXISTS (SELECT 1 FROM seal_event e WHERE e.id=NEW.reseal_id
 AND e.organization_id=NEW.organization_id AND e.event_type='RESEAL'
 AND e.command_id=NEW.command_id AND e.recorded_at=NEW.recorded_at)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal inspection reseal mismatch'; END IF;
IF NEW.break_event_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM seal_event b JOIN seal_event e ON e.id=NEW.reseal_id
 WHERE b.id=NEW.break_event_id AND b.organization_id=NEW.organization_id
 AND b.event_type='BREAK' AND b.sequence < e.sequence AND b.kit_id <=> e.kit_id
 AND (e.kit_id IS NOT NULL OR (b.vehicle_id=e.vehicle_id
 AND b.compartment_slot_id=e.compartment_slot_id)))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal inspection break mismatch'; END IF;"""

ITEM_GUARD = """IF NOT EXISTS (SELECT 1 FROM seal_inspection i WHERE i.id=NEW.inspection_id
 AND i.organization_id=NEW.organization_id AND i.command_id=NEW.command_id
 AND i.recorded_at=NEW.recorded_at)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal inspection item receipt mismatch'; END IF;
IF NOT (NEW.instance_key <=> COALESCE(NEW.kit_id,'00000000-0000-0000-0000-000000000000'))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal inspection instance mismatch'; END IF;
IF (NEW.item_def_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM checklist_item_def d
 WHERE d.id=NEW.item_def_id AND d.organization_id=NEW.organization_id AND d.item_id=NEW.item_id))
 OR (NEW.kit_definition_item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM kit_definition_item d
 WHERE d.id=NEW.kit_definition_item_id AND d.organization_id=NEW.organization_id AND d.item_id=NEW.item_id))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal inspection item source mismatch'; END IF;"""

KIT_COUNT_GUARD = """IF NOT EXISTS (SELECT 1 FROM seal_inspection i JOIN seal_event e
 ON e.id=i.reseal_id AND e.organization_id=i.organization_id JOIN vehicle_kit_requirement r
 ON r.id=NEW.kit_requirement_id AND r.organization_id=i.organization_id JOIN compartment c
 ON c.id=r.compartment_id AND c.organization_id=i.organization_id
 WHERE i.id=NEW.inspection_id AND i.organization_id=NEW.organization_id
 AND i.command_id=NEW.command_id AND i.recorded_at=NEW.recorded_at
 AND e.kit_id IS NULL AND e.vehicle_id=c.vehicle_id AND e.compartment_slot_id=c.stable_slot_id)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal kit count scope mismatch'; END IF;"""

KIT_INSTANCE_GUARD = """IF NOT EXISTS (SELECT 1 FROM seal_inspection_kit_count c
 JOIN vehicle_kit_requirement r ON r.id=c.kit_requirement_id AND r.organization_id=c.organization_id
 JOIN seal_inspection i ON i.id=c.inspection_id AND i.organization_id=c.organization_id
 JOIN seal_event e ON e.id=i.reseal_id AND e.organization_id=i.organization_id
 JOIN kit_instance_definition d ON d.kit_id=NEW.kit_id AND d.organization_id=c.organization_id
 AND d.definition_id=r.definition_id
 JOIN kit_assignment_event a ON a.id=NEW.assignment_id AND a.organization_id=c.organization_id
 JOIN compartment p ON p.id=a.compartment_id AND p.organization_id=a.organization_id
 WHERE c.id=NEW.kit_count_id AND c.organization_id=NEW.organization_id
 AND c.command_id=NEW.command_id AND c.recorded_at=NEW.recorded_at
 AND a.kit_id=NEW.kit_id AND a.active=1 AND a.effective_at <= NEW.recorded_at
 AND a.vehicle_id=e.vehicle_id AND p.stable_slot_id=e.compartment_slot_id
 AND NOT EXISTS (SELECT 1 FROM kit_assignment_event newer WHERE newer.kit_id=a.kit_id
 AND newer.organization_id=a.organization_id AND newer.sequence>a.sequence
 AND newer.effective_at<=NEW.recorded_at))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal counted kit mismatch'; END IF;"""

KIT_VERIFICATION_GUARD = """IF NOT EXISTS (SELECT 1 FROM seal_inspection i
 JOIN seal_event cabinet ON cabinet.id=i.reseal_id AND cabinet.organization_id=i.organization_id
 JOIN seal_substitution s ON s.id=NEW.substitution_id AND s.organization_id=i.organization_id
 JOIN seal_event kit ON kit.id=s.seal_event_id AND kit.organization_id=s.organization_id
 WHERE i.id=NEW.inspection_id AND i.organization_id=NEW.organization_id
 AND i.command_id=NEW.command_id AND i.recorded_at=NEW.recorded_at
 AND cabinet.kit_id IS NULL AND kit.kit_id=NEW.kit_id AND s.vehicle_id=cabinet.vehicle_id
 AND s.recorded_at<=NEW.recorded_at
 AND (i.break_event_id IS NULL OR s.recorded_at >=
  (SELECT b.recorded_at FROM seal_event b WHERE b.id=i.break_event_id)))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'nested kit verification mismatch'; END IF;"""


def forward_statements():
    statements = []
    for table in TABLES:
        statements.extend(
            [
                f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_uuid` "
                f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
                f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_roles` "
                "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
                f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
            ]
        )
        for column, parent in FKS[table]:
            statements.append(
                f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_{column}_org_fk` "
                f"FOREIGN KEY (organization_id,{column}) REFERENCES `{parent}` (organization_id,id) "
                "ON DELETE RESTRICT ON UPDATE RESTRICT"
            )
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd")):
            statements.append(
                f"CREATE TRIGGER cya_{table}_{suffix} BEFORE {operation} ON `{table}` "
                f"FOR EACH ROW {signal('append-only table')}"
            )
    statements.extend(
        [
            insert_guard(
                "seal_event", "'SEAL_BREAK','SEAL_RESEAL','SEAL_AUDIT_REQUEST'", EVENT_GUARD
            ),
            insert_guard("seal_substitution", "'SEAL_SUBSTITUTE'", SUBSTITUTION_GUARD),
            insert_guard("seal_substitution_obligation", "'SEAL_SUBSTITUTE'", LINK_GUARD),
            insert_guard("seal_inspection", "'SEAL_RESEAL'", INSPECTION_GUARD),
            insert_guard("seal_inspection_item", "'SEAL_RESEAL'", ITEM_GUARD),
            insert_guard("seal_inspection_kit_count", "'SEAL_RESEAL'", KIT_COUNT_GUARD),
            insert_guard("seal_inspection_kit_instance", "'SEAL_RESEAL'", KIT_INSTANCE_GUARD),
            insert_guard(
                "seal_inspection_kit_verification", "'SEAL_RESEAL'", KIT_VERIFICATION_GUARD
            ),
        ]
    )
    return statements


def reverse_statements():
    statements = [f"DROP TRIGGER cya_{table}_bi" for table in reversed(TABLES)]
    for table in reversed(TABLES):
        statements.extend(f"DROP TRIGGER cya_{table}_{suffix}" for suffix in ("bd", "bu"))
        statements.extend(
            f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_{column}_org_fk`"
            for column, _ in reversed(FKS[table])
        )
        statements.extend(
            f"ALTER TABLE `{table}` DROP CONSTRAINT `cya_{table}_{name}`"
            for name in ("roles", "uuid")
        )
    return statements


class Migration(migrations.Migration):
    dependencies = [("cya_seals", "0001_initial")]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
