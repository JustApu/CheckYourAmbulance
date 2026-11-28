"""Paper-entry invalidation of checks (SPEC 8.0.0 INV-8, LATE-6, §18.4, AC-43 (7); issue #9 B5).

check_event gains replacement_paper_entry_batch_id, an organization-pair reference set only on
an INVALIDATION of a check mistakenly saved in Normal mode, after the same work was re-entered in
that paper-entry batch. The check_event insert trigger keeps every correction rule and the
administrator's CHECK_INVALIDATION exactly as before, and accepts two more INVALIDATION kinds:

- a CHECK_INVALIDATION naming a replacement batch, by a Supervisor or Administrator other than
  the check's author, of a routine live (not paper) chain, when the batch is for the same vehicle,
  not invalidated, performed no later than the mistaken check was saved, and holds an effective
  paper check of the same work (item, kit instance, kit line or presence, item requirement);
- an INVALIDATION appended by a PAPER_ENTRY_BATCH_INVALIDATION command, by a Supervisor or
  Administrator, of a chain of the batch the receipt's response body names, when the batch's
  entry actor is someone else.

The check insert trigger refuses a paper check of an invalidated batch. The obligation
determination trigger accepts the UTI-3 voiding written by a batch invalidation as it accepts
an administrator's chain invalidation. The reverse refusal is the last operation.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

prior_event = import_module("cya.checks.migrations.0007_chain_invalidation")
prior_check = import_module("cya.checks.migrations.0022_paper_checks")
prior_determination = import_module("cya.checks.migrations.0009_unable_check_chain_effects")

BATCH_COMMAND = "PAPER_ENTRY_BATCH_INVALIDATION"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def _replace_once(sql, anchor, replacement):
    if sql.count(anchor) != 1:
        raise ValueError(f"check guard anchor moved: {anchor[:60]}")
    return sql.replace(anchor, replacement)


def _requirement(alias):
    return (
        "(SELECT rv.requirement_id FROM vehicle_item_requirement_version rv"
        f" WHERE rv.id={alias}.item_requirement_version_id)"
    )


MISTAKEN_NORMAL = f"""IF NEW.replacement_paper_entry_batch_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM check_record root JOIN command_receipt r ON r.id=NEW.command_id
 JOIN paper_entry_batch b ON b.id=NEW.replacement_paper_entry_batch_id
 WHERE root.id=NEW.root_check_id AND r.command_type='CHECK_INVALIDATION'
 AND root.paper_entry_batch_id IS NULL AND root.purpose='ROUTINE'
 AND root.actor_user_id<>NEW.actor_user_id
 AND b.organization_id=NEW.organization_id AND b.vehicle_id=root.vehicle_id
 AND b.performed_at<=root.recorded_at
 AND NOT EXISTS (SELECT 1 FROM paper_entry_invalidation_event i WHERE i.batch_id=b.id)
 AND EXISTS (SELECT 1 FROM check_record p WHERE p.organization_id=b.organization_id
  AND p.paper_entry_batch_id=b.id AND p.item_id=root.item_id AND p.kit_id<=>root.kit_id
  AND p.kit_requirement_id<=>root.kit_requirement_id AND p.kit_presence=root.kit_presence
  AND {_requirement("p")}<=>{_requirement("root")}
  AND NOT EXISTS (SELECT 1 FROM check_event pe WHERE pe.root_check_id=p.root_check_id
   AND pe.event_type='INVALIDATION')))
THEN {signal("mistaken normal check replacement mismatch")}; END IF;
"""


def event_trigger():
    sql = prior_event._event_trigger()
    sql = _replace_once(
        sql,
        """ AND JSON_CONTAINS(NEW.actor_roles_at_time, '"ADMINISTRATOR"')\n""",
        f""" AND (JSON_CONTAINS(NEW.actor_roles_at_time, '"ADMINISTRATOR"')
  OR ((NEW.replacement_paper_entry_batch_id IS NOT NULL OR r.command_type='{BATCH_COMMAND}')
   AND JSON_CONTAINS(NEW.actor_roles_at_time, '"SUPERVISOR"')))\n""",
    )
    sql = _replace_once(
        sql,
        " AND r.command_type='CHECK_INVALIDATION')\n",
        f""" AND (r.command_type='CHECK_INVALIDATION'
  OR (r.command_type='{BATCH_COMMAND}' AND NEW.replacement_paper_entry_batch_id IS NULL
   AND root.paper_entry_batch_id=JSON_VALUE(r.response_body,'$.batch_id')
   AND EXISTS (SELECT 1 FROM paper_entry_batch b WHERE b.id=root.paper_entry_batch_id
    AND b.actor_user_id<>NEW.actor_user_id))))\n""",
    )
    return _replace_once(
        sql,
        "SET MESSAGE_TEXT = 'check event scope mismatch'; END IF;\nELSE\n",
        "SET MESSAGE_TEXT = 'check event scope mismatch'; END IF;\n" + MISTAKEN_NORMAL + "ELSE\n",
    )


INVALIDATED_BATCH = f"""IF NEW.paper_entry_batch_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM paper_entry_invalidation_event i WHERE i.batch_id=NEW.paper_entry_batch_id)
THEN {signal("paper entry batch is invalidated")}; END IF;
"""


def check_trigger():
    sql = prior_check.check_trigger()
    if not sql.endswith("END"):
        raise ValueError("check trigger no longer ends with END")
    return sql[: -len("END")] + INVALIDATED_BATCH + "END"


def determination_trigger():
    sql = prior_determination.trigger_sql()
    for anchor, replacement in (
        (
            "'SUPERVISOR_COMPLETION_INVALIDATION', 'CHECK_CORRECTION', 'CHECK_INVALIDATION'\n ))",
            "'SUPERVISOR_COMPLETION_INVALIDATION', 'CHECK_CORRECTION', 'CHECK_INVALIDATION',"
            f" '{BATCH_COMMAND}'\n ))",
        ),
        (
            "  AND r.command_type='CHECK_INVALIDATION') AND (",
            f"  AND r.command_type IN ('CHECK_INVALIDATION','{BATCH_COMMAND}')) AND (",
        ),
        (
            " AND r.command_type IN ('CHECK_CORRECTION','CHECK_INVALIDATION')) AND (",
            f" AND r.command_type IN ('CHECK_CORRECTION','CHECK_INVALIDATION','{BATCH_COMMAND}'))"
            " AND (",
        ),
        (
            "  AND ((r.command_type='CHECK_INVALIDATION' AND e.event_type='INVALIDATION'",
            f"  AND ((r.command_type IN ('CHECK_INVALIDATION','{BATCH_COMMAND}')"
            " AND e.event_type='INVALIDATION'",
        ),
    ):
        sql = _replace_once(sql, anchor, replacement)
    return sql


def forward_sql():
    return [
        "ALTER TABLE check_event ADD CONSTRAINT cya_check_event_replacement_batch_org_fk "
        "FOREIGN KEY (organization_id,replacement_paper_entry_batch_id) "
        "REFERENCES paper_entry_batch (organization_id,id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT",
        "DROP TRIGGER cya_check_event_bi",
        event_trigger(),
        "DROP TRIGGER cya_check_record_bi",
        check_trigger(),
        "DROP TRIGGER cya_obligation_determination_bi",
        determination_trigger(),
    ]


def reverse_sql():
    return [
        "DROP TRIGGER cya_obligation_determination_bi",
        prior_determination.trigger_sql(),
        "DROP TRIGGER cya_check_record_bi",
        prior_check.check_trigger(),
        "DROP TRIGGER cya_check_event_bi",
        prior_event._event_trigger(),
        "ALTER TABLE check_event DROP FOREIGN KEY cya_check_event_replacement_batch_org_fk",
        "ALTER TABLE check_event DROP INDEX cya_check_event_replacement_batch_org_fk",
    ]


def refuse_reverse_with_paper_invalidations(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT COUNT(*) FROM check_event"
            " WHERE replacement_paper_entry_batch_id IS NOT NULL)"
            " + (SELECT COUNT(*) FROM check_event e JOIN command_receipt r ON r.id=e.command_id"
            f" WHERE r.command_type='{BATCH_COMMAND}')"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse while paper-entry check invalidations exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checks", "0022_paper_checks"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_paper", "0003_paper_invalidation"),
    ]

    # The reverse guard is the last operation, so on unapply it runs first, while the column
    # still exists; MariaDB DDL is not transactional, so a later refusal would leave it half done.
    operations = [
        migrations.AddField(
            model_name="checkevent",
            name="replacement_paper_entry_batch",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_paper.paperentrybatch",
            ),
        ),
        migrations.AddConstraint(
            model_name="checkevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("replacement_paper_entry_batch__isnull", True),
                    ("event_type", "INVALIDATION"),
                    _connector="OR",
                ),
                name="check_event_replacement_batch_shape",
            ),
        ),
        migrations.RunSQL(forward_sql(), reverse_sql=reverse_sql()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_paper_invalidations),
    ]
