"""Supervisor scope expansion and added verification after closure (issue 156 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/156), SPEC 1.22.0).

deficiency_scope_expansion keeps the items a supervisor added; SCOPE_EXPANDED (Pending
Verification, unchanged) and VERIFICATION_ADDED (Closed Verified to Open with a new deadline) are
the events that add them. An expansion also appends a scope revision of the same correction whose
repeated rows keep their instant and whose added rows take the expansion instant.

The triggers repeat what the database can know: each new row and event is written by its own
command type and actor role (Supervisor or Administrator); expansion rows belong to that command's
event; an expansion revision belongs to the correction of the revision it expands, with each repeated
row keeping its earlier instant; a Pass counts for a row when recorded against a revision of the
same correction up to the row's (VER-4 as amended), both for automatic closure and for the INV-4
reopening; and closure also needs every row of the correction's earlier revisions repeated in the
closing revision (an expansion never shrinks scope).
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN

previous = import_module("cya.deficiencies.migrations.0015_verification_reopen")
scope_previous = import_module("cya.deficiencies.migrations.0012_correction_verification_scope")
guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
signal = guards.signal
NO_KIT = previous.NO_KIT
ROLES = "(JSON_CONTAINS(NEW.actor_roles_at_time,'\"SUPERVISOR\"') OR JSON_CONTAINS(NEW.actor_roles_at_time,'\"ADMINISTRATOR\"'))"


def _lineage(row, revision):
    """SQL: the revision is the row's own or an earlier one of the same correction (VER-4)."""
    return (
        f"{revision}<={row}.scope_revision AND EXISTS (SELECT 1 FROM verification_scope_row l "
        f"WHERE l.deficiency_id={row}.deficiency_id AND l.scope_revision={revision} "
        f"AND l.correction_event_id={row}.correction_event_id)"
    )


SCOPE_EVENT_GUARDS = f"""IF NEW.event_type IN ('SCOPE_EXPANDED','VERIFICATION_ADDED') AND (
 NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
  AND r.organization_id=NEW.organization_id AND r.command_type=IF(NEW.event_type='SCOPE_EXPANDED',
  'DEFICIENCY_SCOPE_EXPAND','DEFICIENCY_VERIFICATION_ADD'))
 OR NOT {ROLES})
THEN {signal("scope expansion needs its own command and a supervisor or administrator")}; END IF;
IF NEW.event_type='SCOPE_EXPANDED' AND (NEW.from_status<>'PENDING_VERIFICATION'
 OR NEW.to_status<>'PENDING_VERIFICATION' OR NEW.deadline IS NOT NULL)
THEN {signal("scope expansion keeps a pending deficiency pending")}; END IF;
IF NEW.event_type='VERIFICATION_ADDED' AND (NEW.from_status<>'CLOSED_VERIFIED'
 OR NEW.to_status<>'OPEN' OR NEW.deadline IS NULL OR NEW.deadline<=NEW.recorded_at)
THEN {signal("added verification reopens a verified closure with a future deadline")}; END IF;
"""

# The closing revision must repeat every row of its correction's earlier revisions.
NO_SHRINK = """ OR EXISTS (SELECT 1 FROM verification_scope_row o
  JOIN verification_scope_row top ON top.deficiency_id=o.deficiency_id
   AND top.correction_event_id=o.correction_event_id
   AND top.scope_revision=(SELECT MAX(x.scope_revision) FROM verification_scope_row x
    WHERE x.deficiency_id=NEW.deficiency_id)
  WHERE o.deficiency_id=NEW.deficiency_id AND o.organization_id=NEW.organization_id
  AND NOT EXISTS (SELECT 1 FROM verification_scope_row y WHERE y.deficiency_id=o.deficiency_id
   AND y.scope_revision=top.scope_revision AND y.item_id=o.item_id
   AND BINARY y.instance_key=BINARY o.instance_key))
 """


# A row an expansion repeats verbatim from the revision it expands (same correction, written by
# an earlier command). It was checked when first written; a kit moved or retired since must not
# block the expansion, and it must not be dropped either (an expansion never shrinks scope).
REPEATED = """EXISTS (SELECT 1 FROM verification_scope_row rp
  WHERE rp.deficiency_id=NEW.deficiency_id AND rp.correction_event_id=NEW.correction_event_id
  AND rp.command_id<>NEW.command_id AND rp.scope_revision=NEW.scope_revision-1
  AND rp.item_id=NEW.item_id AND rp.definition_id=NEW.definition_id
  AND rp.kit_instance_id<=>NEW.kit_instance_id AND rp.required_after=NEW.required_after)"""


def _replace(text, before, after, count=1):
    assert text.count(before) == count, before
    return text.replace(before, after)


def event_insert():
    trigger = previous.event_insert()
    trigger = _replace(
        trigger,
        "'DEFICIENCY_EMPTY_SCOPE_CLOSE'))",
        "'DEFICIENCY_EMPTY_SCOPE_CLOSE','DEFICIENCY_SCOPE_EXPAND','DEFICIENCY_VERIFICATION_ADD'))",
    )
    trigger = _replace(
        trigger,
        "'VERIFICATION_REOPENED') AND NOT EXISTS (",
        "'VERIFICATION_REOPENED','SCOPE_EXPANDED','VERIFICATION_ADDED') AND NOT EXISTS (",
    )
    # A Pass counts for a row of its correction's later revision, not only its own (VER-4).
    trigger = _replace(
        trigger,
        "AND q.verification_scope_revision=s.scope_revision",
        "AND " + _lineage("s", "q.verification_scope_revision"),
        count=2,
    )
    closing = previous.CLOSURE_REVISION
    trigger = _replace(
        trigger,
        f"AND c.verification_scope_revision={closing}",
        f"""AND EXISTS (SELECT 1 FROM verification_scope_row cr
   WHERE cr.deficiency_id=NEW.deficiency_id AND cr.scope_revision={closing}
   AND {_lineage("cr", "c.verification_scope_revision")})""",
    )
    trigger = _replace(
        trigger,
        "OR NEW.from_status<>'PENDING_VERIFICATION' OR NEW.to_status<>'CLOSED_VERIFIED')",
        NO_SHRINK
        + "OR NEW.from_status<>'PENDING_VERIFICATION' OR NEW.to_status<>'CLOSED_VERIFIED')",
    )
    marker = "IF NEW.sequence=1 THEN"
    return _replace(trigger, marker, SCOPE_EVENT_GUARDS + marker)


def scope_insert():
    trigger = scope_previous.scope_insert()
    trigger = _replace(
        trigger,
        "r.command_type='DEFICIENCY_CORRECTION_RECORDED')",
        "r.command_type IN ('DEFICIENCY_CORRECTION_RECORDED','DEFICIENCY_SCOPE_EXPAND'))",
    )
    # Either the correction written by this command, or an expansion by this command of the
    # newest revision written before it, which keeps that revision's correction.
    trigger = _replace(
        trigger,
        "AND e.recorded_at=NEW.recorded_at)",
        """AND e.recorded_at=NEW.recorded_at) AND NOT (EXISTS (SELECT 1 FROM deficiency_event x
  WHERE x.deficiency_id=NEW.deficiency_id AND x.organization_id=NEW.organization_id
  AND x.command_id=NEW.command_id AND x.event_type='SCOPE_EXPANDED'
  AND x.recorded_at=NEW.recorded_at)
 AND NEW.correction_event_id<=>(SELECT p.correction_event_id FROM verification_scope_row p
  WHERE p.deficiency_id=NEW.deficiency_id AND p.command_id<>NEW.command_id
  ORDER BY p.scope_revision DESC LIMIT 1))""",
    )
    # A repeated row keeps the instant of the same row in the revision it expands.
    trigger = _replace(
        trigger,
        "IF NEW.required_after<>NEW.recorded_at OR NEW.scope_revision<1",
        """IF NEW.scope_revision<1 OR (NEW.required_after<>NEW.recorded_at AND NOT EXISTS (
 SELECT 1 FROM verification_scope_row p WHERE p.deficiency_id=NEW.deficiency_id
 AND p.correction_event_id=NEW.correction_event_id AND p.command_id<>NEW.command_id
 AND p.scope_revision=NEW.scope_revision-1 AND p.item_id=NEW.item_id
 AND BINARY p.instance_key=BINARY NEW.instance_key AND p.required_after=NEW.required_after))""",
    )
    # A repeated row skips the placement checks it passed when first written.
    slot = "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'verification kit instance does not match the definition slot'"
    trigger = _replace(trigger, slot, f" AND NOT {REPEATED}\n" + slot)
    # An item a supervisor added may lie outside the deficiency's own target; a correction
    # resolves its definition afresh, so the stored pair is matched by item and instance.
    outside = "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'verification item is outside the deficiency target'"
    return _replace(
        trigger,
        outside,
        f""" AND NOT EXISTS (SELECT 1 FROM deficiency_scope_expansion se
  WHERE se.deficiency_id=NEW.deficiency_id AND se.organization_id=NEW.organization_id
  AND se.item_id=NEW.item_id AND BINARY se.instance_key=BINARY NEW.instance_key)
 AND NOT {REPEATED}
"""
        + outside,
    )


def expansion_insert():
    return f"""CREATE TRIGGER cya_deficiency_scope_expansion_bi BEFORE INSERT ON deficiency_scope_expansion
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at
 AND r.command_type IN ('DEFICIENCY_SCOPE_EXPAND','DEFICIENCY_VERIFICATION_ADD'))
THEN {signal("scope expansion receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM deficiency_event e WHERE e.id=NEW.event_id
 AND e.organization_id=NEW.organization_id AND e.deficiency_id=NEW.deficiency_id
 AND e.command_id=NEW.command_id AND e.recorded_at=NEW.recorded_at
 AND e.event_type IN ('SCOPE_EXPANDED','VERIFICATION_ADDED'))
THEN {signal("scope expansion must belong to the event of this command")}; END IF;
IF NOT (BINARY NEW.instance_key <=> BINARY IFNULL(NEW.kit_instance_id,'{NO_KIT}'))
THEN {signal("scope expansion instance key mismatch")}; END IF;
IF (NEW.kit_instance_id IS NULL AND EXISTS (SELECT 1 FROM checklist_item_def cd
 WHERE cd.id=NEW.definition_id AND cd.kit_id IS NOT NULL)) OR
 (NEW.kit_instance_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM checklist_item_def cd
  JOIN kit_instance ref ON ref.id=cd.kit_id
  LEFT JOIN kit_assignment_event ref_first ON ref_first.kit_id=ref.id AND ref_first.sequence=1
  JOIN compartment ref_slot ON ref_slot.id=COALESCE(ref_first.compartment_id,ref.compartment_id)
  JOIN kit_instance actual ON actual.id=NEW.kit_instance_id
  JOIN compartment actual_slot ON actual_slot.id=actual.compartment_id
  JOIN deficiency d ON d.id=NEW.deficiency_id
  WHERE cd.id=NEW.definition_id AND actual.organization_id=NEW.organization_id
  AND actual.vehicle_id=d.vehicle_id AND actual.active
  AND actual_slot.stable_slot_id=ref_slot.stable_slot_id))
THEN {signal("scope expansion kit instance does not fit the item")}; END IF;
END"""


def forward_guards():
    table = "deficiency_scope_expansion"
    fks = [
        ("command_id", "command_receipt"),
        ("actor_user_id", "`user`"),
        ("deficiency_id", "deficiency"),
        ("event_id", "deficiency_event"),
        ("item_id", "item"),
        ("kit_instance_id", "kit_instance"),
    ]
    return [
        "DROP TRIGGER cya_deficiency_event_bi",
        event_insert(),
        "DROP TRIGGER cya_verification_scope_row_bi",
        scope_insert(),
        f"ALTER TABLE {table} ADD CONSTRAINT cya_defexp_uuid CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE {table} ADD CONSTRAINT cya_defexp_roles CHECK "
        "(JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        'AND JSON_CONTAINS(\'[\\"CREW\\",\\"EMT\\",\\"SUPERVISOR\\",\\"SUPPLY_OFFICER\\",\\"ADMINISTRATOR\\"]\', actor_roles_at_time))',
        *[
            f"ALTER TABLE {table} ADD CONSTRAINT cya_defexp_fk_{i:02d} FOREIGN KEY "
            f"(organization_id,{column}) REFERENCES {target} (organization_id,id) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
            for i, (column, target) in enumerate(fks, start=1)
        ],
        f"ALTER TABLE {table} ADD CONSTRAINT cya_defexp_event_deficiency_fk FOREIGN KEY "
        "(event_id,deficiency_id) REFERENCES deficiency_event (id,deficiency_id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        f"ALTER TABLE {table} ADD CONSTRAINT cya_defexp_definition_item_fk FOREIGN KEY "
        "(definition_id,item_id) REFERENCES checklist_item_def (id,item_id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        f"CREATE TRIGGER cya_deficiency_scope_expansion_bu BEFORE UPDATE ON {table} FOR EACH ROW "
        + signal("append-only table"),
        f"CREATE TRIGGER cya_deficiency_scope_expansion_bd BEFORE DELETE ON {table} FOR EACH ROW "
        + signal("append-only table"),
        expansion_insert(),
    ]


def reverse_guards():
    table = "deficiency_scope_expansion"
    return [
        "DROP TRIGGER cya_deficiency_scope_expansion_bi",
        "DROP TRIGGER cya_deficiency_scope_expansion_bu",
        "DROP TRIGGER cya_deficiency_scope_expansion_bd",
        *[f"ALTER TABLE {table} DROP FOREIGN KEY cya_defexp_fk_{i:02d}" for i in range(6, 0, -1)],
        f"ALTER TABLE {table} DROP FOREIGN KEY cya_defexp_event_deficiency_fk",
        f"ALTER TABLE {table} DROP FOREIGN KEY cya_defexp_definition_item_fk",
        f"ALTER TABLE {table} DROP CONSTRAINT cya_defexp_roles",
        f"ALTER TABLE {table} DROP CONSTRAINT cya_defexp_uuid",
        "DROP TRIGGER cya_verification_scope_row_bi",
        scope_previous.scope_insert(),
        "DROP TRIGGER cya_deficiency_event_bi",
        previous.event_insert(),
    ]


def refuse_reverse_with_data(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT COUNT(*) FROM deficiency_scope_expansion) + (SELECT COUNT(*) "
            "FROM deficiency_event WHERE event_type IN ('SCOPE_EXPANDED','VERIFICATION_ADDED'))"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse scope expansion while expansions exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0003_publication_command_type_guards"),
        ("cya_checks", "0010_verification_checks"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0015_verification_reopen"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeficiencyScopeExpansion",
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
            ],
            options={
                "db_table": "deficiency_scope_expansion",
            },
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
                        ("event_type__in", ("SCOPE_EXPANDED", "VERIFICATION_ADDED")), _negated=True
                    ),
                    models.Q(
                        ("acknowledgement_until__isnull", True),
                        ("action_plan__isnull", True),
                        ("assignee__isnull", True),
                        ("correction_ref__isnull", True),
                        ("decision__isnull", True),
                        ("explanation__isnull", False),
                        ("follow_up_at__isnull", True),
                        ("previous_severity_version__isnull", True),
                        ("proposed_deadline__isnull", True),
                        ("request_event__isnull", True),
                        ("severity_version__isnull", True),
                        ("suspensions_ended", []),
                        ("verification_check__isnull", True),
                        models.Q(
                            models.Q(
                                ("deadline__isnull", True),
                                ("event_type", "SCOPE_EXPANDED"),
                                ("from_status", "PENDING_VERIFICATION"),
                                ("to_status", "PENDING_VERIFICATION"),
                            ),
                            models.Q(
                                ("deadline__isnull", False),
                                ("event_type", "VERIFICATION_ADDED"),
                                ("from_status", "CLOSED_VERIFIED"),
                                ("to_status", "OPEN"),
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="defevent_scope_shape",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="actor_user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_accounts.user",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="command",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_core.commandreceipt",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="deficiency",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiency",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="definition",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.checklistitemdef",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiencyevent",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.item",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="kit_instance",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.kitinstance",
            ),
        ),
        migrations.AddField(
            model_name="deficiencyscopeexpansion",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_accounts.organization",
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyscopeexpansion",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="defexpansion_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyscopeexpansion",
            constraint=models.UniqueConstraint(
                fields=("event", "item", "instance_key"), name="defexpansion_event_item_instance"
            ),
        ),
        migrations.AddConstraint(
            model_name="deficiencyscopeexpansion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "instance_key__regex",
                        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    )
                ),
                name="defexpansion_instance_key",
            ),
        ),
        migrations.RunSQL(forward_guards(), reverse_sql=reverse_guards()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_data),
    ]
