"""Append-only reading corrections, chain invalidation, and effective odometer links."""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN

prior = import_module("cya.odometer.migrations.0002_odometer_guards")
APPEND = "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'"
ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def reading_insert(table, threshold):
    """The prior is the latest effective tip; a correction has the same author and root."""
    root_event = "root_odometer_id" if table == "odometer_entry" else "root_hours_id"
    limit = (
        f"""SET max_up = IFNULL((SELECT t.max_increase FROM odometer_threshold t
 WHERE t.organization_id=NEW.organization_id AND t.unit=NEW.unit
 ORDER BY t.revision DESC LIMIT 1), {prior.DEFAULTS});
SET anomalous = anomalous OR (NEW.value - prior_value > max_up);"""
        if threshold
        else ""
    )
    return f"""CREATE TRIGGER cya_{table}_bi BEFORE INSERT ON {table} FOR EACH ROW BEGIN
DECLARE prior_value DECIMAL(10,1);
DECLARE prior_unit VARCHAR(2);
DECLARE max_up DECIMAL(10,1);
DECLARE expected_prior VARCHAR(36);
DECLARE correction_root_recorded_at DATETIME(6);
DECLARE anomalous BOOLEAN DEFAULT 0;
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.operation_id=NEW.operation_id
 AND r.command_type=IF(NEW.predecessor_entry_id IS NULL,'ODOMETER_RECORD','READING_CORRECTION'))
THEN {signal("reading receipt attribution mismatch")}; END IF;
IF NEW.predecessor_entry_id IS NOT NULL THEN
 SELECT root.recorded_at INTO correction_root_recorded_at FROM {table} root
  WHERE root.id=NEW.root_entry_id AND root.root_entry_id=root.id;
END IF;
SET expected_prior = (SELECT e.id FROM {table} e JOIN {table} candidate_root
 ON candidate_root.id=e.root_entry_id WHERE e.organization_id=NEW.organization_id
 AND e.vehicle_id=NEW.vehicle_id
 AND (NEW.predecessor_entry_id IS NULL OR candidate_root.recorded_at<correction_root_recorded_at
  OR (candidate_root.recorded_at=correction_root_recorded_at
   AND candidate_root.id<NEW.root_entry_id))
 AND NOT EXISTS (SELECT 1 FROM {table} c WHERE c.predecessor_entry_id=e.id)
 AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.{root_event}=e.root_entry_id
  AND x.event_type='INVALIDATION')
 ORDER BY candidate_root.recorded_at DESC,candidate_root.id DESC LIMIT 1);
IF NOT (NEW.prior_entry_id <=> expected_prior)
THEN {signal("previous reading is not the latest valid reading")}; END IF;
IF NEW.predecessor_entry_id IS NULL THEN
 IF NEW.root_entry_id<>NEW.id THEN {signal("first reading must be root")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM {table} p JOIN {table} root ON root.id=NEW.root_entry_id
  WHERE p.id=NEW.predecessor_entry_id AND p.root_entry_id=root.id
  AND p.organization_id=NEW.organization_id AND root.organization_id=NEW.organization_id
  AND p.vehicle_id=NEW.vehicle_id AND p.actor_user_id=NEW.actor_user_id
  AND root.actor_user_id=NEW.actor_user_id AND p.unit=NEW.unit AND root.unit=NEW.unit
  AND NOT EXISTS (SELECT 1 FROM {table} c WHERE c.predecessor_entry_id=p.id)
  AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.{root_event}=root.id
   AND x.event_type='INVALIDATION'))
 THEN {signal("reading predecessor scope mismatch")}; END IF;
END IF;
IF NEW.prior_entry_id IS NOT NULL THEN
 SELECT e.value,e.unit INTO prior_value,prior_unit FROM {table} e WHERE e.id=NEW.prior_entry_id;
 IF prior_unit<>NEW.unit THEN {signal("reading unit differs from prior")}; END IF;
 SET anomalous = NEW.value < prior_value;
 {limit}
END IF;
IF anomalous AND NOT NEW.anomaly_confirmed
THEN {signal("anomalous reading needs confirmation")}; END IF;
IF NEW.anomaly_confirmed AND NOT anomalous
THEN {signal("confirmation without an anomaly")}; END IF;
END"""


def event_insert():
    kinds = []
    for name, table in (("odometer", "odometer_entry"), ("hours", "engine_hours_entry")):
        kinds.append(f"""(NEW.root_{name}_id IS NOT NULL AND NEW.selected_{name}_id IS NOT NULL
 AND NEW.root_{"hours" if name == "odometer" else "odometer"}_id IS NULL
 AND EXISTS (SELECT 1 FROM {table} root JOIN {table} selected
  ON selected.id=NEW.selected_{name}_id AND selected.root_entry_id=root.id
  WHERE root.id=NEW.root_{name}_id AND root.organization_id=NEW.organization_id
  AND selected.organization_id=NEW.organization_id
  AND ((NEW.event_type='CORRECTION' AND EXISTS (SELECT 1 FROM {table} replacement
    WHERE replacement.id=NEW.replacement_{name}_id
    AND replacement.root_entry_id=root.id AND replacement.predecessor_entry_id=selected.id
    AND replacement.command_id=NEW.command_id AND replacement.recorded_at=NEW.recorded_at
    AND replacement.actor_user_id=NEW.actor_user_id))
   OR (NEW.event_type='INVALIDATION' AND NEW.replacement_{name}_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM {table} c WHERE c.predecessor_entry_id=selected.id)))))""")
    return f"""CREATE TRIGGER cya_reading_event_bi BEFORE INSERT ON reading_event FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at
 AND r.command_type=IF(NEW.event_type='CORRECTION','READING_CORRECTION','READING_INVALIDATION'))
THEN {signal("reading event receipt attribution mismatch")}; END IF;
IF NOT ({" OR ".join(kinds)})
THEN {signal("reading event chain mismatch")}; END IF;
IF NEW.event_type='INVALIDATION' AND
 (NEW.explanation IS NULL OR NOT JSON_CONTAINS(NEW.actor_roles_at_time,'"ADMINISTRATOR"'))
THEN {signal("invalid reading invalidation")}; END IF;
IF EXISTS (SELECT 1 FROM reading_event e WHERE e.event_type='INVALIDATION'
 AND (e.root_odometer_id=NEW.root_odometer_id OR e.root_hours_id=NEW.root_hours_id))
THEN {signal("reading chain already invalidated")}; END IF;
END"""


def link_insert():
    existing = prior.LINK_INSERT
    assert existing.endswith("END")
    guard = f"""IF EXISTS (SELECT 1 FROM odometer_obligation_link l JOIN odometer_entry e
 ON e.id=l.entry_id WHERE l.obligation_id=NEW.obligation_id
 AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.root_odometer_id=e.root_entry_id
  AND x.event_type='INVALIDATION'))
THEN {signal("odometer obligation already has valid evidence")}; END IF;
"""
    return existing[:-3] + guard + "END"


def forward_sql():
    statements = []
    for table in ("odometer_entry", "engine_hours_entry"):
        statements += [
            f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_predecessor_org_fk` "
            f"FOREIGN KEY (organization_id,predecessor_entry_id) REFERENCES `{table}` "
            "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
            f"DROP TRIGGER cya_{table}_bi",
            reading_insert(table, table == "odometer_entry"),
        ]
    for column, table in (
        ("root_odometer_id", "odometer_entry"),
        ("selected_odometer_id", "odometer_entry"),
        ("replacement_odometer_id", "odometer_entry"),
        ("root_hours_id", "engine_hours_entry"),
        ("selected_hours_id", "engine_hours_entry"),
        ("replacement_hours_id", "engine_hours_entry"),
    ):
        statements.append(
            f"ALTER TABLE `reading_event` ADD CONSTRAINT `cya_reading_event_{column}_org_fk` "
            f"FOREIGN KEY (organization_id,{column}) REFERENCES `{table}` "
            "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT"
        )
    for column, table in (("command_id", "command_receipt"), ("actor_user_id", "user")):
        statements.append(
            f"ALTER TABLE `reading_event` ADD CONSTRAINT `cya_reading_event_{column}_org_fk` "
            f"FOREIGN KEY (organization_id,{column}) REFERENCES `{table}` "
            "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT"
        )
    statements += [
        f"ALTER TABLE `reading_event` ADD CONSTRAINT `cya_reading_event_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `reading_event` ADD CONSTRAINT `cya_reading_event_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}',actor_roles_at_time))",
        f"CREATE TRIGGER cya_reading_event_bu BEFORE UPDATE ON reading_event FOR EACH ROW {APPEND}",
        f"CREATE TRIGGER cya_reading_event_bd BEFORE DELETE ON reading_event FOR EACH ROW {APPEND}",
        event_insert(),
        "DROP TRIGGER cya_odometer_obligation_link_bi",
        link_insert(),
    ]
    return statements


def reverse_sql():
    statements = [
        "DROP TRIGGER cya_odometer_obligation_link_bi",
        prior.LINK_INSERT,
        "DROP TRIGGER cya_reading_event_bi",
        "DROP TRIGGER cya_reading_event_bu",
        "DROP TRIGGER cya_reading_event_bd",
        "ALTER TABLE `reading_event` DROP CONSTRAINT `cya_reading_event_roles`",
        "ALTER TABLE `reading_event` DROP CONSTRAINT `cya_reading_event_uuid`",
    ]
    for column in (
        "actor_user_id",
        "command_id",
        "replacement_hours_id",
        "selected_hours_id",
        "root_hours_id",
        "replacement_odometer_id",
        "selected_odometer_id",
        "root_odometer_id",
    ):
        statements.append(
            f"ALTER TABLE `reading_event` DROP FOREIGN KEY `cya_reading_event_{column}_org_fk`"
        )
    for table in ("engine_hours_entry", "odometer_entry"):
        statements += [
            f"DROP TRIGGER cya_{table}_bi",
            prior.reading_trigger(table, "ODOMETER_RECORD", table == "odometer_entry"),
            f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_predecessor_org_fk`",
        ]
    return statements


def guard_reverse(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT COUNT(*) FROM reading_event) + "
            "(SELECT COUNT(*) FROM odometer_entry WHERE predecessor_entry_id IS NOT NULL) + "
            "(SELECT COUNT(*) FROM engine_hours_entry WHERE predecessor_entry_id IS NOT NULL) + "
            "IFNULL((SELECT COUNT(*) FROM odometer_obligation_link GROUP BY obligation_id "
            "HAVING COUNT(*)>1 LIMIT 1),0)"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse reading decisions while decision rows exist")


class Migration(migrations.Migration):
    dependencies = [("cya_odometer", "0002_odometer_guards")]
    operations = [
        migrations.RemoveConstraint(
            model_name="odometerentry", name="odometer_entry_first_is_root"
        ),
        migrations.RemoveConstraint(
            model_name="enginehoursentry", name="engine_hours_first_is_root"
        ),
        migrations.AddField(
            model_name="odometerentry",
            name="predecessor_entry",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replacement_entry",
                to="cya_odometer.odometerentry",
            ),
        ),
        migrations.AddField(
            model_name="enginehoursentry",
            name="predecessor_entry",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replacement_entry",
                to="cya_odometer.enginehoursentry",
            ),
        ),
        migrations.AddConstraint(
            model_name="odometerentry",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(predecessor_entry__isnull=True, root_entry=models.F("id"))
                    | models.Q(predecessor_entry__isnull=False)
                ),
                name="odometer_entry_root_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="enginehoursentry",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(predecessor_entry__isnull=True, root_entry=models.F("id"))
                    | models.Q(predecessor_entry__isnull=False)
                ),
                name="engine_hours_root_shape",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="odometerobligationlink", name="odometer_link_obligation"
        ),
        migrations.AddConstraint(
            model_name="odometerobligationlink",
            constraint=models.UniqueConstraint(
                fields=("obligation", "entry"), name="odometer_link_obligation_entry"
            ),
        ),
        migrations.CreateModel(
            name="ReadingEvent",
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
                    "root_odometer",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_odometer.odometerentry",
                    ),
                ),
                (
                    "selected_odometer",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_odometer.odometerentry",
                    ),
                ),
                (
                    "replacement_odometer",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_odometer.odometerentry",
                    ),
                ),
                (
                    "root_hours",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_odometer.enginehoursentry",
                    ),
                ),
                (
                    "selected_hours",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_odometer.enginehoursentry",
                    ),
                ),
                (
                    "replacement_hours",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_odometer.enginehoursentry",
                    ),
                ),
            ],
            options={
                "db_table": "reading_event",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="reading_event_org_id"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(event_type__in=("CORRECTION", "INVALIDATION")),
                        name="reading_event_type",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                root_odometer__isnull=False,
                                selected_odometer__isnull=False,
                                root_hours__isnull=True,
                                selected_hours__isnull=True,
                                replacement_hours__isnull=True,
                            )
                            | models.Q(
                                root_hours__isnull=False,
                                selected_hours__isnull=False,
                                root_odometer__isnull=True,
                                selected_odometer__isnull=True,
                                replacement_odometer__isnull=True,
                            )
                        ),
                        name="reading_event_kind",
                    ),
                    models.CheckConstraint(
                        condition=(
                            (
                                models.Q(event_type="CORRECTION")
                                & (
                                    models.Q(replacement_odometer__isnull=False)
                                    | models.Q(replacement_hours__isnull=False)
                                )
                            )
                            | models.Q(
                                event_type="INVALIDATION",
                                replacement_odometer__isnull=True,
                                replacement_hours__isnull=True,
                            )
                        ),
                        name="reading_event_replacement",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(explanation__isnull=True)
                        | models.Q(explanation__regex=r"\S"),
                        name="reading_event_explanation",
                    ),
                ],
            },
        ),
        migrations.RunSQL(forward_sql(), reverse_sql=reverse_sql()),
        migrations.RunPython(migrations.RunPython.noop, guard_reverse),
    ]
