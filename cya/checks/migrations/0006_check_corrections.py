"""Append-only author correction chains and their audited decision event (issue 76 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/76))."""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN

APPEND = "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'"


def _check_insert():
    previous = import_module("cya.checks.migrations.0005_check_conflict_basis").CHECK_INSERT
    sql = previous.replace(
        "r.command_type='CHECK_RECORD'",
        "r.command_type=IF(NEW.predecessor_check_id IS NULL,'CHECK_RECORD','CHECK_CORRECTION')",
    )
    assert sql.endswith("END")
    rules = """IF NEW.predecessor_check_id IS NULL AND NEW.root_check_id<>NEW.id
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'first check must be its root'; END IF;
IF NEW.predecessor_check_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM check_record p
 JOIN check_record root ON root.id=NEW.root_check_id WHERE p.id=NEW.predecessor_check_id
 AND p.organization_id=NEW.organization_id AND p.root_check_id=NEW.root_check_id
 AND p.vehicle_id=NEW.vehicle_id AND p.item_id=NEW.item_id
 AND p.actor_user_id=NEW.actor_user_id AND root.actor_user_id=NEW.actor_user_id
 AND p.checklist_version_id=NEW.checklist_version_id AND p.item_def_id=NEW.item_def_id
 AND p.kit_id<=>NEW.kit_id AND p.session_id<=>NEW.session_id AND p.purpose=NEW.purpose)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check predecessor scope mismatch'; END IF;
"""
    return sql[:-3] + rules + "END"


def _link_trigger():
    trigger = _original_link_trigger()
    trigger = trigger.replace(
        "r.command_type='CHECK_RECORD'",
        "r.command_type IN ('CHECK_RECORD','CHECK_CORRECTION')",
    )
    root_time = "(SELECT root.recorded_at FROM check_record c JOIN check_record root ON root.id=c.root_check_id WHERE c.id=NEW.check_id)"
    for alias, column in (
        ("s", "recorded_at"),
        ("x", "recorded_at"),
        ("r", "recorded_at"),
        ("r", "effective_at"),
        ("x", "effective_at"),
    ):
        trigger = trigger.replace(
            f"{alias}.{column}<=NEW.recorded_at", f"{alias}.{column}<={root_time}"
        )
    # The active shared-rule lookup is at the root's original evidence instant as well.
    return trigger


def _original_link_trigger():
    old = import_module("cya.checks.migrations.0004_obligation_links")
    return next(
        statement
        for operation in old.Migration.operations
        if isinstance(operation, migrations.RunSQL)
        for statement in (operation.sql if isinstance(operation.sql, list) else [operation.sql])
        if isinstance(statement, str)
        and statement.startswith("CREATE TRIGGER cya_check_obligation_link_bi")
    )


def _reverse_guard(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT COUNT(*) FROM check_event) + "
            "(SELECT COUNT(*) FROM check_record WHERE predecessor_check_id IS NOT NULL)"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse check corrections while correction rows exist")


def _sql():
    event_bi = """CREATE TRIGGER cya_check_event_bi BEFORE INSERT ON check_event FOR EACH ROW BEGIN
IF NEW.event_type<>'CORRECTION' OR NEW.replacement_check_id IS NULL
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'unsupported check event'; END IF;
IF NOT EXISTS (SELECT 1 FROM check_record root JOIN check_record selected
 ON selected.id=NEW.selected_check_id JOIN check_record replacement
 ON replacement.id=NEW.replacement_check_id JOIN command_receipt r ON r.id=NEW.command_id
 WHERE root.id=NEW.root_check_id AND root.id=selected.root_check_id
 AND root.id=replacement.root_check_id AND replacement.predecessor_check_id=selected.id
 AND root.organization_id=NEW.organization_id AND selected.organization_id=NEW.organization_id
 AND replacement.organization_id=NEW.organization_id AND root.actor_user_id=NEW.actor_user_id
 AND selected.actor_user_id=NEW.actor_user_id AND replacement.actor_user_id=NEW.actor_user_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='CHECK_CORRECTION'
 AND replacement.command_id=NEW.command_id AND replacement.recorded_at=NEW.recorded_at)
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check event scope mismatch'; END IF;
END"""
    return [
        "ALTER TABLE `check_record` ADD CONSTRAINT `cya_check_record_predecessor_org_fk` FOREIGN KEY (organization_id,predecessor_check_id) REFERENCES `check_record` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "DROP TRIGGER cya_check_record_bi",
        _check_insert(),
        "ALTER TABLE `check_event` ADD CONSTRAINT `cya_check_event_uuid` CHECK (BINARY id REGEXP '"
        + UUID_PATTERN
        + "')",
        'ALTER TABLE `check_event` ADD CONSTRAINT `cya_check_event_roles` CHECK (JSON_TYPE(actor_roles_at_time)=\'ARRAY\' AND JSON_LENGTH(actor_roles_at_time)<=4 AND JSON_CONTAINS(\'["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\',actor_roles_at_time))',
        "ALTER TABLE `check_event` ADD CONSTRAINT `cya_check_event_receipt_fk` FOREIGN KEY (organization_id,command_id) REFERENCES `command_receipt` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "ALTER TABLE `check_event` ADD CONSTRAINT `cya_check_event_actor_fk` FOREIGN KEY (organization_id,actor_user_id) REFERENCES `user` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "ALTER TABLE `check_event` ADD CONSTRAINT `cya_check_event_root_fk` FOREIGN KEY (organization_id,root_check_id) REFERENCES `check_record` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "ALTER TABLE `check_event` ADD CONSTRAINT `cya_check_event_selected_fk` FOREIGN KEY (organization_id,selected_check_id) REFERENCES `check_record` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "ALTER TABLE `check_event` ADD CONSTRAINT `cya_check_event_replacement_fk` FOREIGN KEY (organization_id,replacement_check_id) REFERENCES `check_record` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "CREATE TRIGGER cya_check_event_bu BEFORE UPDATE ON check_event FOR EACH ROW " + APPEND,
        "CREATE TRIGGER cya_check_event_bd BEFORE DELETE ON check_event FOR EACH ROW " + APPEND,
        event_bi,
        "DROP TRIGGER cya_check_obligation_link_bi",
        _link_trigger(),
    ]


def _reverse_sql():
    return [
        "DROP TRIGGER cya_check_obligation_link_bi",
        _original_link_trigger(),
        "DROP TRIGGER cya_check_event_bi",
        "DROP TRIGGER cya_check_event_bu",
        "DROP TRIGGER cya_check_event_bd",
        "ALTER TABLE `check_event` DROP FOREIGN KEY `cya_check_event_replacement_fk`",
        "ALTER TABLE `check_event` DROP FOREIGN KEY `cya_check_event_selected_fk`",
        "ALTER TABLE `check_event` DROP FOREIGN KEY `cya_check_event_root_fk`",
        "ALTER TABLE `check_event` DROP FOREIGN KEY `cya_check_event_actor_fk`",
        "ALTER TABLE `check_event` DROP FOREIGN KEY `cya_check_event_receipt_fk`",
        "ALTER TABLE `check_event` DROP CONSTRAINT `cya_check_event_roles`",
        "ALTER TABLE `check_event` DROP CONSTRAINT `cya_check_event_uuid`",
        "DROP TRIGGER cya_check_record_bi",
        import_module("cya.checks.migrations.0005_check_conflict_basis").CHECK_INSERT,
        "ALTER TABLE `check_record` DROP FOREIGN KEY `cya_check_record_predecessor_org_fk`",
        # As in 0005, a dumped FK index must go before Django drops its reference column.
        "DROP INDEX IF EXISTS `cya_check_record_predecessor_org_fk` ON `check_record`",
    ]


class Migration(migrations.Migration):
    dependencies = [("cya_checks", "0005_check_conflict_basis")]
    operations = [
        migrations.RemoveConstraint(model_name="checkrecord", name="check_first_is_root"),
        migrations.AddField(
            model_name="checkrecord",
            name="predecessor_check",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replacement_check",
                to="cya_checks.checkrecord",
            ),
        ),
        migrations.AddConstraint(
            model_name="checkrecord",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(predecessor_check__isnull=True, root_check=models.F("id"))
                    | models.Q(predecessor_check__isnull=False)
                ),
                name="check_root_shape",
            ),
        ),
        migrations.CreateModel(
            name="CheckEvent",
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
                ("event_type", models.CharField(max_length=16)),
                ("explanation", models.CharField(max_length=4000, null=True)),
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
                (
                    "root_check",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checks.checkrecord",
                    ),
                ),
                (
                    "selected_check",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checks.checkrecord",
                    ),
                ),
                (
                    "replacement_check",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checks.checkrecord",
                    ),
                ),
            ],
            options={
                "db_table": "check_event",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="check_event_org_id"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(explanation__isnull=True)
                        | models.Q(explanation__regex=r"\S"),
                        name="check_event_explanation",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(event_type__in=("CORRECTION", "INVALIDATION")),
                        name="check_event_type",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(event_type="CORRECTION", replacement_check__isnull=False)
                            | models.Q(event_type="INVALIDATION", replacement_check__isnull=True)
                        ),
                        name="check_event_replacement_shape",
                    ),
                    models.UniqueConstraint(
                        fields=("replacement_check",), name="check_event_replacement_once"
                    ),
                ],
            },
        ),
        migrations.RunSQL(_sql(), reverse_sql=_reverse_sql()),
        migrations.RunPython(migrations.RunPython.noop, _reverse_guard),
    ]
