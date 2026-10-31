"""Assignment, deadline and work-start events (issue 146 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/146); SPEC DEF-4, section 3.3, 8.2).

Three more event types extend the closed deficiency_event schema. The CHECK constraints keep each
type's shape; the insert trigger repeats the command's rules for a writer that bypasses it: the
event is written by its own command type, the actor's role snapshot may assign (a Supply Officer
only on a supply-related category), a deadline lies after the instant it is set, and an assignee
is an active user of the organization who holds Crew, Supervisor or Supply Officer at that
instant. Assignment-at-report is held to the same assignee rule. Predecessor, status and
organization-pair foreign-key guards are the ones the earlier events already use.
"""

from importlib import import_module

from django.db import migrations, models

guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
regrade = import_module("cya.deficiencies.migrations.0005_severity_regrade")
signal = guards.signal

COMMANDS = ("DEFICIENCY_ASSIGN", "DEFICIENCY_DEADLINE", "DEFICIENCY_START_WORK")
TYPES = "'ASSIGNED','DEADLINE_SET','WORK_STARTED'"
RECEIPT = guards.receipt_mismatch(
    "'CHECK_RECORD','DEFICIENCY_REPORT','CHECK_CORRECTION','CHECK_INVALIDATION',"
    "'DEFICIENCY_REGRADE'," + ",".join(f"'{c}'" for c in COMMANDS)
)
ROLES = "NEW.actor_roles_at_time"
HOLDS_ROLE = "('CREW','EMT','SUPERVISOR','SUPPLY_OFFICER')"

NEW_GUARDS = f"""IF NEW.event_type IN ({TYPES}) AND NOT EXISTS (SELECT 1 FROM command_receipt r
 WHERE r.id=NEW.command_id AND r.command_type=CASE NEW.event_type WHEN 'ASSIGNED'
 THEN 'DEFICIENCY_ASSIGN' WHEN 'DEADLINE_SET' THEN 'DEFICIENCY_DEADLINE'
 ELSE 'DEFICIENCY_START_WORK' END)
THEN {signal("assignment event needs its own command")}; END IF;
IF NEW.event_type IN ({TYPES}) AND NOT (JSON_CONTAINS({ROLES},'"SUPERVISOR"')
 OR JSON_CONTAINS({ROLES},'"ADMINISTRATOR"')
 OR (JSON_CONTAINS({ROLES},'"SUPPLY_OFFICER"') AND EXISTS (SELECT 1 FROM deficiency d
  JOIN deficiency_category c ON c.id=d.category_version_id
  WHERE d.id=NEW.deficiency_id AND d.organization_id=NEW.organization_id AND c.supply_related)))
THEN {signal("the actor may not assign or start work on this deficiency")}; END IF;
IF NEW.event_type='DEADLINE_SET' AND NEW.deadline<=NEW.recorded_at
THEN {signal("a deadline must lie after the instant it is set")}; END IF;
IF NEW.assignee_id IS NOT NULL AND NEW.event_type IN ('REPORTED','ASSIGNED') AND NOT EXISTS (
 SELECT 1 FROM `user` u JOIN role_grant g ON g.user_id=u.id AND g.organization_id=u.organization_id
 WHERE u.id=NEW.assignee_id AND u.organization_id=NEW.organization_id AND u.active
 AND g.action='GRANT' AND g.role IN {HOLDS_ROLE} AND g.recorded_at<=NEW.recorded_at
 AND NOT EXISTS (SELECT 1 FROM role_grant x WHERE x.prior_grant_id=g.id
  AND x.recorded_at<=NEW.recorded_at))
THEN {signal("the assignee is not an active Crew, Supervisor or Supply Officer")}; END IF;
"""


def _event_insert():
    trigger = regrade.EVENT_INSERT
    for old, new in (
        (regrade.RECEIPT, RECEIPT),
        (
            "IF NEW.event_type NOT IN ('REPORTED','SEVERITY_CHANGED') AND NOT EXISTS (",
            f"IF NEW.event_type NOT IN ('REPORTED','SEVERITY_CHANGED',{TYPES}) AND NOT EXISTS (",
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
    return ["DROP TRIGGER cya_deficiency_event_bi", regrade.EVENT_INSERT]


def _reverse_guard(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM deficiency_event WHERE event_type IN ({TYPES})")
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse assignment events while they exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_authorization", "0002_role_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0005_severity_regrade"),
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
                        ("event_type__in", ("ASSIGNED", "DEADLINE_SET", "WORK_STARTED")),
                        _negated=True,
                    ),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("correction_ref__isnull", True),
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
                        ("proposed_deadline__isnull", True),
                        ("to_status__isnull", False),
                        models.Q(
                            models.Q(
                                ("assignee__isnull", False),
                                ("deadline__isnull", True),
                                ("event_type", "ASSIGNED"),
                                ("to_status", models.F("from_status")),
                            ),
                            models.Q(
                                ("assignee__isnull", True),
                                ("deadline__isnull", False),
                                ("event_type", "DEADLINE_SET"),
                                ("to_status", models.F("from_status")),
                            ),
                            models.Q(
                                ("assignee__isnull", True),
                                ("deadline__isnull", True),
                                ("event_type", "WORK_STARTED"),
                                ("from_status", "OPEN"),
                                ("to_status", "IN_PROGRESS"),
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_assignment_shape",
            ),
        ),
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
        migrations.RunPython(migrations.RunPython.noop, _reverse_guard),
    ]
