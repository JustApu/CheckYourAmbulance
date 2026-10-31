"""Frozen tenant, UUID and payload guards; schema owner only.

Catalogue is a migration-owned baseline, not placeholder tables. Future feature
migrations must install their own guards/grants and update the live manifest.
"""

from django.db import migrations

TABLE_CLASSES = {
    "A": (
        "attachment",
        "audit_event",
        "check_event",
        "check_obligation_link",
        "check_record",
        "check_suspension",
        "checklist_publication",
        "command_receipt",
        "deficiency_affected_item",
        "deficiency_event",
        "deficiency_evidence",
        "deficiency_link",
        "determination_check",
        "engine_hours_entry",
        "kit_inspection",
        "kit_inspection_item",
        "kit_seal_event",
        "notification_delivery_event",
        "notification_outbox",
        "obligation",
        "obligation_determination",
        "obligation_selection",
        "odometer_entry",
        "odometer_obligation",
        "odometer_obligation_link",
        "reading_event",
        "requirement_version",
        "role_grant",
        "roster_member",
        "roster_snapshot",
        "seal_substitution",
        "seal_substitution_obligation",
        "session_event",
        "session_participant",
        "session_roster_correction",
        "status_request",
        "vehicle_status_event",
        "verification_scope_row",
    ),
    "V": (
        "checklist_item_def",
        "checklist_version",
        "compartment",
        "conditional_rule",
        "deficiency_category",
        "na_rule",
        "schedule_rule",
        "shared_satisfaction_rule",
        "stocking_deviation",
        "vehicle_override_item",
        "vehicle_override_version",
        "vehicle_system",
    ),
    "M": (
        "checklist_template",
        "deficiency",
        "deficiency_lock",
        "deficiency_projection",
        "inspection_session",
        "item",
        "item_lock",
        "kit_instance",
        "kit_lock",
        "notification_pref",
        "organization",
        "session_projection",
        "user",
        "user_preference",
        "vehicle",
        "vehicle_lock",
    ),
}

UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
CODE = "^[A-Z][A-Z0-9_]*$"


def code_check(column):
    # PCRE '$' can match before a terminal newline; forbid every non-code byte.
    return f"BINARY {column} REGEXP '{CODE}' AND BINARY {column} NOT REGEXP '[^A-Z0-9_]'"


CHECKS = {
    "command_receipt": {
        "receipt_uuid": " AND ".join(
            f"BINARY {c} REGEXP '{UUID}'" for c in ("id", "operation_id", "correlation_id")
        ),
        "receipt_version": "(actor_user_id IS NOT NULL AND SUBSTRING(operation_id,15,1) = '4') OR (actor_user_id IS NULL AND SUBSTRING(operation_id,15,1) = '5' AND CHAR_LENGTH(TRIM(service_identity)) > 0 AND CHAR_LENGTH(TRIM(job_key)) > 0)",
        "receipt_values": f"{code_check('command_type')} AND BINARY request_digest REGEXP '^[0-9a-f]{{64}}$' AND result_schema_version = 1 AND response_status BETWEEN 200 AND 299 AND JSON_TYPE(response_body) = 'OBJECT' AND OCTET_LENGTH(response_body) <= 262144",
    },
    "audit_event": {
        "audit_uuid": " AND ".join(
            f"BINARY {c} REGEXP '{UUID}'" for c in ("id", "request_attempt_id", "correlation_id")
        ),
        "audit_values": f"{code_check('action')} AND BINARY outcome IN ('SUCCEEDED','DENIED','FAILED','OUTCOME_UNKNOWN') AND (BINARY outcome = 'SUCCEEDED' OR reason_code IS NOT NULL) AND (reason_code IS NULL OR ({code_check('reason_code')}))",
        "audit_target": f"(target_type IS NULL AND target_id IS NULL) OR (target_type IS NOT NULL AND target_id IS NOT NULL AND {code_check('target_type')} AND BINARY target_type IN ('USER','ORGANIZATION','COMMAND_RECEIPT') AND BINARY target_id REGEXP '{UUID}')",
        "audit_roles": 'JSON_TYPE(roles_at_time) = \'ARRAY\' AND JSON_LENGTH(roles_at_time) <= 4 AND JSON_CONTAINS(\'["EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\', roles_at_time) AND (actor_user_id IS NOT NULL OR JSON_LENGTH(roles_at_time) = 0)',
        "audit_service": "(service_identity IS NULL AND job_key IS NULL) OR (actor_user_id IS NULL AND service_identity IS NOT NULL AND job_key IS NOT NULL AND CHAR_LENGTH(TRIM(service_identity)) > 0 AND CHAR_LENGTH(TRIM(job_key)) > 0)",
    },
}
for column in ("prior_snapshot", "new_snapshot"):
    CHECKS["audit_event"]["audit_" + column] = (
        f"{column} IS NULL OR (JSON_TYPE({column}) = 'OBJECT' AND OCTET_LENGTH({column}) <= 262144 AND JSON_LENGTH({column}) = 2 AND JSON_CONTAINS_PATH({column}, 'all', '$.schema_version', '$.values') AND JSON_TYPE(JSON_EXTRACT({column}, '$.schema_version')) = 'INTEGER' AND JSON_EXTRACT({column}, '$.schema_version') = 1 AND JSON_TYPE(JSON_EXTRACT({column}, '$.values')) = 'OBJECT')"
    )

FKS = [
    (
        "command_receipt",
        "receipt_actor_org_fk",
        "organization_id, actor_user_id",
        "user",
        "organization_id, id",
    ),
    (
        "audit_event",
        "audit_actor_org_fk",
        "organization_id, actor_user_id",
        "user",
        "organization_id, id",
    ),
    (
        "audit_event",
        "audit_command_org_fk",
        "organization_id, command_id",
        "command_receipt",
        "organization_id, id",
    ),
]
GUARD = """CREATE TRIGGER cya_audit_attribution_bi BEFORE INSERT ON audit_event
FOR EACH ROW BEGIN
IF NEW.command_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM command_receipt r WHERE r.id = NEW.command_id AND
 (NOT (r.actor_user_id <=> NEW.actor_user_id) OR
  NOT (BINARY r.service_identity <=> BINARY NEW.service_identity) OR
  NOT (BINARY r.job_key <=> BINARY NEW.job_key) OR
  NOT (r.correlation_id <=> NEW.correlation_id))) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit attribution mismatch';
END IF;
IF (NEW.target_type = 'USER' AND NOT EXISTS (
 SELECT 1 FROM `user` u WHERE u.id=NEW.target_id AND u.organization_id=NEW.organization_id))
 OR (NEW.target_type = 'ORGANIZATION' AND NEW.target_id <> NEW.organization_id)
 OR (NEW.target_type = 'COMMAND_RECEIPT' AND NOT EXISTS (
 SELECT 1 FROM command_receipt r WHERE r.id=NEW.target_id AND r.organization_id=NEW.organization_id)) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit target scope mismatch';
END IF; END"""


class Migration(migrations.Migration):
    dependencies = [("cya_core", "0003_auditevent_job_key_auditevent_service_identity")]
    operations = [
        migrations.RunSQL(
            [
                (f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` CHECK ({condition})")
                for table, checks in CHECKS.items()
                for name, condition in checks.items()
            ]
            + [
                f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` FOREIGN KEY ({columns}) REFERENCES `{parent}` ({keys}) ON DELETE RESTRICT ON UPDATE RESTRICT"
                for table, name, columns, parent, keys in FKS
            ]
            + [GUARD],
            ["DROP TRIGGER cya_audit_attribution_bi"]
            + [
                f"ALTER TABLE `{table}` DROP FOREIGN KEY `{name}`"
                for table, name, *_ in reversed(FKS)
            ]
            + [
                f"ALTER TABLE `{table}` DROP CONSTRAINT `{name}`"
                for table, checks in reversed(list(CHECKS.items()))
                for name in reversed(checks)
            ],
        ),
    ]
