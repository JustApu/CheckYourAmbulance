"""Schema-owner guards, composite tenant foreign keys, and append-only triggers for cya_conditions models."""

from django.db import migrations

from tools.audit_enforcement import append_only_triggers

UUID_REGEX = "^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

APPEND_ONLY_TABLES = [
    "roster_snapshot",
    "roster_member",
    "session_roster_correction",
    "obligation_determination",
]

ALL_TABLES = [
    "conditional_rule",
    "roster_snapshot",
    "roster_member",
    "session_roster_correction",
    "obligation_determination",
]

COMPOSITE_FKS = [
    (
        "cond_rule_cmd_org_fk",
        "conditional_rule",
        "organization_id, command_id",
        "command_receipt",
        "organization_id, id",
    ),
    (
        "cond_rule_actor_org_fk",
        "conditional_rule",
        "organization_id, actor_user_id",
        "user",
        "organization_id, id",
    ),
    (
        "roster_snap_cmd_org_fk",
        "roster_snapshot",
        "organization_id, command_id",
        "command_receipt",
        "organization_id, id",
    ),
    (
        "roster_snap_actor_org_fk",
        "roster_snapshot",
        "organization_id, actor_user_id",
        "user",
        "organization_id, id",
    ),
    (
        "roster_member_snap_org_fk",
        "roster_member",
        "organization_id, snapshot_id",
        "roster_snapshot",
        "organization_id, id",
    ),
    (
        "sess_corr_cmd_org_fk",
        "session_roster_correction",
        "organization_id, command_id",
        "command_receipt",
        "organization_id, id",
    ),
    (
        "sess_corr_actor_org_fk",
        "session_roster_correction",
        "organization_id, actor_user_id",
        "user",
        "organization_id, id",
    ),
    (
        "sess_corr_snap_org_fk",
        "session_roster_correction",
        "organization_id, snapshot_id",
        "roster_snapshot",
        "organization_id, id",
    ),
    (
        "sess_corr_resolves_org_fk",
        "session_roster_correction",
        "organization_id, resolves_correction_id",
        "session_roster_correction",
        "organization_id, id",
    ),
    (
        "ob_det_cmd_org_fk",
        "obligation_determination",
        "organization_id, command_id",
        "command_receipt",
        "organization_id, id",
    ),
    (
        "ob_det_actor_org_fk",
        "obligation_determination",
        "organization_id, actor_user_id",
        "user",
        "organization_id, id",
    ),
    (
        "ob_det_rule_org_fk",
        "obligation_determination",
        "organization_id, rule_id",
        "conditional_rule",
        "organization_id, id",
    ),
    (
        "ob_det_prior_org_fk",
        "obligation_determination",
        "organization_id, prior_determination_id",
        "obligation_determination",
        "organization_id, id",
    ),
]


def build_forward_sql():
    sql = []
    # 1. UUID checks
    for table in ALL_TABLES:
        sql.append(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `{table}_uuid` CHECK (BINARY `id` REGEXP '{UUID_REGEX}')"
        )

    # 2. Composite tenant foreign keys
    for name, table, child_cols, parent_table, parent_cols in COMPOSITE_FKS:
        sql.append(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` FOREIGN KEY ({child_cols}) "
            f"REFERENCES `{parent_table}` ({parent_cols}) ON DELETE RESTRICT ON UPDATE RESTRICT"
        )

    # 3. Append-only triggers for the 4 A tables
    for table in APPEND_ONLY_TABLES:
        sql.extend(append_only_triggers(table))

    # 4. Immutability for conditional_rule (Kind V)
    sql.append(
        "CREATE TRIGGER `cya_conditional_rule_bu` BEFORE UPDATE ON `conditional_rule` "
        "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'"
    )
    sql.append(
        "CREATE TRIGGER `cya_conditional_rule_bd` BEFORE DELETE ON `conditional_rule` "
        "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'"
    )

    return sql


def build_reverse_sql():
    sql = []
    for table in ALL_TABLES:
        sql.append(f"DROP TRIGGER IF EXISTS `cya_{table}_bu`")
        sql.append(f"DROP TRIGGER IF EXISTS `cya_{table}_bd`")
    for name, table, _, _, _ in COMPOSITE_FKS:
        sql.append(f"ALTER TABLE `{table}` DROP FOREIGN KEY IF EXISTS `{name}`")
    for table in ALL_TABLES:
        sql.append(f"ALTER TABLE `{table}` DROP CONSTRAINT IF EXISTS `{table}_uuid`")
    return sql


class Migration(migrations.Migration):
    dependencies = [
        ("cya_conditions", "0001_initial"),
        ("cya_core", "0004_audit_integrity"),
    ]

    operations = [
        migrations.RunSQL(
            sql=build_forward_sql(),
            reverse_sql=build_reverse_sql(),
        ),
    ]
