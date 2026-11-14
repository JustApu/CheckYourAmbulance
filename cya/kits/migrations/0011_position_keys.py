"""Organization-wide position keys for compartments and vehicle systems (SPEC 2.1.0 IMP-6).

position_key is append-only (A): one key per (kind, stable slot), set once and never changed or
removed. The schema owner adds organization-pair keys for the evidence columns, append-only
triggers, and an insert guard: the receipt is a POSITION_KEY_SET command of the same actor and
instant (or a VEHICLE_IMPORT, which keys the positions it creates), and the stable slot exists among the organization's compartments or vehicle systems.
"""

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers

ORG = "organization_id"
TABLE = "position_key"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
FKS = [("command_id", "command_receipt"), ("actor_user_id", "user")]
INSERT_GUARD = f"""CREATE TRIGGER cya_position_key_bi BEFORE INSERT ON position_key
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type IN ('POSITION_KEY_SET','VEHICLE_IMPORT'))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'position key receipt attribution mismatch'; END IF;
IF (NEW.kind='COMPARTMENT' AND NOT EXISTS (SELECT 1 FROM compartment c
  WHERE c.{ORG}=NEW.{ORG} AND c.stable_slot_id=NEW.stable_slot_id))
 OR (NEW.kind='SYSTEM' AND NOT EXISTS (SELECT 1 FROM vehicle_system s
  WHERE s.{ORG}=NEW.{ORG} AND s.stable_slot_id=NEW.stable_slot_id))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'position key names no position'; END IF;
END"""
FORWARD = [
    f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
    f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_roles` CHECK ("
    "JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
    f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
    *(
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_{column}_org_fk` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for column, parent in FKS
    ),
    *append_only_triggers(TABLE),
    INSERT_GUARD,
]
REVERSE = [
    "DROP TRIGGER cya_position_key_bi",
    *(f"DROP TRIGGER cya_{TABLE}_{suffix}" for suffix in ("bd", "bu")),
    *(f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_{column}_org_fk`" for column, _ in FKS),
    f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_roles`",
    f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`",
]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_kits", "0010_kit_presence_item_guards"),
    ]

    operations = [
        migrations.CreateModel(
            name="PositionKey",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                (
                    "id",
                    cya.accounts.fields.IdentityIDField(
                        db_collation="ascii_bin",
                        default=cya.accounts.fields.new_id,
                        editable=False,
                        max_length=36,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("kind", models.CharField(max_length=12)),
                (
                    "stable_slot_id",
                    cya.accounts.fields.IdentityIDField(db_collation="ascii_bin", max_length=36),
                ),
                ("key", models.CharField(db_collation="ascii_bin", max_length=100)),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
                (
                    "command",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_core.commandreceipt",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.organization",
                    ),
                ),
            ],
            options={
                "db_table": "position_key",
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="poskey_org_id"),
                    models.UniqueConstraint(
                        fields=("organization", "kind", "stable_slot_id"), name="poskey_slot"
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "kind", "key"), name="poskey_key"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("kind__in", ("COMPARTMENT", "SYSTEM"))),
                        name="poskey_kind",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("key__regex", "^[a-z0-9][a-z0-9._-]*$")),
                        name="poskey_format",
                    ),
                ],
            },
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
