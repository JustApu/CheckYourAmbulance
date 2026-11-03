"""End one check suspension early while the deficiency is open (SPEC SUS-5, issue 32
(https://github.com/JustApu/CheckYourAmbulance/issues/32)).

SUSPENSION_ENDED keeps the status and lists exactly one START still in force in suspensions_ended,
which until now only a closing event could fill. The event trigger requires its own command, a
Supervisor or Administrator and a deficiency still being worked; the check_suspension trigger lets
that event's command write the END, as a closing event's command does. A closing event still lists
exactly the suspensions in force, so an early end and a closure serialize on the deficiency lock and
never end the same START twice.
"""

from importlib import import_module

from django.db import migrations, models

previous = import_module("cya.deficiencies.migrations.0017_check_suspension")
guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
signal = guards.signal
ROLES = previous.ROLES
ACTIVE = previous.ACTIVE
CLOSING = previous.CLOSING
CLOSING_COMMANDS = previous.CLOSING_COMMANDS
IN_FORCE = previous.IN_FORCE
END_COMMAND = "DEFICIENCY_CHECK_SUSPENSION_END"
ENDING = CLOSING[:-1] + ",'SUSPENSION_ENDED')"
ENDING_COMMANDS = CLOSING_COMMANDS[:-1] + f",'{END_COMMAND}')"

EARLY_END_GUARDS = f"""IF NEW.event_type='SUSPENSION_ENDED' AND (
 NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
  AND r.organization_id=NEW.organization_id AND r.command_type='{END_COMMAND}')
 OR NOT {ROLES} OR NEW.from_status NOT IN {ACTIVE} OR NEW.to_status<>NEW.from_status
 OR JSON_TYPE(NEW.suspensions_ended)<>'ARRAY' OR JSON_LENGTH(NEW.suspensions_ended)<>1
 OR JSON_TYPE(JSON_EXTRACT(NEW.suspensions_ended,'$[0]'))<>'STRING'
 OR NOT EXISTS (SELECT 1 {IN_FORCE}
  AND BINARY s.id=BINARY JSON_UNQUOTE(JSON_EXTRACT(NEW.suspensions_ended,'$[0]'))))
THEN {signal("an early suspension end needs its own command, a supervisor or administrator, an active deficiency and one suspension in force")}; END IF;
"""


def _replace(text, before, after):
    assert text.count(before) == 1, before
    return text.replace(before, after)


def event_insert():
    trigger = previous.event_insert()
    trigger = _replace(
        trigger,
        "'DEFICIENCY_VERIFICATION_ADD','DEFICIENCY_CHECKS_SUSPEND'))",
        f"'DEFICIENCY_VERIFICATION_ADD','DEFICIENCY_CHECKS_SUSPEND','{END_COMMAND}'))",
    )
    trigger = _replace(
        trigger,
        "'SCOPE_EXPANDED','VERIFICATION_ADDED','CHECKS_SUSPENDED') AND NOT EXISTS (",
        "'SCOPE_EXPANDED','VERIFICATION_ADDED','CHECKS_SUSPENDED','SUSPENSION_ENDED')"
        " AND NOT EXISTS (",
    )
    trigger = _replace(
        trigger,
        f" OR (NEW.event_type NOT IN {CLOSING} AND JSON_LENGTH(NEW.suspensions_ended)<>0)\n"
        f"THEN {signal('only a closing event ends check suspensions')}; END IF;",
        f" OR (NEW.event_type NOT IN {ENDING} AND JSON_LENGTH(NEW.suspensions_ended)<>0)\n"
        f"THEN {signal('only a closing event or an early end ends check suspensions')}; END IF;",
    )
    marker = "IF NEW.sequence=1 THEN"
    return _replace(trigger, marker, EARLY_END_GUARDS + marker)


def suspension_insert():
    trigger = previous.suspension_insert()
    trigger = _replace(
        trigger,
        f"(NEW.action='END' AND r.command_type IN {CLOSING_COMMANDS})",
        f"(NEW.action='END' AND r.command_type IN {ENDING_COMMANDS})",
    )
    return _replace(
        trigger,
        f"  AND e.event_type IN {CLOSING}\n",
        f"  AND e.event_type IN {ENDING}\n",
    )


def forward_guards():
    return [
        "DROP TRIGGER cya_deficiency_event_bi",
        event_insert(),
        "DROP TRIGGER cya_check_suspension_bi",
        suspension_insert(),
    ]


def reverse_guards():
    return [
        "DROP TRIGGER cya_check_suspension_bi",
        previous.suspension_insert(),
        "DROP TRIGGER cya_deficiency_event_bi",
        previous.event_insert(),
    ]


def refuse_reverse_with_data(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM deficiency_event WHERE event_type='SUSPENSION_ENDED'")
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse early suspension ends while any exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checks", "0010_verification_checks"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0018_acknowledgement_maximum_cap"),
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
            name="defevent_suspensions_closing",
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
                            "SCOPE_EXPANDED",
                            "VERIFICATION_ADDED",
                            "CHECKS_SUSPENDED",
                            "SUSPENSION_ENDED",
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
                            "SCOPE_EXPANDED",
                            "VERIFICATION_ADDED",
                            "CHECKS_SUSPENDED",
                            "SUSPENSION_ENDED",
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
                            "SCOPE_EXPANDED",
                            "VERIFICATION_ADDED",
                            "CHECKS_SUSPENDED",
                            "SUSPENSION_ENDED",
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
                    ("suspensions_ended", []),
                    (
                        "event_type__in",
                        (
                            "REPORTED_IN_ERROR",
                            "ADMINISTRATOR_CLOSED",
                            "EMPTY_SCOPE_CLOSED",
                            "VERIFICATION_CLOSED",
                            "SUSPENSION_ENDED",
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_suspensions_closing",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("event_type__in", ("SUSPENSION_ENDED",)), _negated=True),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("assignee__isnull", True),
                        ("correction_ref__isnull", True),
                        ("deadline__isnull", True),
                        ("decision__isnull", True),
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
                        ("previous_severity_version__isnull", True),
                        ("proposed_deadline__isnull", True),
                        ("request_event__isnull", True),
                        ("severity_version__isnull", True),
                        ("to_status", models.F("from_status")),
                        ("verification_check__isnull", True),
                        models.Q(("suspensions_ended", []), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="defevent_suspension_end_shape",
            ),
        ),
        migrations.RunSQL(forward_guards(), reverse_sql=reverse_guards()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_data),
    ]
