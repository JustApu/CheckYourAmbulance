"""Awaiting External Dependency events and progress notes (issue 149 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/149); SPEC 8.5, 8.1, 3.3).

Four more event types extend the closed deficiency_event schema. The CHECK constraints keep each
type's shape; the insert trigger repeats the commands' rules for a writer that bypasses them: the
event is written by its own command type, the actor's role snapshot may update the dependency or
add a note (Crew, Supervisor and Administrator on any deficiency, a Supply Officer only on a
supply-related category), and a follow-up instant lies after the instant it is set. Predecessor,
status and organization-pair foreign-key guards are the ones the earlier events already use.
"""

from importlib import import_module

from django.db import migrations, models

guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
assignment = import_module("cya.deficiencies.migrations.0006_assignment_deadlines")
signal = guards.signal

COMMANDS = (
    "DEFICIENCY_DEPENDENCY_ENTER",
    "DEFICIENCY_DEPENDENCY_REVISE",
    "DEFICIENCY_DEPENDENCY_END",
    "DEFICIENCY_PROGRESS_NOTE",
)
TYPES = "'DEPENDENCY_ENTERED','DEPENDENCY_REVISED','DEPENDENCY_ENDED','PROGRESS_NOTE'"
RECEIPT = guards.receipt_mismatch(
    "'CHECK_RECORD','DEFICIENCY_REPORT','CHECK_CORRECTION','CHECK_INVALIDATION',"
    "'DEFICIENCY_REGRADE','DEFICIENCY_ASSIGN','DEFICIENCY_DEADLINE','DEFICIENCY_START_WORK',"
    + ",".join(f"'{c}'" for c in COMMANDS)
)
ROLES = "NEW.actor_roles_at_time"

NEW_GUARDS = f"""IF NEW.event_type IN ({TYPES}) AND NOT EXISTS (SELECT 1 FROM command_receipt r
 WHERE r.id=NEW.command_id AND r.command_type=CASE NEW.event_type
 WHEN 'DEPENDENCY_ENTERED' THEN 'DEFICIENCY_DEPENDENCY_ENTER'
 WHEN 'DEPENDENCY_REVISED' THEN 'DEFICIENCY_DEPENDENCY_REVISE'
 WHEN 'DEPENDENCY_ENDED' THEN 'DEFICIENCY_DEPENDENCY_END'
 ELSE 'DEFICIENCY_PROGRESS_NOTE' END)
THEN {signal("dependency or note event needs its own command")}; END IF;
IF NEW.event_type IN ({TYPES}) AND NOT (JSON_CONTAINS({ROLES},'"CREW"')
 OR JSON_CONTAINS({ROLES},'"EMT"') OR JSON_CONTAINS({ROLES},'"SUPERVISOR"')
 OR JSON_CONTAINS({ROLES},'"ADMINISTRATOR"')
 OR (JSON_CONTAINS({ROLES},'"SUPPLY_OFFICER"') AND EXISTS (SELECT 1 FROM deficiency d
  JOIN deficiency_category c ON c.id=d.category_version_id
  WHERE d.id=NEW.deficiency_id AND d.organization_id=NEW.organization_id AND c.supply_related)))
THEN {signal("the actor may not update the dependency or add a note on this deficiency")};
END IF;
IF NEW.follow_up_at IS NOT NULL AND NEW.follow_up_at<=NEW.recorded_at
THEN {signal("a follow-up date must lie after the instant it is set")}; END IF;
"""


def _event_insert():
    trigger = assignment.EVENT_INSERT
    for old, new in (
        (assignment.RECEIPT, RECEIPT),
        (
            f"IF NEW.event_type NOT IN ('REPORTED','SEVERITY_CHANGED',{assignment.TYPES}) AND NOT EXISTS (",
            f"IF NEW.event_type NOT IN ('REPORTED','SEVERITY_CHANGED',{assignment.TYPES},{TYPES})"
            " AND NOT EXISTS (",
        ),
        ("IF NEW.sequence=1 THEN", NEW_GUARDS + "IF NEW.sequence=1 THEN"),
    ):
        assert trigger.count(old) == 1, old
        trigger = trigger.replace(old, new)
    return trigger


EVENT_INSERT = _event_insert()


def forward_statements():
    return ["DROP TRIGGER cya_deficiency_event_bi", EVENT_INSERT]


def reverse_statements():
    return ["DROP TRIGGER cya_deficiency_event_bi", assignment.EVENT_INSERT]


def _reverse_guard(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM deficiency_event WHERE event_type IN ({TYPES})")
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse dependency and note events while they exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0006_assignment_deadlines"),
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
                        (
                            "event_type__in",
                            (
                                "DEPENDENCY_ENTERED",
                                "DEPENDENCY_REVISED",
                                "DEPENDENCY_ENDED",
                                "PROGRESS_NOTE",
                            ),
                        ),
                        _negated=True,
                    ),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("assignee__isnull", True),
                        ("correction_ref__isnull", True),
                        ("deadline__isnull", True),
                        ("from_status__isnull", False),
                        ("proposed_deadline__isnull", True),
                        ("to_status__isnull", False),
                        models.Q(
                            models.Q(
                                ("event_type", "DEPENDENCY_ENTERED"),
                                ("explanation__isnull", False),
                                ("from_status__in", ("OPEN", "IN_PROGRESS")),
                                ("to_status", "AWAITING_EXTERNAL_DEPENDENCY"),
                            ),
                            models.Q(
                                ("event_type", "DEPENDENCY_REVISED"),
                                ("explanation__isnull", False),
                                ("from_status", "AWAITING_EXTERNAL_DEPENDENCY"),
                                ("to_status", "AWAITING_EXTERNAL_DEPENDENCY"),
                            ),
                            models.Q(
                                ("event_type", "DEPENDENCY_ENDED"),
                                ("explanation__isnull", True),
                                ("follow_up_at__isnull", True),
                                ("from_status", "AWAITING_EXTERNAL_DEPENDENCY"),
                                ("to_status__in", ("OPEN", "IN_PROGRESS")),
                            ),
                            models.Q(
                                ("event_type", "PROGRESS_NOTE"),
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
                                ("to_status", models.F("from_status")),
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_dependency_shape",
            ),
        ),
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
        migrations.RunPython(migrations.RunPython.noop, _reverse_guard),
    ]
