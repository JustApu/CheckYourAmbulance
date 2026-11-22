"""One check links to every eligible obligation of its item (SPEC 6.0.0 SCH-5, SCH-6; issue #256).

Before 6.0.0 a check had one link unless an administrator's shared-satisfaction rule named the
additional obligation: the link guard (cya_checks 0013's) refused a second link without the current
rule, and the check constraint required every second link to carry one. Now a check links to every
on-time obligation and every late one within its L (SCH-6), so:

- the check constraint keeps only that a primary link (ordinal 0) carries no rule;
- the link guard keeps its scope rule (the check's vehicle, item and kit instance or kit list line,
  and the selected obligation) and the primary-first order, and refuses a rule on a new link;
- the shared-satisfaction rule guard keeps its command receipt check and then refuses every new
  rule (SCH-5: history only).

No row is changed. Links recorded under a rule keep it (§18.1 item 2).
"""

from importlib import import_module

from django.db import migrations, models

previous = import_module("cya.checks.migrations.0013_kit_presence_checks")
rules = import_module("cya.obligations.migrations.0004_shared_satisfaction_rule")


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


HISTORY_ONLY = "shared-satisfaction rules are history only"
SECOND_LINK_OLD = " IF NEW.ordinal>0 AND (NOT EXISTS"
SECOND_LINK_NEW = (
    " IF NEW.ordinal>0 AND NOT EXISTS (SELECT 1 FROM check_obligation_link x "
    "WHERE x.check_id=NEW.check_id AND x.ordinal=0) THEN "
    + signal("primary link must be inserted first")
    + "; END IF; IF NEW.shared_rule_id IS NOT NULL THEN "
    + signal(HISTORY_ONLY)
    + "; END IF; END"
)


def link_trigger():
    """The 0013 link guard with the second-link rule replaced."""
    sql = previous.link_trigger()
    assert sql.count(SECOND_LINK_OLD) == 1 and sql.endswith("END IF; END")
    return sql[: sql.index(SECOND_LINK_OLD)] + SECOND_LINK_NEW


def rule_trigger():
    """The shared-satisfaction rule insert guard as migration 0004 of cya_obligations wrote it."""
    found = [
        statement
        for operation in rules.Migration.operations
        if isinstance(operation, migrations.RunSQL)
        for statement in (
            operation.sql if isinstance(operation.sql, (list, tuple)) else [operation.sql]
        )
        if isinstance(statement, str) and statement.startswith("CREATE TRIGGER cya_shared_rule_bi ")
    ]
    assert len(found) == 1
    return found[0]


def retired_rule_trigger():
    """The 0004 rule guard, keeping its command receipt check, then refusing every new rule."""
    sql = rule_trigger()
    assert sql.endswith(" END IF; END")
    return sql[: -len(" END")] + " " + signal(HISTORY_ONLY) + "; END"


RETIRED_RULE_TRIGGER = retired_rule_trigger()

FORWARD = [
    "DROP TRIGGER cya_check_obligation_link_bi",
    link_trigger(),
    "DROP TRIGGER cya_shared_rule_bi",
    RETIRED_RULE_TRIGGER,
]
REVERSE = [
    "DROP TRIGGER cya_shared_rule_bi",
    rule_trigger(),
    "DROP TRIGGER cya_check_obligation_link_bi",
    previous.link_trigger(),
]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0014_standalone_kit_presence"),
        ("cya_obligations", "0017_vehicle_checklist_assignment_guard"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="checkobligationlink",
            name="check_link_shared_shape",
        ),
        migrations.AddConstraint(
            model_name="checkobligationlink",
            constraint=models.CheckConstraint(
                condition=models.Q(("ordinal", 0), ("shared_rule__isnull", True))
                | models.Q(("ordinal__gt", 0)),
                name="check_link_primary_unshared",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
