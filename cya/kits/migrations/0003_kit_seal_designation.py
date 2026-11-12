"""Append-only kit seal designation history (SPEC 10.1 SEAL-1, 10.2; issue #131).

kit_seal_designation records whether a kit instance is sealed, one row at a time per kit. As for
kit_assignment_event (0002), the schema owner adds: UUID and role checks, organization-pair
foreign keys, the append-only update/delete triggers the audit schema inspector requires on A
tables, and an insert trigger that repeats the command layer's rules, so a writer that bypasses
the command still cannot store a row that does not match its command receipt (organization,
actor, recorded instant, command type) or that is not the next row of its kit (consecutive
sequence, strictly later instant). Each trigger is one list element, so Django runs it whole; Django
would split a single SQL string at the semicolons inside a trigger body.
"""

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
TABLE = "kit_seal_designation"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
COMMAND_TYPE = "KIT_SEAL_DESIGNATE"

FKS = [
    ("command_id", "command_receipt", "id"),
    ("actor_user_id", "user", "id"),
    ("kit_id", "kit_instance", "id"),
    ("prior_designation_id", TABLE, "id"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


INSERT_GUARD = f"""CREATE TRIGGER cya_{TABLE}_bi BEFORE INSERT ON {TABLE}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='{COMMAND_TYPE}')
THEN {signal("kit seal designation receipt attribution mismatch")}; END IF;
IF NEW.prior_designation_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {TABLE} p
 WHERE p.id=NEW.prior_designation_id AND p.{ORG}=NEW.{ORG} AND p.kit_id=NEW.kit_id
 AND p.sequence=NEW.sequence-1 AND p.effective_at < NEW.effective_at)
THEN {signal("kit seal designation chain mismatch")}; END IF;
END"""


def forward_statements():
    statements = [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
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
    # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
    statements += [
        f"CREATE TRIGGER cya_{TABLE}_{suffix} BEFORE {operation} ON {TABLE} "
        f"FOR EACH ROW {signal('append-only table')}"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    ]
    return [*statements, INSERT_GUARD]


def reverse_statements():
    drops = [f"DROP TRIGGER cya_{TABLE}_bi"]
    drops += [f"DROP TRIGGER cya_{TABLE}_{suffix}" for suffix in ("bd", "bu")]
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
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0004_compartment_sealable"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_kits", "0002_kit_guards"),
    ]

    operations = [
        migrations.CreateModel(
            name="KitSealDesignation",
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
                ("sequence", models.PositiveBigIntegerField()),
                ("sealable", models.BooleanField()),
                ("effective_at", models.DateTimeField()),
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
                    "kit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.kitinstance",
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
                (
                    "prior_designation",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_kits.kitsealdesignation",
                    ),
                ),
            ],
            options={
                "db_table": "kit_seal_designation",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="kitsealdes_org_id"
                    ),
                    models.UniqueConstraint(fields=("kit", "sequence"), name="kitsealdes_sequence"),
                    models.CheckConstraint(
                        condition=models.Q(("sequence__gte", 1)), name="kitsealdes_seq_min"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("sealable__in", [True, False])),
                        name="kitsealdes_sealable_bool",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("prior_designation__isnull", False), ("sequence", 1), _connector="OR"
                        ),
                        name="kitsealdes_first",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("effective_at", models.F("recorded_at"))),
                        name="kitsealdes_effective_when_recorded",
                    ),
                ],
            },
        ),
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
    ]
