"""Let the check_event guard accept an administrator's INVALIDATION of a chain (issue 77 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/77)).

check_event already allows the INVALIDATION type and an empty replacement (0006); its insert
trigger refused every event but a correction. This replaces that trigger so that:

- an INVALIDATION names a chain (root and selected row of the same chain), selects the chain's
  current tip, carries a non-null explanation, comes from an administrator (the role snapshot
  the command stores) and matches a CHECK_INVALIDATION receipt for the same actor and instant;
- a chain is invalidated at most once (INV-6: no second event, no undo);
- a correction of an already invalidated chain is refused (INV-2a).

Who may invalidate their own chain (OQ-33) is a policy of the command, not repeated here, so the
owner's later decision does not need a migration. Reverse restores the 0006 trigger.
"""

from importlib import import_module

from django.db import migrations

prior = import_module("cya.checks.migrations.0006_check_corrections")


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def _previous_event_trigger():
    return next(
        statement
        for statement in prior._sql()
        if statement.startswith("CREATE TRIGGER cya_check_event_bi")
    )


def _event_trigger():
    previous = _previous_event_trigger()
    body = previous[previous.index("BEGIN\n") + len("BEGIN\n") : -len("END")]
    invalidation = f"""IF NEW.replacement_check_id IS NOT NULL OR NEW.explanation IS NULL
THEN {signal("invalid invalidation event")}; END IF;
IF EXISTS (SELECT 1 FROM check_event e WHERE e.root_check_id=NEW.root_check_id
 AND e.event_type='INVALIDATION')
THEN {signal("check chain is already invalidated")}; END IF;
IF NOT EXISTS (SELECT 1 FROM check_record root JOIN check_record selected
 ON selected.id=NEW.selected_check_id JOIN command_receipt r ON r.id=NEW.command_id
 WHERE root.id=NEW.root_check_id AND root.id=selected.root_check_id
 AND root.organization_id=NEW.organization_id AND selected.organization_id=NEW.organization_id
 AND NOT EXISTS (SELECT 1 FROM check_record c WHERE c.predecessor_check_id=selected.id)
 AND JSON_CONTAINS(NEW.actor_roles_at_time, '"ADMINISTRATOR"')
 AND r.organization_id=NEW.organization_id AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='CHECK_INVALIDATION')
THEN {signal("check event scope mismatch")}; END IF;
"""
    return f"""CREATE TRIGGER cya_check_event_bi BEFORE INSERT ON check_event FOR EACH ROW BEGIN
IF NEW.event_type='CORRECTION' THEN
IF EXISTS (SELECT 1 FROM check_event e WHERE e.root_check_id=NEW.root_check_id
 AND e.event_type='INVALIDATION')
THEN {signal("check chain is invalidated")}; END IF;
{body}ELSEIF NEW.event_type='INVALIDATION' THEN
{invalidation}ELSE
{signal("unsupported check event")};
END IF;
END"""


class Migration(migrations.Migration):
    dependencies = [("cya_checks", "0006_check_corrections")]
    operations = [
        migrations.RunSQL(
            ["DROP TRIGGER cya_check_event_bi", _event_trigger()],
            reverse_sql=["DROP TRIGGER cya_check_event_bi", _previous_event_trigger()],
        )
    ]
