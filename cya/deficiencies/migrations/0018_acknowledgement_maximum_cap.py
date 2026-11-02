"""Cap the escalation acknowledgement maximum at 30 days (issue #20; owner decision 2026-10-29).

The command validation, the rule trigger and the table CHECK all repeat the cap. Rules are
append-only, so a stored maximum above 30 cannot be corrected in place: the migration refuses
with an explanation instead of letting the CHECK fail without one.
"""

from importlib import import_module

from django.db import migrations, models

ack = import_module("cya.deficiencies.migrations.0010_escalation_acknowledgement")
guards = import_module("cya.deficiencies.migrations.0002_deficiency_guards")

CAP = 30
REPLACEMENTS = (
    ("IF NEW.maximum_days<1 OR ", f"IF NEW.maximum_days<1 OR NEW.maximum_days>{CAP} OR "),
    (
        guards.signal("acknowledgement maximum needs an administrator and positive days"),
        guards.signal(f"acknowledgement maximum needs an administrator and 1 to {CAP} days"),
    ),
)
RULE_INSERT = ack.RULE_INSERT
for old, new in REPLACEMENTS:
    assert RULE_INSERT.count(old) == 1, old
    RULE_INSERT = RULE_INSERT.replace(old, new)


def refuse_stored_maximum_above_cap(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM deficiency_acknowledgement_rule WHERE maximum_days > %s", [CAP]
        )
        (count,) = cursor.fetchone()
    if count:
        raise RuntimeError(
            f"{count} stored acknowledgement maximum(s) exceed {CAP} days. Rules are append-only, "
            "so this database cannot take the cap in place; see docs/deficiencies.md."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0017_check_suspension"),
    ]

    operations = [
        migrations.RunPython(refuse_stored_maximum_above_cap, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="deficiencyacknowledgementrule",
            name="defackrule_max_days",
        ),
        migrations.AddConstraint(
            model_name="deficiencyacknowledgementrule",
            constraint=models.CheckConstraint(
                condition=models.Q(("maximum_days__gte", 1), ("maximum_days__lte", CAP)),
                name="defackrule_max_days",
            ),
        ),
        migrations.RunSQL(
            ["DROP TRIGGER cya_deficiency_acknowledgement_rule_bi", RULE_INSERT],
            reverse_sql=["DROP TRIGGER cya_deficiency_acknowledgement_rule_bi", ack.RULE_INSERT],
        ),
    ]
