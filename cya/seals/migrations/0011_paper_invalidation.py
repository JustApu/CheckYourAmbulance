"""Paper seal records of an invalidated batch (SPEC 8.0.0 INV-8, LATE-7; issue #9 Part B5).

Once its paper-entry batch is invalidated, a paper seal substitution supports nothing: obligation
state and linking read it as void from the invalidation, and it is no longer later evidence for
a paper break, so the break guard of seal_event ignores it. A paper break of an invalidated batch
stays in force (the seal stays open until a full inspection and reseal; fail closed), so it is
still later evidence. Neither table accepts a new paper record of an invalidated batch.

The reverse refusal is the last operation, so on unapply it runs first.
"""

from importlib import import_module

from django.db import migrations

guards = import_module("cya.seals.migrations.0002_evidence_guards")
previous = import_module("cya.seals.migrations.0010_paper_seal_records")

INVALIDATED_BATCH = f"""
IF NEW.paper_entry_batch_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM paper_entry_invalidation_event i WHERE i.batch_id=NEW.paper_entry_batch_id)
THEN {guards.signal("paper entry batch is invalidated")}; END IF;"""


def event_guard():
    sql = previous._replace_once(
        previous.event_guard(),
        " AND COALESCE(s.performed_at,s.recorded_at)>NEW.performed_at)",
        " AND COALESCE(s.performed_at,s.recorded_at)>NEW.performed_at\n"
        " AND NOT EXISTS (SELECT 1 FROM paper_entry_invalidation_event i"
        " WHERE i.batch_id=s.paper_entry_batch_id))",
    )
    return sql + INVALIDATED_BATCH


def substitution_guard():
    return previous.substitution_guard() + INVALIDATED_BATCH


REBUILT = (
    ("seal_event", "'SEAL_BREAK','SEAL_RESEAL','SEAL_AUDIT_REQUEST'", event_guard, "event_guard"),
    ("seal_substitution", "'SEAL_SUBSTITUTE'", substitution_guard, "substitution_guard"),
)


def forward_sql():
    statements = []
    for table, types, body, _old in REBUILT:
        statements += [f"DROP TRIGGER cya_{table}_bi", guards.insert_guard(table, types, body())]
    return statements


def reverse_sql():
    statements = []
    for table, types, _body, old in REBUILT:
        statements += [
            f"DROP TRIGGER cya_{table}_bi",
            guards.insert_guard(table, types, getattr(previous, old)()),
        ]
    return statements


def refuse_reverse_with_invalidated_seals(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT COUNT(*) FROM seal_event e JOIN paper_entry_invalidation_event i"
            " ON i.batch_id=e.paper_entry_batch_id)"
            " + (SELECT COUNT(*) FROM seal_substitution s JOIN paper_entry_invalidation_event i"
            " ON i.batch_id=s.paper_entry_batch_id)"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse while invalidated paper seal records exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_seals", "0010_paper_seal_records"),
        ("cya_paper", "0003_paper_invalidation"),
    ]

    operations = [
        migrations.RunSQL(forward_sql(), reverse_sql=reverse_sql()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_invalidated_seals),
    ]
