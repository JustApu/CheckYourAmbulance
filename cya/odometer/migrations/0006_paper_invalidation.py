"""Paper-entry batch invalidation of readings (SPEC 8.0.0 INV-8, LATE-6, LATE-7; issue #9 B5).

The reading_event insert trigger now also accepts an INVALIDATION appended by a
PAPER_ENTRY_BATCH_INVALIDATION command: by a Supervisor or Administrator (role snapshot), of a
reading chain whose root belongs to the batch named in the receipt's response body, when the
batch's entry actor is someone else. Every other rule of the trigger is unchanged; an
administrator's READING_INVALIDATION is accepted exactly as before.

The odometer and engine-hours insert triggers refuse a paper reading of an invalidated batch.
The reverse refusal is the last operation, so on unapply it runs first.
"""

from importlib import import_module

from django.db import migrations

prior_decisions = import_module("cya.odometer.migrations.0003_reading_decisions")
prior_paper = import_module("cya.odometer.migrations.0005_paper_readings")

BATCH_COMMAND = "PAPER_ENTRY_BATCH_INVALIDATION"
BATCH_OF_RECEIPT = (
    "(SELECT JSON_VALUE(r.response_body,'$.batch_id') FROM command_receipt r"
    " WHERE r.id=NEW.command_id)"
)


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def _replace_once(sql, anchor, replacement):
    if sql.count(anchor) != 1:
        raise ValueError(f"reading guard anchor moved: {anchor[:60]}")
    return sql.replace(anchor, replacement)


def _batch_receipt():
    return (
        "EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id"
        f" AND r.command_type='{BATCH_COMMAND}')"
    )


def event_insert():
    sql = prior_decisions.event_insert()
    sql = _replace_once(
        sql,
        " AND r.command_type=IF(NEW.event_type='CORRECTION','READING_CORRECTION','READING_INVALIDATION'))",
        " AND (r.command_type=IF(NEW.event_type='CORRECTION','READING_CORRECTION',"
        "'READING_INVALIDATION')\n  OR (NEW.event_type='INVALIDATION'"
        f" AND r.command_type='{BATCH_COMMAND}')))",
    )
    sql = _replace_once(
        sql,
        """ (NEW.explanation IS NULL OR NOT JSON_CONTAINS(NEW.actor_roles_at_time,'"ADMINISTRATOR"'))""",
        f""" (NEW.explanation IS NULL OR NOT (JSON_CONTAINS(NEW.actor_roles_at_time,'"ADMINISTRATOR"')
  OR ({_batch_receipt()}
   AND JSON_CONTAINS(NEW.actor_roles_at_time,'"SUPERVISOR"'))))""",
    )
    batch_scope = f"""IF {_batch_receipt()} AND NOT EXISTS (
 SELECT 1 FROM paper_entry_batch b WHERE b.id={BATCH_OF_RECEIPT}
 AND b.organization_id=NEW.organization_id AND b.actor_user_id<>NEW.actor_user_id
 AND (EXISTS (SELECT 1 FROM odometer_entry o WHERE o.id=NEW.root_odometer_id
   AND o.paper_entry_batch_id=b.id)
  OR EXISTS (SELECT 1 FROM engine_hours_entry h WHERE h.id=NEW.root_hours_id
   AND h.paper_entry_batch_id=b.id)))
THEN {signal("reading is not of the invalidated paper entry batch")}; END IF;
"""
    if not sql.endswith("END"):
        raise ValueError("reading event trigger no longer ends with END")
    return sql[: -len("END")] + batch_scope + "END"


INVALIDATED_BATCH = f"""IF NEW.paper_entry_batch_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM paper_entry_invalidation_event i WHERE i.batch_id=NEW.paper_entry_batch_id)
THEN {signal("paper entry batch is invalidated")}; END IF;
"""


def reading_insert(table, threshold):
    sql = prior_paper.reading_insert(table, threshold)
    if not sql.endswith("END"):
        raise ValueError("reading trigger no longer ends with END")
    return sql[: -len("END")] + INVALIDATED_BATCH + "END"


def forward_sql():
    statements = ["DROP TRIGGER cya_reading_event_bi", event_insert()]
    for table in prior_paper.TABLES:
        statements += [
            f"DROP TRIGGER cya_{table}_bi",
            reading_insert(table, table == "odometer_entry"),
        ]
    return statements


def reverse_sql():
    statements = ["DROP TRIGGER cya_reading_event_bi", prior_decisions.event_insert()]
    for table in prior_paper.TABLES:
        statements += [
            f"DROP TRIGGER cya_{table}_bi",
            prior_paper.reading_insert(table, table == "odometer_entry"),
        ]
    return statements


def refuse_reverse_with_batch_invalidations(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM reading_event e JOIN command_receipt r ON r.id=e.command_id"
            f" WHERE r.command_type='{BATCH_COMMAND}'"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse while paper batch reading invalidations exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_odometer", "0005_paper_readings"),
        ("cya_paper", "0003_paper_invalidation"),
    ]

    operations = [
        migrations.RunSQL(forward_sql(), reverse_sql=reverse_sql()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_batch_invalidations),
    ]
