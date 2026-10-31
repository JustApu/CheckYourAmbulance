"""Deadline extension events (issue 150 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/150); SPEC DEF-5, section 3.3, 8.4).

Two more event types extend the closed deficiency_event schema, with a decision column and a
one-to-one request reference. The CHECK constraints keep each type's shape; the insert trigger
repeats the commands' rules for a writer that bypasses them: the event is written by its own
command type; the actor's role snapshot may request (Crew, Supervisor or Administrator, or a
Supply Officer on a deficiency assigned to them) or decide (Supervisor or Administrator); a date
lies after the instant it is set and after the deadline then governing, which is the deadline of
the newest event that carries one; a decision answers an undecided request that no later
deadline-setting event has superseded, and an approval carries exactly the requested date.
Predecessor, status and organization-pair foreign-key guards are the ones the earlier events
already use.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
dependency = import_module("cya.deficiencies.migrations.0007_dependency_and_progress_notes")
assignment = import_module("cya.deficiencies.migrations.0006_assignment_deadlines")
signal = guards.signal

COMMANDS = ("DEFICIENCY_EXTENSION_REQUEST", "DEFICIENCY_EXTENSION_DECIDE")
TYPES = "'EXTENSION_REQUESTED','EXTENSION_DECIDED'"
RECEIPT = guards.receipt_mismatch(
    "'CHECK_RECORD','DEFICIENCY_REPORT','CHECK_CORRECTION','CHECK_INVALIDATION',"
    "'DEFICIENCY_REGRADE','DEFICIENCY_ASSIGN','DEFICIENCY_DEADLINE','DEFICIENCY_START_WORK',"
    "'DEFICIENCY_DEPENDENCY_ENTER','DEFICIENCY_DEPENDENCY_REVISE','DEFICIENCY_DEPENDENCY_END',"
    "'DEFICIENCY_PROGRESS_NOTE'," + ",".join(f"'{c}'" for c in COMMANDS)
)
ROLES = "NEW.actor_roles_at_time"
# The deadline then governing a deficiency: that of its newest deadline-bearing event.
GOVERNING = (
    "(SELECT e.deadline FROM deficiency_event e WHERE e.deficiency_id=NEW.deficiency_id"
    " AND e.deadline IS NOT NULL ORDER BY e.sequence DESC LIMIT 1)"
)

NEW_GUARDS = f"""IF NEW.event_type IN ({TYPES}) AND NOT EXISTS (SELECT 1 FROM command_receipt r
 WHERE r.id=NEW.command_id AND r.command_type=CASE NEW.event_type
 WHEN 'EXTENSION_REQUESTED' THEN 'DEFICIENCY_EXTENSION_REQUEST'
 ELSE 'DEFICIENCY_EXTENSION_DECIDE' END)
THEN {signal("extension event needs its own command")}; END IF;
IF NEW.event_type='EXTENSION_REQUESTED' AND NOT (JSON_CONTAINS({ROLES},'"CREW"')
 OR JSON_CONTAINS({ROLES},'"EMT"') OR JSON_CONTAINS({ROLES},'"SUPERVISOR"')
 OR JSON_CONTAINS({ROLES},'"ADMINISTRATOR"')
 OR (JSON_CONTAINS({ROLES},'"SUPPLY_OFFICER"') AND EXISTS (SELECT 1 FROM deficiency_projection p
  WHERE p.deficiency_id=NEW.deficiency_id AND p.organization_id=NEW.organization_id
  AND p.assignee_id=NEW.actor_user_id)))
THEN {signal("the actor may not request an extension on this deficiency")}; END IF;
IF NEW.event_type='EXTENSION_DECIDED' AND NOT (JSON_CONTAINS({ROLES},'"SUPERVISOR"')
 OR JSON_CONTAINS({ROLES},'"ADMINISTRATOR"'))
THEN {signal("only a supervisor or administrator may decide an extension")}; END IF;
IF NEW.event_type='EXTENSION_REQUESTED' AND (NEW.proposed_deadline<=NEW.recorded_at
 OR {GOVERNING} IS NULL OR NEW.proposed_deadline<={GOVERNING})
THEN {signal("an extension asks for a later date than the one governing")}; END IF;
IF NEW.event_type='EXTENSION_DECIDED' AND NEW.deadline IS NOT NULL
 AND (NEW.deadline<=NEW.recorded_at OR NEW.deadline<={GOVERNING})
THEN {signal("a decided date must lie after the instant and the governing deadline")}; END IF;
IF NEW.event_type='EXTENSION_DECIDED' AND NOT EXISTS (SELECT 1 FROM deficiency_event q
 WHERE q.id=NEW.request_event_id AND q.organization_id=NEW.organization_id
 AND q.deficiency_id=NEW.deficiency_id AND q.event_type='EXTENSION_REQUESTED'
 AND NOT EXISTS (SELECT 1 FROM deficiency_event b WHERE b.deficiency_id=q.deficiency_id
  AND b.sequence>q.sequence AND b.deadline IS NOT NULL AND b.event_type<>'EXTENSION_DECIDED'))
THEN {signal("a decision must answer a request not superseded by a later deadline")}; END IF;
IF NEW.event_type='EXTENSION_DECIDED' AND NEW.decision='APPROVED' AND NOT EXISTS (
 SELECT 1 FROM deficiency_event q WHERE q.id=NEW.request_event_id
 AND q.proposed_deadline=NEW.deadline)
THEN {signal("an approval carries exactly the requested date")}; END IF;
"""


def _event_insert():
    trigger = dependency.EVENT_INSERT
    chain = f"{assignment.TYPES},{dependency.TYPES}"
    for old, new in (
        (dependency.RECEIPT, RECEIPT),
        (
            f"IF NEW.event_type NOT IN ('REPORTED','SEVERITY_CHANGED',{chain}) AND NOT EXISTS (",
            f"IF NEW.event_type NOT IN ('REPORTED','SEVERITY_CHANGED',{chain},{TYPES})"
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
    return ["DROP TRIGGER cya_deficiency_event_bi", dependency.EVENT_INSERT]


def _reverse_guard(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM deficiency_event WHERE event_type IN ({TYPES})")
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse extension events while they exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0007_dependency_and_progress_notes"),
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
            name="decision",
            field=models.CharField(max_length=14, null=True),
        ),
        migrations.AddField(
            model_name="deficiencyevent",
            name="request_event",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiencyevent",
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
                        ("event_type__in", ("EXTENSION_REQUESTED", "EXTENSION_DECIDED")),
                        _negated=True,
                    ),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("assignee__isnull", True),
                        ("correction_ref__isnull", True),
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
                        models.Q(
                            models.Q(
                                ("deadline__isnull", True),
                                ("decision__isnull", True),
                                ("event_type", "EXTENSION_REQUESTED"),
                                ("proposed_deadline__isnull", False),
                                ("request_event__isnull", True),
                            ),
                            models.Q(
                                ("decision__in", ("APPROVED", "DENIED", "DIFFERENT_DATE")),
                                ("event_type", "EXTENSION_DECIDED"),
                                ("proposed_deadline__isnull", True),
                                ("request_event__isnull", False),
                                models.Q(
                                    models.Q(("deadline__isnull", True), ("decision", "DENIED")),
                                    models.Q(
                                        ("deadline__isnull", False),
                                        ("decision__in", ("APPROVED", "DIFFERENT_DATE")),
                                    ),
                                    _connector="OR",
                                ),
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_extension_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("event_type", "EXTENSION_DECIDED"),
                    models.Q(("decision__isnull", True), ("request_event__isnull", True)),
                    _connector="OR",
                ),
                name="defevent_extension_only_decision",
            ),
        ),
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
        migrations.RunPython(migrations.RunPython.noop, _reverse_guard),
    ]
