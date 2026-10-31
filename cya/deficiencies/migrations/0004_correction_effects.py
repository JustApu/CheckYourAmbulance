"""Correction and invalidation effects on deficiencies (issue 81 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/81); SPEC COR-3, COR-4, INV-7)."""

from importlib import import_module

from django.db import migrations, models

guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
signal = guards.signal
RECEIPT = guards.receipt_mismatch(
    "'CHECK_RECORD','DEFICIENCY_REPORT','CHECK_CORRECTION','CHECK_INVALIDATION'"
)


def _command(*types):
    listed = ",".join(f"'{t}'" for t in types)
    return (
        "NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id "
        f"AND r.command_type IN ({listed}))"
    )


# The chain a correction effect names must support this deficiency, and (for the notice and the
# error) be corrected, or (for the reopening) be invalidated, by the very command writing the row.
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
IF NEW.event_type<>'REPORTED' AND NOT EXISTS (SELECT 1 FROM deficiency_evidence e
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

# Evidence is a Failed or Unable check of the deficiency's vehicle; one check may support several
# deficiencies, so it need not have been written by the command that attaches it.
EVIDENCE_INSERT = f"""CREATE TRIGGER cya_deficiency_evidence_bi BEFORE INSERT ON deficiency_evidence
FOR EACH ROW BEGIN
IF {guards.receipt_mismatch(guards.CREATION_COMMANDS)}
THEN {signal("deficiency evidence receipt attribution mismatch")}; END IF;
IF NEW.check_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM check_record c
 JOIN deficiency d ON d.id=NEW.deficiency_id AND d.organization_id=NEW.organization_id
 WHERE c.id=NEW.check_id AND c.organization_id=NEW.organization_id
 AND c.recorded_at<=NEW.recorded_at AND c.vehicle_id=d.vehicle_id
 AND c.outcome IN ('FAILED','UNABLE_TO_INSPECT'))
THEN {signal("evidence check does not support the deficiency")}; END IF;
END"""


def _reverse_guard(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM deficiency_event WHERE event_type<>'REPORTED'")
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse correction effects while their events exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0003_default_severities"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="deficiencyevent",
            name="defevent_type",
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "event_type__in",
                        ("REPORTED", "REPORTED_IN_ERROR", "CORRECTION_NOTICE", "REOPENED"),
                    )
                ),
                name="defevent_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("event_type__in", ("REPORTED", "REOPENED", "CORRECTION_NOTICE")),
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
                    ("event_type", "REPORTED"),
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
        migrations.RunSQL(
            [
                "DROP TRIGGER cya_deficiency_event_bi",
                EVENT_INSERT,
                "DROP TRIGGER cya_deficiency_evidence_bi",
                EVIDENCE_INSERT,
            ],
            reverse_sql=[
                "DROP TRIGGER cya_deficiency_evidence_bi",
                guards.EVIDENCE_INSERT,
                "DROP TRIGGER cya_deficiency_event_bi",
                guards.EVENT_INSERT,
            ],
        ),
        migrations.RunPython(migrations.RunPython.noop, _reverse_guard),
    ]
