"""Supervisor suspension of routine checks for a known deficiency (issue 13
(https://github.com/JustApu/CheckYourAmbulance/issues/13), SPEC 8.9 SUS-1 to SUS-4, AC-18).

check_suspension keeps each suspended item (and physical kit instance) as an append-only START,
written with the CHECKS_SUSPENDED event of the same command, and its END, written by the event that
closes or cancels the deficiency. That closing event lists the ended STARTs in suspensions_ended,
which until now every CHECK constraint and the event trigger forced empty (issue 152
(https://github.com/JustApu/CheckYourAmbulance-archive/issues/152), migration 0011).

The triggers repeat the command's rules: a suspension starts only with its own command and event,
from a Supervisor or Administrator, on a deficiency still being worked, for an item and kit that fit
the vehicle, and not twice at once; a closing event lists exactly the deficiency's suspensions still
in force (so none outlives an open deficiency), and every other event lists none; an END names its
START once and the closing event of its own command that lists it.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN

previous = import_module("cya.deficiencies.migrations.0016_scope_expansion")
guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
signal = guards.signal
NO_KIT = previous.NO_KIT
ROLES = previous.ROLES
ACTIVE = "('OPEN','IN_PROGRESS','AWAITING_EXTERNAL_DEPENDENCY','PENDING_VERIFICATION')"
CLOSING = "('REPORTED_IN_ERROR','ADMINISTRATOR_CLOSED','EMPTY_SCOPE_CLOSED','VERIFICATION_CLOSED')"
# The commands that write a closing event, and so the END rows it lists.
CLOSING_COMMANDS = (
    "('CHECK_CORRECTION','DEFICIENCY_ADMINISTRATOR_CLOSE','DEFICIENCY_EMPTY_SCOPE_CLOSE',"
    "'CHECK_RECORD')"
)
# A START of this deficiency that no END names yet.
IN_FORCE = """FROM check_suspension s WHERE s.deficiency_id=NEW.deficiency_id
 AND s.organization_id=NEW.organization_id AND s.action='START'
 AND NOT EXISTS (SELECT 1 FROM check_suspension x WHERE x.prior_suspension_id=s.id)"""
LISTED = (
    "JSON_TABLE(NEW.suspensions_ended,'$[*]' COLUMNS(n FOR ORDINALITY, sid VARCHAR(64) PATH '$')) j"
)

SUSPENSION_LIST_GUARDS = f"""IF JSON_TYPE(NEW.suspensions_ended)<>'ARRAY'
 OR (NEW.event_type NOT IN {CLOSING} AND JSON_LENGTH(NEW.suspensions_ended)<>0)
THEN {signal("only a closing event ends check suspensions")}; END IF;
IF NEW.event_type IN {CLOSING} AND (
 EXISTS (SELECT 1 FROM {LISTED}
  WHERE JSON_TYPE(JSON_EXTRACT(NEW.suspensions_ended,CONCAT('$[',j.n-1,']')))<>'STRING'
  OR NOT EXISTS (SELECT 1 {IN_FORCE} AND BINARY s.id=BINARY j.sid))
 OR (SELECT COUNT(DISTINCT CAST(j.sid AS BINARY)) FROM {LISTED})
  <>JSON_LENGTH(NEW.suspensions_ended)
 OR (SELECT COUNT(*) {IN_FORCE})<>JSON_LENGTH(NEW.suspensions_ended))
THEN {signal("a closing event must list exactly the check suspensions in force")}; END IF;
"""

SUSPENSION_EVENT_GUARDS = f"""IF NEW.event_type='CHECKS_SUSPENDED' AND (
 NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
  AND r.organization_id=NEW.organization_id AND r.command_type='DEFICIENCY_CHECKS_SUSPEND')
 OR NOT {ROLES} OR NEW.from_status NOT IN {ACTIVE} OR NEW.to_status<>NEW.from_status)
THEN {signal("a check suspension needs its own command, a supervisor or administrator and an active deficiency")}; END IF;
"""


def _replace(text, before, after):
    assert text.count(before) == 1, before
    return text.replace(before, after)


def event_insert():
    trigger = previous.event_insert()
    trigger = _replace(
        trigger,
        "'DEFICIENCY_EMPTY_SCOPE_CLOSE','DEFICIENCY_SCOPE_EXPAND','DEFICIENCY_VERIFICATION_ADD'))",
        "'DEFICIENCY_EMPTY_SCOPE_CLOSE','DEFICIENCY_SCOPE_EXPAND','DEFICIENCY_VERIFICATION_ADD',"
        "'DEFICIENCY_CHECKS_SUSPEND'))",
    )
    trigger = _replace(
        trigger,
        "'SCOPE_EXPANDED','VERIFICATION_ADDED') AND NOT EXISTS (",
        "'SCOPE_EXPANDED','VERIFICATION_ADDED','CHECKS_SUSPENDED') AND NOT EXISTS (",
    )
    trigger = _replace(
        trigger,
        "IF JSON_TYPE(NEW.suspensions_ended)<>'ARRAY' OR JSON_LENGTH(NEW.suspensions_ended)<>0\n"
        "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'suspension endings are not available yet'; END IF;\n",
        SUSPENSION_LIST_GUARDS,
    )
    marker = "IF NEW.sequence=1 THEN"
    return _replace(trigger, marker, SUSPENSION_EVENT_GUARDS + marker)


def suspension_insert():
    return f"""CREATE TRIGGER cya_check_suspension_bi BEFORE INSERT ON check_suspension
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at
 AND ((NEW.action='START' AND r.command_type='DEFICIENCY_CHECKS_SUSPEND')
  OR (NEW.action='END' AND r.command_type IN {CLOSING_COMMANDS})))
THEN {signal("check suspension receipt attribution mismatch")}; END IF;
IF NOT (BINARY NEW.instance_key <=> BINARY IFNULL(NEW.kit_instance_id,'{NO_KIT}'))
THEN {signal("check suspension instance key mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM deficiency d WHERE d.id=NEW.deficiency_id
 AND d.organization_id=NEW.organization_id AND d.vehicle_id=NEW.vehicle_id)
THEN {signal("a check suspension concerns the vehicle of its deficiency")}; END IF;
IF NEW.action='START' THEN
 IF NOT EXISTS (SELECT 1 FROM deficiency_event e WHERE e.id=NEW.deficiency_event_id
  AND e.organization_id=NEW.organization_id AND e.deficiency_id=NEW.deficiency_id
  AND e.command_id=NEW.command_id AND e.recorded_at=NEW.recorded_at
  AND e.event_type='CHECKS_SUSPENDED' AND e.to_status IN {ACTIVE}
  AND NOT EXISTS (SELECT 1 FROM deficiency_event n WHERE n.deficiency_id=e.deficiency_id
   AND n.sequence>e.sequence))
 THEN {signal("a check suspension starts with its own event on an open deficiency")}; END IF;
 IF EXISTS (SELECT 1 FROM check_suspension s WHERE s.deficiency_id=NEW.deficiency_id
  AND s.organization_id=NEW.organization_id AND s.action='START' AND s.item_id=NEW.item_id
  AND BINARY s.instance_key=BINARY NEW.instance_key
  AND NOT EXISTS (SELECT 1 FROM check_suspension x WHERE x.prior_suspension_id=s.id))
 THEN {signal("the check is already suspended for this deficiency")}; END IF;
 IF (NEW.kit_instance_id IS NULL AND EXISTS (SELECT 1 FROM checklist_item_def cd
  WHERE cd.id=NEW.definition_id AND cd.kit_id IS NOT NULL)) OR
  (NEW.kit_instance_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM checklist_item_def cd
   JOIN kit_instance ref ON ref.id=cd.kit_id
   LEFT JOIN kit_assignment_event ref_first ON ref_first.kit_id=ref.id AND ref_first.sequence=1
   JOIN compartment ref_slot ON ref_slot.id=COALESCE(ref_first.compartment_id,ref.compartment_id)
   JOIN kit_instance actual ON actual.id=NEW.kit_instance_id
   JOIN compartment actual_slot ON actual_slot.id=actual.compartment_id
   WHERE cd.id=NEW.definition_id AND actual.organization_id=NEW.organization_id
   AND actual.vehicle_id=NEW.vehicle_id AND actual.active
   AND actual_slot.stable_slot_id=ref_slot.stable_slot_id))
 THEN {signal("check suspension kit instance does not fit the item")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM check_suspension s WHERE s.id=NEW.prior_suspension_id
  AND s.organization_id=NEW.organization_id AND s.action='START'
  AND s.deficiency_id=NEW.deficiency_id AND s.vehicle_id=NEW.vehicle_id
  AND s.item_id=NEW.item_id AND s.definition_id=NEW.definition_id
  AND s.kit_instance_id <=> NEW.kit_instance_id)
  OR NOT EXISTS (SELECT 1 FROM deficiency_event e WHERE e.id=NEW.deficiency_event_id
  AND e.organization_id=NEW.organization_id AND e.deficiency_id=NEW.deficiency_id
  AND e.command_id=NEW.command_id AND e.recorded_at=NEW.recorded_at
  AND e.event_type IN {CLOSING}
  AND JSON_CONTAINS(e.suspensions_ended,JSON_QUOTE(NEW.prior_suspension_id)))
 THEN {signal("a check suspension ends only with the closing event that lists its start")}; END IF;
END IF;
END"""


TABLE = "check_suspension"
FKS = [
    ("command_id", "command_receipt"),
    ("actor_user_id", "`user`"),
    ("deficiency_id", "deficiency"),
    ("vehicle_id", "vehicle"),
    ("item_id", "item"),
    ("kit_instance_id", "kit_instance"),
    ("deficiency_event_id", "deficiency_event"),
    ("prior_suspension_id", "check_suspension"),
]


def forward_guards():
    return [
        "DROP TRIGGER cya_deficiency_event_bi",
        event_insert(),
        f"ALTER TABLE {TABLE} ADD CONSTRAINT cya_checksusp_uuid CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT cya_checksusp_roles CHECK "
        "(JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        'AND JSON_CONTAINS(\'[\\"CREW\\",\\"EMT\\",\\"SUPERVISOR\\",\\"SUPPLY_OFFICER\\",\\"ADMINISTRATOR\\"]\', actor_roles_at_time))',
        *[
            f"ALTER TABLE {TABLE} ADD CONSTRAINT cya_checksusp_fk_{i:02d} FOREIGN KEY "
            f"(organization_id,{column}) REFERENCES {target} (organization_id,id) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
            for i, (column, target) in enumerate(FKS, start=1)
        ],
        f"ALTER TABLE {TABLE} ADD CONSTRAINT cya_checksusp_event_deficiency_fk FOREIGN KEY "
        "(deficiency_event_id,deficiency_id) REFERENCES deficiency_event (id,deficiency_id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT cya_checksusp_definition_item_fk FOREIGN KEY "
        "(definition_id,item_id) REFERENCES checklist_item_def (id,item_id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        f"CREATE TRIGGER cya_check_suspension_bu BEFORE UPDATE ON {TABLE} FOR EACH ROW "
        + signal("append-only table"),
        f"CREATE TRIGGER cya_check_suspension_bd BEFORE DELETE ON {TABLE} FOR EACH ROW "
        + signal("append-only table"),
        suspension_insert(),
    ]


def reverse_guards():
    return [
        "DROP TRIGGER cya_check_suspension_bi",
        "DROP TRIGGER cya_check_suspension_bu",
        "DROP TRIGGER cya_check_suspension_bd",
        *[
            f"ALTER TABLE {TABLE} DROP FOREIGN KEY cya_checksusp_fk_{i:02d}"
            for i in range(len(FKS), 0, -1)
        ],
        f"ALTER TABLE {TABLE} DROP FOREIGN KEY cya_checksusp_event_deficiency_fk",
        f"ALTER TABLE {TABLE} DROP FOREIGN KEY cya_checksusp_definition_item_fk",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT cya_checksusp_roles",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT cya_checksusp_uuid",
        "DROP TRIGGER cya_deficiency_event_bi",
        previous.event_insert(),
    ]


def refuse_reverse_with_data(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT COUNT(*) FROM check_suspension) + (SELECT COUNT(*) "
            "FROM deficiency_event WHERE event_type='CHECKS_SUSPENDED' "
            "OR JSON_LENGTH(suspensions_ended)<>0)"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse check suspension while suspensions exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0003_publication_command_type_guards"),
        ("cya_checks", "0010_verification_checks"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0016_scope_expansion"),
    ]

    operations = [
        migrations.CreateModel(
            name="CheckSuspension",
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
                ("instance_key", models.CharField(db_collation="ascii_bin", max_length=36)),
                ("action", models.CharField(max_length=5)),
            ],
            options={
                "db_table": "check_suspension",
            },
        ),
        migrations.RemoveConstraint(
            model_name="deficiencyevent",
            name="defevent_suspensions_empty",
        ),
        migrations.RemoveConstraint(
            model_name="deficiencyevent",
            name="defevent_admin_close_shape",
        ),
        migrations.RemoveConstraint(
            model_name="deficiencyevent",
            name="defevent_verification_shape",
        ),
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
                                "ADMINISTRATOR_CLOSED",
                                "ADMINISTRATOR_REOPENED",
                                "EMPTY_SCOPE_CLOSED",
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
                        ("decision__isnull", True),
                        ("explanation__isnull", False),
                        ("follow_up_at__isnull", True),
                        ("previous_severity_version__isnull", True),
                        ("proposed_deadline__isnull", True),
                        ("request_event__isnull", True),
                        ("severity_version__isnull", True),
                        ("explanation__regex", "\\S"),
                        models.Q(
                            models.Q(
                                ("event_type", "ADMINISTRATOR_CLOSED"),
                                (
                                    "from_status__in",
                                    (
                                        "OPEN",
                                        "IN_PROGRESS",
                                        "AWAITING_EXTERNAL_DEPENDENCY",
                                        "PENDING_VERIFICATION",
                                    ),
                                ),
                                ("to_status", "CLOSED_ADMINISTRATOR"),
                            ),
                            models.Q(
                                ("event_type", "EMPTY_SCOPE_CLOSED"),
                                (
                                    "from_status__in",
                                    ("OPEN", "IN_PROGRESS", "AWAITING_EXTERNAL_DEPENDENCY"),
                                ),
                                ("to_status", "CLOSED_UNVERIFIABLE"),
                            ),
                            models.Q(
                                ("event_type", "ADMINISTRATOR_REOPENED"),
                                (
                                    "from_status__in",
                                    ("CLOSED_ADMINISTRATOR", "CLOSED_UNVERIFIABLE"),
                                ),
                                ("to_status", "OPEN"),
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_admin_close_shape",
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
                    models.Q(("event_type__in", ("CHECKS_SUSPENDED",)), _negated=True),
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
                        ("suspensions_ended", []),
                        ("to_status", models.F("from_status")),
                        ("verification_check__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="defevent_suspension_shape",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="actor_user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_accounts.user",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="command",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_core.commandreceipt",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="deficiency",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiency",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="deficiency_event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiencyevent",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="definition",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.checklistitemdef",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.item",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="kit_instance",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.kitinstance",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_accounts.organization",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="prior_suspension",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.checksuspension",
            ),
        ),
        migrations.AddField(
            model_name="checksuspension",
            name="vehicle",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.vehicle",
            ),
        ),
        migrations.AddConstraint(
            model_name="checksuspension",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="checksusp_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="checksuspension",
            constraint=models.UniqueConstraint(
                fields=("deficiency_event", "item", "instance_key"),
                name="checksusp_event_item_instance",
            ),
        ),
        migrations.AddConstraint(
            model_name="checksuspension",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("action", "START"), ("prior_suspension__isnull", True)),
                    models.Q(("action", "END"), ("prior_suspension__isnull", False)),
                    _connector="OR",
                ),
                name="checksusp_action",
            ),
        ),
        migrations.AddConstraint(
            model_name="checksuspension",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "instance_key__regex",
                        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    )
                ),
                name="checksusp_instance_key",
            ),
        ),
        migrations.RunSQL(forward_guards(), reverse_sql=reverse_guards()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_data),
    ]
