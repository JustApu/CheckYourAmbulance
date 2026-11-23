"""No check into a session at or after its effective resume deadline (SPEC 6.0.0 SES-8, SES-9).

Rewrites the check_record insert trigger of 0018 by inserting one guard right after the 0003
inactivity guard (its anchor asserted to occur once). A check that names a session is refused at
or after R* = GREATEST(opened_at + resume_limit_hours, A), where A is the session's last persisted
activity as cya_inspections 0005 and 0007 define it (the latest OPEN or REOPEN, or the latest
check in the session). The command refuses first (SESSION_DEADLINE_PASSED); this repeats it for a
writer that skips the command. The reverse restores 0018's text.
"""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.checks.migrations.0018_verification_place")
session_guards = import_module("cya.checks.migrations.0003_check_session")
signal = session_guards.signal
FLOOR = "'1000-01-01 00:00:00.000000'"

IDLE_ANCHOR = f"THEN {signal('check session has been idle for the inactivity limit')}; END IF;\n"

DEADLINE_GUARD = f"""IF NEW.session_id IS NOT NULL AND NEW.recorded_at >= (SELECT GREATEST(
 s.opened_at + INTERVAL s.resume_limit_hours HOUR,
 COALESCE((SELECT MAX(a.effective_at) FROM session_event a WHERE a.session_id=NEW.session_id
  AND a.persisted_activity_command_id IS NOT NULL), {FLOOR}),
 COALESCE((SELECT MAX(c.recorded_at) FROM check_record c WHERE c.session_id=NEW.session_id),
  {FLOOR})) FROM inspection_session s WHERE s.id=NEW.session_id)
THEN {signal("check session has passed its resume deadline")}; END IF;
"""


def check_trigger():
    sql = previous.check_trigger()
    if sql.count(IDLE_ANCHOR) != 1:
        raise ValueError("check session inactivity guard text moved")
    return sql.replace(IDLE_ANCHOR, IDLE_ANCHOR + DEADLINE_GUARD)


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0018_verification_place"),
        ("cya_inspections", "0007_session_resume_limit"),
    ]

    operations = [
        migrations.RunSQL(
            ["DROP TRIGGER cya_check_record_bi", check_trigger()],
            reverse_sql=["DROP TRIGGER cya_check_record_bi", previous.check_trigger()],
        )
    ]
