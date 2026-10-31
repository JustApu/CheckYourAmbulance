"""Verification events close a deficiency or return it to work with their check (issue 154 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/154)).

VERIFICATION_CLOSED (Pending Verification to Closed Verified, VER-2/VER-4) and VERIFICATION_FAILED
(Pending Verification to In Progress, VER-5, SPEC 1.15.0) are written only by record_check, in the
transaction that inserts the verification check they name in verification_check_id. The insert
trigger repeats what the database can know: the check is that command's own verification check for
this deficiency, a Pass for a closure and a Fail for a return, and a closure also needs every row
of the newest scope revision satisfied by an effective Pass recorded strictly after the row's
instant, and no supporting check chain of the deficiency invalidated (INV-3: such a deficiency
stays open until an explicit closure).
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

previous = import_module("cya.deficiencies.migrations.0013_empty_scope_closure")
guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
signal = guards.signal
NO_KIT = "00000000-0000-0000-0000-000000000000"

VERIFICATION_GUARDS = f"""IF NEW.event_type IN ('VERIFICATION_CLOSED','VERIFICATION_FAILED') AND (
 NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
  AND r.organization_id=NEW.organization_id AND r.command_type='CHECK_RECORD')
 OR NOT EXISTS (SELECT 1 FROM check_record c WHERE c.id=NEW.verification_check_id
  AND c.organization_id=NEW.organization_id AND c.command_id=NEW.command_id
  AND c.recorded_at=NEW.recorded_at AND c.actor_user_id <=> NEW.actor_user_id
  AND c.predecessor_check_id IS NULL AND c.purpose='VERIFICATION'
  AND c.verification_for_deficiency_id=NEW.deficiency_id
  AND c.outcome=IF(NEW.event_type='VERIFICATION_CLOSED','PASSED','FAILED')))
THEN {signal("verification event does not match its check")}; END IF;
IF NEW.event_type='VERIFICATION_CLOSED' AND (
 NOT EXISTS (SELECT 1 FROM verification_scope_row s WHERE s.deficiency_id=NEW.deficiency_id
  AND s.organization_id=NEW.organization_id)
 OR EXISTS (SELECT 1 FROM verification_scope_row s WHERE s.deficiency_id=NEW.deficiency_id
  AND s.organization_id=NEW.organization_id
  AND s.scope_revision=(SELECT MAX(x.scope_revision) FROM verification_scope_row x
   WHERE x.deficiency_id=s.deficiency_id)
  AND NOT EXISTS (SELECT 1 FROM check_record q WHERE q.organization_id=NEW.organization_id
   AND q.purpose='VERIFICATION' AND q.verification_for_deficiency_id=NEW.deficiency_id
   AND q.item_id=s.item_id AND BINARY IFNULL(q.kit_id,'{NO_KIT}')=BINARY s.instance_key
   AND q.verification_scope_revision=s.scope_revision
   AND q.outcome='PASSED' AND q.recorded_at>s.required_after AND q.recorded_at<=NEW.recorded_at
   AND NOT EXISTS (SELECT 1 FROM check_record n WHERE n.predecessor_check_id=q.id)
   AND NOT EXISTS (SELECT 1 FROM check_event ie WHERE ie.root_check_id=q.root_check_id
    AND ie.event_type='INVALIDATION' AND ie.recorded_at<=NEW.recorded_at)))
 OR EXISTS (SELECT 1 FROM deficiency_evidence de
  JOIN check_record dc ON dc.id=de.check_id
  JOIN check_event ie ON ie.root_check_id=dc.root_check_id AND ie.event_type='INVALIDATION'
  WHERE de.deficiency_id=NEW.deficiency_id AND de.organization_id=NEW.organization_id
  AND ie.recorded_at<=NEW.recorded_at)
 OR NEW.from_status<>'PENDING_VERIFICATION' OR NEW.to_status<>'CLOSED_VERIFIED')
THEN {signal("verification closure needs every scope row satisfied and no invalidated support")}; END IF;
IF NEW.event_type='VERIFICATION_FAILED' AND
 (NEW.from_status<>'PENDING_VERIFICATION' OR NEW.to_status<>'IN_PROGRESS')
THEN {signal("failed verification must return a pending deficiency to work")}; END IF;
"""


def event_insert():
    trigger = previous.event_insert()
    before = "'CORRECTION_RECORDED','EMPTY_SCOPE_CLOSED') AND NOT EXISTS ("
    after = "'CORRECTION_RECORDED','EMPTY_SCOPE_CLOSED','VERIFICATION_CLOSED','VERIFICATION_FAILED') AND NOT EXISTS ("
    assert trigger.count(before) == 1
    trigger = trigger.replace(before, after)
    marker = "IF NEW.sequence=1 THEN"
    assert trigger.count(marker) == 1
    return trigger.replace(marker, VERIFICATION_GUARDS + marker)


def forward_guards():
    return [
        "ALTER TABLE deficiency_event ADD CONSTRAINT cya_defevent_verification_check_org_fk "
        "FOREIGN KEY (organization_id,verification_check_id) REFERENCES check_record "
        "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        "DROP TRIGGER cya_deficiency_event_bi",
        event_insert(),
    ]


def reverse_guards():
    return [
        "DROP TRIGGER cya_deficiency_event_bi",
        previous.event_insert(),
        "ALTER TABLE deficiency_event DROP FOREIGN KEY cya_defevent_verification_check_org_fk",
        # As in the check migrations, a dumped FK index must go before the column does.
        "DROP INDEX IF EXISTS cya_defevent_verification_check_org_fk ON deficiency_event",
    ]


def refuse_reverse_with_data(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM deficiency_event "
            "WHERE event_type IN ('VERIFICATION_CLOSED','VERIFICATION_FAILED')"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse verification events while such events exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checks", "0010_verification_checks"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0013_empty_scope_closure"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="deficiencyevent",
            name="defevent_type",
        ),
        migrations.RemoveConstraint(
            model_name="deficiencyevent",
            name="defevent_error_shape",
        ),
        migrations.RemoveConstraint(
            model_name="deficiencyevent",
            name="defevent_effect_shape",
        ),
        migrations.AddField(
            model_name="deficiencyevent",
            name="verification_check",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checks.checkrecord",
            ),
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
                            "ASSIGNED",
                            "DEADLINE_SET",
                            "WORK_STARTED",
                            "DEPENDENCY_ENTERED",
                            "DEPENDENCY_REVISED",
                            "DEPENDENCY_ENDED",
                            "PROGRESS_NOTE",
                            "EXTENSION_REQUESTED",
                            "EXTENSION_DECIDED",
                            "ESCALATION_ACKNOWLEDGED",
                            "ADMINISTRATOR_CLOSED",
                            "ADMINISTRATOR_REOPENED",
                            "EMPTY_SCOPE_CLOSED",
                            "CORRECTION_RECORDED",
                            "VERIFICATION_CLOSED",
                            "VERIFICATION_FAILED",
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
                        (
                            "REPORTED",
                            "REOPENED",
                            "CORRECTION_NOTICE",
                            "SEVERITY_CHANGED",
                            "ASSIGNED",
                            "DEADLINE_SET",
                            "WORK_STARTED",
                            "DEPENDENCY_ENTERED",
                            "DEPENDENCY_REVISED",
                            "DEPENDENCY_ENDED",
                            "PROGRESS_NOTE",
                            "EXTENSION_REQUESTED",
                            "EXTENSION_DECIDED",
                            "ESCALATION_ACKNOWLEDGED",
                            "ADMINISTRATOR_CLOSED",
                            "ADMINISTRATOR_REOPENED",
                            "EMPTY_SCOPE_CLOSED",
                            "CORRECTION_RECORDED",
                            "VERIFICATION_CLOSED",
                            "VERIFICATION_FAILED",
                        ),
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
                    (
                        "event_type__in",
                        (
                            "REPORTED",
                            "SEVERITY_CHANGED",
                            "ASSIGNED",
                            "DEADLINE_SET",
                            "WORK_STARTED",
                            "DEPENDENCY_ENTERED",
                            "DEPENDENCY_REVISED",
                            "DEPENDENCY_ENDED",
                            "PROGRESS_NOTE",
                            "EXTENSION_REQUESTED",
                            "EXTENSION_DECIDED",
                            "ESCALATION_ACKNOWLEDGED",
                            "ADMINISTRATOR_CLOSED",
                            "ADMINISTRATOR_REOPENED",
                            "EMPTY_SCOPE_CLOSED",
                            "CORRECTION_RECORDED",
                            "VERIFICATION_CLOSED",
                            "VERIFICATION_FAILED",
                        ),
                    ),
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
                    models.Q(
                        ("event_type__in", ("VERIFICATION_CLOSED", "VERIFICATION_FAILED")),
                        _negated=True,
                    ),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("assignee__isnull", True),
                        ("correction_ref__isnull", True),
                        ("deadline__isnull", True),
                        ("decision__isnull", True),
                        ("explanation__isnull", True),
                        ("follow_up_at__isnull", True),
                        ("from_status", "PENDING_VERIFICATION"),
                        ("previous_severity_version__isnull", True),
                        ("proposed_deadline__isnull", True),
                        ("request_event__isnull", True),
                        ("severity_version__isnull", True),
                        ("suspensions_ended", []),
                        ("verification_check__isnull", False),
                        models.Q(
                            models.Q(
                                ("event_type", "VERIFICATION_CLOSED"),
                                ("to_status", "CLOSED_VERIFIED"),
                            ),
                            models.Q(
                                ("event_type", "VERIFICATION_FAILED"), ("to_status", "IN_PROGRESS")
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_verification_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("event_type__in", ("VERIFICATION_CLOSED", "VERIFICATION_FAILED")),
                    ("verification_check__isnull", True),
                    _connector="OR",
                ),
                name="defevent_verification_only",
            ),
        ),
        migrations.RunSQL(forward_guards(), reverse_sql=reverse_guards()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_data),
    ]
