"""Age-highlight rule revisions (issue 150 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/150); SPEC DEF-6, AUD-4).

deficiency_age_rule is append-only (contracts section 2). The guards repeat what the command
layer enforces, so a writer that bypasses it still cannot store a row that: does not match its
command receipt; skips or repeats a revision number; or carries a threshold list that is not an
array of at most five strictly ascending whole numbers of days. Triggers are BEGIN..END blocks in one-item lists: Django would split
a plain string at the semicolons inside a trigger body.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN

guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
signal = guards.signal
ORG = "organization_id"
MAX_AGE_DAYS = 3650  # the command's bound, fixed here so the migration never follows the model
TABLE = "deficiency_age_rule"


def _threshold_test():
    """True when any of the (at most five) thresholds is not a whole number of days in range or
    does not exceed the one before it: the command's own rule, repeated for a direct writer.

    JSON_EXTRACT returns text, so the order and range tests compare the cast numbers, never the
    strings ('30' sorts before '7'); the type test comes first for anything that is not an integer.
    """
    array = "NEW.age_thresholds_days"
    tests = []
    for index in range(5):
        item = f"JSON_EXTRACT({array},'$[{index}]')"
        number = f"CAST({item} AS SIGNED)"
        bad = f"JSON_TYPE({item})<>'INTEGER' OR {number}<1 OR {number}>{MAX_AGE_DAYS}"
        if index:
            bad += f" OR {number}<=CAST(JSON_EXTRACT({array},'$[{index - 1}]') AS SIGNED)"
        tests.append(f"({item} IS NOT NULL AND ({bad}))")
    return "(" + " OR ".join(tests) + ")"


THRESHOLD_TEST = _threshold_test()

INSERT = f"""CREATE TRIGGER cya_{TABLE}_bi BEFORE INSERT ON {TABLE}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.operation_id=NEW.operation_id
 AND r.command_type='DEFICIENCY_AGE_RULE_SET')
THEN {signal("age rule receipt attribution mismatch")}; END IF;
IF {THRESHOLD_TEST}
THEN {signal("age thresholds must be ascending whole numbers of days")}; END IF;
IF NEW.revision <> IFNULL((SELECT MAX(t.revision) FROM {TABLE} t WHERE t.{ORG}=NEW.{ORG}), 0) + 1
THEN {signal("age rule revision is not the next one")}; END IF;
END"""


def forward_statements():
    return [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` CHECK "
        f"(BINARY id REGEXP '{UUID_PATTERN}' AND BINARY operation_id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{guards.ROLE_CODES}', actor_roles_at_time))",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_thresholds` "
        "CHECK (JSON_TYPE(age_thresholds_days)='ARRAY' AND JSON_LENGTH(age_thresholds_days)<=5)",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_command_id_org_fk` "
        f"FOREIGN KEY ({ORG},command_id) REFERENCES `command_receipt` ({ORG},id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_actor_user_id_org_fk` "
        f"FOREIGN KEY ({ORG},actor_user_id) REFERENCES `user` ({ORG},id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        # Same statement the audit schema inspector (cya.core.audit_schema) requires on A tables.
        *(
            f"CREATE TRIGGER cya_{TABLE}_{suffix} BEFORE {operation} ON {TABLE} "
            f"FOR EACH ROW {signal('append-only table')}"
            for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
        ),
        INSERT,
    ]


def reverse_statements():
    return [
        f"DROP TRIGGER cya_{TABLE}_bi",
        f"DROP TRIGGER cya_{TABLE}_bd",
        f"DROP TRIGGER cya_{TABLE}_bu",
        f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_actor_user_id_org_fk`",
        f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_command_id_org_fk`",
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_thresholds`",
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_roles`",
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`",
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0008_deadline_extensions"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeficiencyAgeRule",
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
                (
                    "operation_id",
                    cya.accounts.fields.IdentityIDField(db_collation="ascii_bin", max_length=36),
                ),
                ("revision", models.PositiveIntegerField()),
                ("age_thresholds_days", models.JSONField(default=list)),
                ("long_running_age_days", models.PositiveIntegerField(null=True)),
                ("long_running_extensions", models.PositiveIntegerField(null=True)),
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
                "db_table": "deficiency_age_rule",
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="defage_org_id"),
                    models.UniqueConstraint(
                        fields=("organization", "operation_id"), name="defage_org_operation"
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "revision"), name="defage_revision"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("revision__gte", 1)), name="defage_revision_min"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("long_running_age_days__isnull", True),
                            models.Q(
                                ("long_running_age_days__gte", 1),
                                ("long_running_age_days__lte", 3650),
                            ),
                            _connector="OR",
                        ),
                        name="defage_long_age",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("long_running_extensions__isnull", True),
                            ("long_running_extensions__gte", 1),
                            _connector="OR",
                        ),
                        name="defage_long_extensions",
                    ),
                ],
            },
        ),
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
    ]
