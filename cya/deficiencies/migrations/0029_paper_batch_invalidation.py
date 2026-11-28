"""Deficiency effects of a paper-entry batch invalidation (SPEC 8.0.0 INV-8, LATE-6; issue #9 B5).

Invalidating a batch invalidates each of its check chains with the ordinary consequences: INV-3
leaves every deficiency open, and INV-7 reopens a deficiency that a chain's Failed-to-Passed
correction had put in Reported in Error. The deficiency_event insert trigger therefore accepts a
PAPER_ENTRY_BATCH_INVALIDATION receipt where it accepts CHECK_INVALIDATION for a REOPENED event;
the event must still follow that chain's cancellation and name a chain the same command
invalidated. Nothing else changes. The reverse refusal is the last operation.
"""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.deficiencies.migrations.0027_item_requirement_place_guards")

BATCH_COMMAND = "PAPER_ENTRY_BATCH_INVALIDATION"


def _replace_once(sql, anchor, replacement):
    if sql.count(anchor) != 1:
        raise ValueError(f"deficiency event guard anchor moved: {anchor[:60]}")
    return sql.replace(anchor, replacement)


def event_insert():
    sql = previous.event_insert()
    sql = _replace_once(
        sql,
        "'CHECK_CORRECTION','CHECK_INVALIDATION','DEFICIENCY_REGRADE'",
        f"'CHECK_CORRECTION','CHECK_INVALIDATION','{BATCH_COMMAND}','DEFICIENCY_REGRADE'",
    )
    return _replace_once(
        sql,
        "IF NEW.event_type='REOPENED' AND NOT EXISTS (SELECT 1 FROM command_receipt r"
        " WHERE r.id=NEW.command_id AND r.command_type IN ('CHECK_INVALIDATION'))",
        "IF NEW.event_type='REOPENED' AND NOT EXISTS (SELECT 1 FROM command_receipt r"
        f" WHERE r.id=NEW.command_id AND r.command_type IN ('CHECK_INVALIDATION','{BATCH_COMMAND}'))",
    )


def refuse_reverse_with_batch_effects(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM deficiency_event e JOIN command_receipt r ON r.id=e.command_id"
            f" WHERE r.command_type='{BATCH_COMMAND}'"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse while paper batch deficiency effects exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_deficiencies", "0028_physical_kit_presence_scope"),
        ("cya_paper", "0003_paper_invalidation"),
    ]

    operations = [
        migrations.RunSQL(
            ["DROP TRIGGER cya_deficiency_event_bi", event_insert()],
            reverse_sql=["DROP TRIGGER cya_deficiency_event_bi", previous.event_insert()],
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_batch_effects),
    ]
