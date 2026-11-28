"""Paper-entry batch invalidation (SPEC 8.0.0 INV-8, LATE-6, LATE-7, §18.4; issue #9 Part B5).

paper_entry_invalidation_event is append-only (A), one row per batch at most. As for the other
paper-entry tables (0002), the schema owner adds UUID and role checks, organization-pair foreign
keys, the append-only update/delete triggers and an insert trigger that repeats the command's
rules, so a writer that bypasses cya.paper.invalidation still cannot store an invalidation:

- that does not match its PAPER_ENTRY_BATCH_INVALIDATION receipt (organization, actor, recorded
  instant, command type, and the batch the receipt's response body names);
- whose actor's recorded roles include neither Supervisor nor Administrator (§3.3);
- by the batch's own entry actor (INV-8: another Supervisor or Administrator must do it);
- while any check, odometer or engine-hours chain of the batch is not invalidated. The command
  appends those chain events first in the same transaction, so the batch and its linked records
  are invalidated as a unit.

Each trigger is one list element, so Django runs it whole. The reverse refusal is the last
operation, so on unapply it runs first, before the table is dropped.
"""

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN

ORG = "organization_id"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
COMMAND_TYPE = "PAPER_ENTRY_BATCH_INVALIDATION"
TABLE = "paper_entry_invalidation_event"
FKS = [
    ("command_id", "command_receipt", "id"),
    ("actor_user_id", "user", "id"),
    ("batch_id", "paper_entry_batch", "id"),
]


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def _open_chain(table, alias, event_column):
    """A root reading of the batch with no INVALIDATION reading_event."""
    return f"""EXISTS (SELECT 1 FROM {table} {alias} WHERE {alias}.{ORG}=NEW.{ORG}
  AND {alias}.paper_entry_batch_id=NEW.batch_id AND {alias}.root_entry_id={alias}.id
  AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.{event_column}={alias}.id
   AND x.event_type='INVALIDATION'))"""


GUARD = f"""CREATE TRIGGER cya_{TABLE}_bi BEFORE INSERT ON {TABLE}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='{COMMAND_TYPE}'
 AND JSON_VALUE(r.response_body,'$.batch_id')=NEW.batch_id)
THEN {signal("paper entry invalidation receipt attribution mismatch")}; END IF;
IF NOT (JSON_CONTAINS(NEW.actor_roles_at_time, '"SUPERVISOR"')
 OR JSON_CONTAINS(NEW.actor_roles_at_time, '"ADMINISTRATOR"'))
THEN {signal("paper entry invalidation needs a supervisor or administrator")}; END IF;
IF EXISTS (SELECT 1 FROM paper_entry_batch b WHERE b.id=NEW.batch_id
 AND b.actor_user_id=NEW.actor_user_id)
THEN {signal("a paper entry batch cannot be invalidated by its entry actor")}; END IF;
IF EXISTS (SELECT 1 FROM check_record c WHERE c.{ORG}=NEW.{ORG}
  AND c.paper_entry_batch_id=NEW.batch_id AND c.root_check_id=c.id
  AND NOT EXISTS (SELECT 1 FROM check_event e WHERE e.root_check_id=c.id
   AND e.event_type='INVALIDATION'))
 OR {_open_chain("odometer_entry", "o", "root_odometer_id")}
 OR {_open_chain("engine_hours_entry", "h", "root_hours_id")}
THEN {signal("a record chain of the paper entry batch is not invalidated")}; END IF;
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
    return [*statements, GUARD]


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


def refuse_reverse_with_invalidations(apps, schema_editor):
    if apps.get_model("cya_paper", "PaperEntryInvalidationEvent").objects.exists():
        raise RuntimeError("cannot reverse paper batch invalidation while invalidations exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_paper", "0002_paper_entry_guards"),
        # The guard reads the paper columns of checks, readings and their events.
        ("cya_checks", "0022_paper_checks"),
        ("cya_odometer", "0005_paper_readings"),
        ("cya_seals", "0010_paper_seal_records"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaperEntryInvalidationEvent",
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
                ("explanation", models.CharField(max_length=4000)),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
                (
                    "batch",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_paper.paperentrybatch",
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
                "db_table": TABLE,
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="paperinvalid_org_id"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("explanation__regex", "\\S")),
                        name="paperinvalid_explanation",
                    ),
                ],
            },
        ),
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_invalidations),
    ]
