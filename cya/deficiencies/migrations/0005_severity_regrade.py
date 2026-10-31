"""Severity re-grade events and the projection's current severity (issue 123 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/123); SPEC DEF-8)."""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
effects = import_module("cya.deficiencies.migrations.0004_correction_effects")
signal = guards.signal
_command = effects._command
RECEIPT = guards.receipt_mismatch(
    "'CHECK_RECORD','DEFICIENCY_REPORT','CHECK_CORRECTION','CHECK_INVALIDATION','DEFICIENCY_REGRADE'"
)

# A re-grade names the level revision the deficiency has now (its header's, or the newest
# re-grade's) and an active revision of a different level, in force at the instant it is written.
# It keeps the status (the CHECK and the predecessor guard below) and needs no check chain, so
# the chain guard that the correction effects use does not apply to it.
EVENT_INSERT = f"""CREATE TRIGGER cya_deficiency_event_bi BEFORE INSERT ON deficiency_event
FOR EACH ROW BEGIN
IF {RECEIPT}
THEN {signal("deficiency event receipt attribution mismatch")}; END IF;
IF NEW.event_type='REPORTED' AND {_command("CHECK_RECORD", "DEFICIENCY_REPORT")}
THEN {signal("reported event needs a creating command")}; END IF;
IF NEW.event_type IN ('REPORTED_IN_ERROR','CORRECTION_NOTICE') AND {_command("CHECK_CORRECTION")}
THEN {signal("correction effect needs a correction command")}; END IF;
IF NEW.event_type='REOPENED' AND {_command("CHECK_INVALIDATION")}
THEN {signal("reopening needs an invalidation command")}; END IF;
IF NEW.event_type='SEVERITY_CHANGED' AND {_command("DEFICIENCY_REGRADE")}
THEN {signal("severity change needs a re-grade command")}; END IF;
IF NEW.event_type='SEVERITY_CHANGED' AND (NOT EXISTS (SELECT 1 FROM deficiency_severity v
 WHERE v.id=NEW.severity_version_id AND v.organization_id=NEW.organization_id AND v.active
 AND v.effective_at<=NEW.recorded_at AND NOT EXISTS (SELECT 1 FROM deficiency_severity n
  WHERE n.organization_id=v.organization_id AND n.code=v.code AND n.revision>v.revision
  AND n.effective_at<=NEW.recorded_at))
 OR NOT EXISTS (SELECT 1 FROM deficiency_severity a JOIN deficiency_severity b
  ON b.organization_id=a.organization_id AND b.code<>a.code
  WHERE a.id=NEW.previous_severity_version_id AND b.id=NEW.severity_version_id
  AND a.organization_id=NEW.organization_id)
 OR NOT EXISTS (SELECT 1 FROM deficiency d WHERE d.id=NEW.deficiency_id
  AND d.organization_id=NEW.organization_id
  AND NEW.previous_severity_version_id <=> COALESCE((SELECT e.severity_version_id
   FROM deficiency_event e WHERE e.deficiency_id=d.id AND e.event_type='SEVERITY_CHANGED'
   ORDER BY e.sequence DESC LIMIT 1), d.severity_version_id)))
THEN {signal("severity change must follow the current level and set another active level")};
END IF;
IF NEW.event_type NOT IN ('REPORTED','SEVERITY_CHANGED') AND NOT EXISTS (
 SELECT 1 FROM deficiency_evidence e
 JOIN check_record c ON c.id=e.check_id WHERE e.deficiency_id=NEW.deficiency_id
 AND e.organization_id=NEW.organization_id AND BINARY c.root_check_id=BINARY NEW.correction_ref)
THEN {signal("the check chain does not support the deficiency")}; END IF;
IF NEW.event_type IN ('REPORTED_IN_ERROR','CORRECTION_NOTICE') AND NOT EXISTS (
 SELECT 1 FROM check_event ev WHERE ev.organization_id=NEW.organization_id
 AND BINARY ev.root_check_id=BINARY NEW.correction_ref AND ev.event_type='CORRECTION'
 AND ev.command_id=NEW.command_id)
THEN {signal("the chain was not corrected by this command")}; END IF;
IF NEW.event_type='REOPENED' AND (NOT EXISTS (
 SELECT 1 FROM check_event ev WHERE ev.organization_id=NEW.organization_id
 AND BINARY ev.root_check_id=BINARY NEW.correction_ref AND ev.event_type='INVALIDATION'
 AND ev.command_id=NEW.command_id)
 OR NOT EXISTS (SELECT 1 FROM deficiency_event x WHERE x.id=NEW.predecessor_event_id
 AND x.deficiency_id=NEW.deficiency_id AND x.event_type='REPORTED_IN_ERROR'
 AND BINARY x.correction_ref=BINARY NEW.correction_ref))
THEN {signal("the chain did not just cancel this deficiency or was not invalidated")}; END IF;
IF NEW.sequence=1 THEN
 IF NEW.event_type<>'REPORTED' OR NOT EXISTS (SELECT 1 FROM deficiency d
  WHERE d.id=NEW.deficiency_id AND d.organization_id=NEW.organization_id
  AND d.command_id=NEW.command_id)
 THEN {signal("first deficiency event must report its header")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM deficiency_event p WHERE p.id=NEW.predecessor_event_id
  AND p.organization_id=NEW.organization_id AND p.deficiency_id=NEW.deficiency_id
  AND p.sequence=NEW.sequence-1 AND p.recorded_at<=NEW.recorded_at
  AND p.to_status<=>NEW.from_status)
 THEN {signal("deficiency event transition mismatch")}; END IF;
END IF;
END"""

# (table, column): each is an organization-pair foreign key to deficiency_severity, like the
# header's severity_version_id in migration 0002.
FKS = (
    ("deficiency_event", "previous_severity_version_id"),
    ("deficiency_event", "severity_version_id"),
    ("deficiency_projection", "severity_version_id"),
)


def forward_statements():
    return [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_def_regrade_fk_{index:02d}` "
        f"FOREIGN KEY (organization_id,{column}) REFERENCES `deficiency_severity` "
        "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT"
        for index, (table, column) in enumerate(FKS, start=1)
    ] + ["DROP TRIGGER cya_deficiency_event_bi", EVENT_INSERT]


def reverse_statements():
    return [
        "DROP TRIGGER cya_deficiency_event_bi",
        effects.EVENT_INSERT,
        *(
            f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_def_regrade_fk_{index:02d}`"
            for index, (table, _column) in reversed(list(enumerate(FKS, start=1)))
        ),
    ]


def _reverse_guard(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM deficiency_event WHERE event_type='SEVERITY_CHANGED'")
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse severity re-grades while their events exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0004_correction_effects"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="deficiencyevent", name="defevent_type"),
        migrations.RemoveConstraint(model_name="deficiencyevent", name="defevent_error_shape"),
        migrations.RemoveConstraint(model_name="deficiencyevent", name="defevent_effect_shape"),
        migrations.AddField(
            model_name="deficiencyevent",
            name="previous_severity_version",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiencyseverity",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyevent",
            name="severity_version",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiencyseverity",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyprojection",
            name="severity_version",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiencyseverity",
            ),
        ),
        # No deficiency has been re-graded yet, so every current level is its header's.
        migrations.RunSQL(
            "UPDATE deficiency_projection p JOIN deficiency d ON d.id=p.deficiency_id "
            "SET p.severity_version_id=d.severity_version_id",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # AlterField would rebuild the column with the table's default collation; every id column
        # and the level it points at are ascii_bin, and a foreign key needs them to agree.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="deficiencyprojection",
                    name="severity_version",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_deficiencies.deficiencyseverity",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    "ALTER TABLE deficiency_projection MODIFY severity_version_id "
                    "char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL",
                    reverse_sql="ALTER TABLE deficiency_projection MODIFY severity_version_id "
                    "char(36) CHARACTER SET ascii COLLATE ascii_bin NULL",
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "event_type__in",
                        (
                            "REPORTED",
                            "REPORTED_IN_ERROR",
                            "CORRECTION_NOTICE",
                            "REOPENED",
                            "SEVERITY_CHANGED",
                        ),
                    )
                ),
                name="defevent_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "event_type__in",
                        ("REPORTED", "REOPENED", "CORRECTION_NOTICE", "SEVERITY_CHANGED"),
                    ),
                    models.Q(
                        ("event_type", "REPORTED_IN_ERROR"),
                        ("from_status__in", ("OPEN", "IN_PROGRESS", "PENDING_VERIFICATION")),
                        ("to_status", "REPORTED_IN_ERROR"),
                    ),
                    _connector="OR",
                ),
                name="defevent_error_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("event_type__in", ("REPORTED", "SEVERITY_CHANGED")),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("assignee__isnull", True),
                        ("correction_ref__isnull", False),
                        ("deadline__isnull", True),
                        ("explanation__isnull", False),
                        ("follow_up_at__isnull", True),
                        ("from_status__isnull", False),
                        ("proposed_deadline__isnull", True),
                        ("to_status__isnull", False),
                        models.Q(
                            ("event_type", "REPORTED_IN_ERROR"),
                            models.Q(
                                ("event_type", "CORRECTION_NOTICE"),
                                ("to_status", models.F("from_status")),
                            ),
                            models.Q(
                                ("event_type", "REOPENED"),
                                ("from_status", "REPORTED_IN_ERROR"),
                                ("to_status", "OPEN"),
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_effect_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("event_type", "SEVERITY_CHANGED"), _negated=True),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("assignee__isnull", True),
                        ("correction_ref__isnull", True),
                        ("deadline__isnull", True),
                        ("explanation__isnull", False),
                        ("follow_up_at__isnull", True),
                        (
                            "from_status__in",
                            (
                                "OPEN",
                                "IN_PROGRESS",
                                "AWAITING_EXTERNAL_DEPENDENCY",
                                "PENDING_VERIFICATION",
                            ),
                        ),
                        ("previous_severity_version__isnull", False),
                        ("proposed_deadline__isnull", True),
                        ("severity_version__isnull", False),
                        ("to_status", models.F("from_status")),
                        ("to_status__isnull", False),
                        models.Q(
                            ("previous_severity_version", models.F("severity_version")),
                            _negated=True,
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_severity_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("event_type", "SEVERITY_CHANGED"),
                    models.Q(
                        ("previous_severity_version__isnull", True),
                        ("severity_version__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="defevent_severity_only_regrade",
            ),
        ),
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
        migrations.RunPython(migrations.RunPython.noop, _reverse_guard),
    ]
