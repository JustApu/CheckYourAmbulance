"""A lost verification Pass reopens the automatic closure it supported (issue 155 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/155), INV-4).

VERIFICATION_REOPENED (Closed Verified to Open) is written only by the invalidation of a
verification check chain, or by its author's correction of the Pass to Failed or Unable to Inspect,
in the same transaction as that check event. The insert trigger repeats what the database can know:
the event names the root of a verification chain for this deficiency that this very command
invalidated or corrected, the chain belongs to the scope revision of the deficiency's newest
automatic closure, and at least one row of that revision is no longer satisfied by an effective
Pass (an independent Pass of the same row keeps the closure).
"""

from importlib import import_module

from django.db import migrations, models

previous = import_module("cya.deficiencies.migrations.0014_verification_events")
signal = previous.signal
NO_KIT = previous.NO_KIT

# The scope revision the newest automatic closure satisfied (the one the reopening may revisit).
CLOSURE_REVISION = """(SELECT k.verification_scope_revision FROM deficiency_event z
  JOIN check_record k ON k.id=z.verification_check_id
  WHERE z.deficiency_id=NEW.deficiency_id AND z.event_type='VERIFICATION_CLOSED'
  ORDER BY z.sequence DESC LIMIT 1)"""

REOPEN_GUARDS = f"""IF NEW.event_type='VERIFICATION_REOPENED' AND (
 NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
  AND r.organization_id=NEW.organization_id
  AND r.command_type IN ('CHECK_INVALIDATION','CHECK_CORRECTION'))
 OR NOT EXISTS (SELECT 1 FROM check_record c WHERE c.id=NEW.verification_check_id
  AND c.organization_id=NEW.organization_id AND c.predecessor_check_id IS NULL
  AND c.purpose='VERIFICATION' AND c.verification_for_deficiency_id=NEW.deficiency_id
  AND c.verification_scope_revision={CLOSURE_REVISION}
  AND EXISTS (SELECT 1 FROM check_event ev WHERE ev.root_check_id=c.id
   AND ev.organization_id=NEW.organization_id AND ev.command_id=NEW.command_id
   AND ev.event_type IN ('INVALIDATION','CORRECTION')))
 OR NOT EXISTS (SELECT 1 FROM verification_scope_row s WHERE s.deficiency_id=NEW.deficiency_id
  AND s.organization_id=NEW.organization_id AND s.scope_revision={CLOSURE_REVISION}
  AND NOT EXISTS (SELECT 1 FROM check_record q WHERE q.organization_id=NEW.organization_id
   AND q.purpose='VERIFICATION' AND q.verification_for_deficiency_id=NEW.deficiency_id
   AND q.item_id=s.item_id AND BINARY IFNULL(q.kit_id,'{NO_KIT}')=BINARY s.instance_key
   AND q.verification_scope_revision=s.scope_revision
   AND q.outcome='PASSED' AND q.recorded_at>s.required_after AND q.recorded_at<=NEW.recorded_at
   AND NOT EXISTS (SELECT 1 FROM check_record n WHERE n.predecessor_check_id=q.id)
   AND NOT EXISTS (SELECT 1 FROM check_event ie WHERE ie.root_check_id=q.root_check_id
    AND ie.event_type='INVALIDATION' AND ie.recorded_at<=NEW.recorded_at)))
 OR NEW.from_status<>'CLOSED_VERIFIED' OR NEW.to_status<>'OPEN')
THEN {signal("verification reopening needs a lost Pass of the closing revision")}; END IF;
"""


def event_insert():
    trigger = previous.event_insert()
    before = "'VERIFICATION_CLOSED','VERIFICATION_FAILED') AND NOT EXISTS ("
    after = "'VERIFICATION_CLOSED','VERIFICATION_FAILED','VERIFICATION_REOPENED') AND NOT EXISTS ("
    assert trigger.count(before) == 1
    trigger = trigger.replace(before, after)
    marker = "IF NEW.sequence=1 THEN"
    assert trigger.count(marker) == 1
    return trigger.replace(marker, REOPEN_GUARDS + marker)


FORWARD = ["DROP TRIGGER cya_deficiency_event_bi", event_insert()]
REVERSE = ["DROP TRIGGER cya_deficiency_event_bi", previous.event_insert()]


def refuse_reverse_with_data(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM deficiency_event WHERE event_type='VERIFICATION_REOPENED'"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse verification reopenings while such events exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checks", "0010_verification_checks"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0014_verification_events"),
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
        migrations.RemoveConstraint(
            model_name="deficiencyevent",
            name="defevent_verification_only",
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
                            "VERIFICATION_REOPENED",
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
                            "VERIFICATION_REOPENED",
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
                            "VERIFICATION_REOPENED",
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
                    (
                        "event_type__in",
                        ("VERIFICATION_CLOSED", "VERIFICATION_FAILED", "VERIFICATION_REOPENED"),
                    ),
                    ("verification_check__isnull", True),
                    _connector="OR",
                ),
                name="defevent_verification_only",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("event_type__in", ("VERIFICATION_REOPENED",)), _negated=True),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("assignee__isnull", True),
                        ("correction_ref__isnull", True),
                        ("deadline__isnull", True),
                        ("decision__isnull", True),
                        ("explanation__isnull", False),
                        ("follow_up_at__isnull", True),
                        ("from_status", "CLOSED_VERIFIED"),
                        ("previous_severity_version__isnull", True),
                        ("proposed_deadline__isnull", True),
                        ("request_event__isnull", True),
                        ("severity_version__isnull", True),
                        ("suspensions_ended", []),
                        ("to_status", "OPEN"),
                        ("verification_check__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="defevent_verification_reopen_shape",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_data),
    ]
