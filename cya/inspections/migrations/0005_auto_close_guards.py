"""Session event guard for the auto-close job (SPEC 6.3 SES-1, SES-2, SES-6).

Replaces the 0003 insert trigger on session_event. Everything it checked still holds for a
person's event. An AUTO_INCOMPLETE event is the job's: it needs a service receipt of the right
command type (no actor, no participant check), and its effective instant must equal the last
persisted activity of the session plus six hours, where activity is an OPEN or REOPEN event or a
check recorded in the session (SPEC 6.3 SES-2). Because the database recomputes that instant,
a late job cannot back-date a close past work that came in after it, and a session can only be
auto-closed once it has truly been idle for six hours.

Depends on the check_record session column (cya_checks 0003), which supplies that activity.
"""

from importlib import import_module

from django.db import migrations

old = import_module("cya.inspections.migrations.0003_session_guards")
signal = old.signal

SERVICE_IDENTITY = "worker.session_auto_close"
COMMAND_TYPE = "SESSION_AUTO_CLOSE"
FLOOR = "'1000-01-01 00:00:00.000000'"

AUTO_RECEIPT_MATCH = f"""NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id IS NULL
 AND r.service_identity='{SERVICE_IDENTITY}' AND r.recorded_at=NEW.recorded_at
 AND r.command_type='{COMMAND_TYPE}')"""

AUTO_INSTANT = f"""NEW.effective_at <> (SELECT GREATEST(
 COALESCE((SELECT MAX(a.effective_at) FROM session_event a WHERE a.session_id=NEW.session_id
  AND a.persisted_activity_command_id IS NOT NULL), {FLOOR}),
 COALESCE((SELECT MAX(c.recorded_at) FROM check_record c WHERE c.session_id=NEW.session_id),
  {FLOOR})) + INTERVAL {{hours}} HOUR)"""


def event_insert(hours):
    return f"""CREATE TRIGGER cya_session_event_bi BEFORE INSERT ON session_event
FOR EACH ROW BEGIN
IF NEW.event_type='AUTO_INCOMPLETE' THEN
 IF {AUTO_RECEIPT_MATCH} THEN {signal("session event receipt mismatch")}; END IF;
 IF {AUTO_INSTANT.format(hours=hours)}
 THEN {signal("auto-close instant is not the last activity plus the inactivity limit")}; END IF;
ELSE
 IF {old.EVENT_RECEIPT_MATCH} THEN {signal("session event receipt mismatch")}; END IF;
 IF NOT EXISTS (SELECT 1 FROM session_participant p
  WHERE p.organization_id=NEW.organization_id AND p.session_id=NEW.session_id
  AND p.user_id=NEW.actor_user_id)
 THEN {signal("session actor is not a participant")}; END IF;
END IF;
IF NEW.effective_at > NEW.recorded_at THEN {signal("session event is future dated")}; END IF;
IF NEW.persisted_activity_command_id IS NOT NULL
 AND NEW.persisted_activity_command_id<>NEW.command_id
THEN {signal("session activity receipt mismatch")}; END IF;
IF NEW.sequence=1 THEN
 IF NEW.event_type<>'OPEN' OR NEW.prior_event_id IS NOT NULL OR NOT EXISTS (
  SELECT 1 FROM inspection_session s WHERE s.id=NEW.session_id
  AND s.organization_id=NEW.organization_id AND s.command_id=NEW.command_id
  AND s.opened_at=NEW.effective_at)
 THEN {signal("first session event must open its header")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM session_event p WHERE p.id=NEW.prior_event_id
  AND p.organization_id=NEW.organization_id AND p.session_id=NEW.session_id
  AND p.sequence=NEW.sequence-1 AND p.effective_at<=NEW.effective_at
  AND p.recorded_at<=NEW.recorded_at
  AND ((NEW.event_type IN ('JOIN','END_CLOSED','END_INCOMPLETE','AUTO_INCOMPLETE')
    AND p.event_type IN ('OPEN','JOIN','REOPEN'))
   OR (NEW.event_type='REOPEN' AND p.event_type IN ('END_CLOSED','END_INCOMPLETE',
    'AUTO_INCOMPLETE'))))
 THEN {signal("session event transition mismatch")}; END IF;
END IF;
END"""


def forward_statements():
    # A migration states its own constant: it must not move if the model's changes later.
    return ["DROP TRIGGER cya_session_event_bi", event_insert(6)]


def reverse_statements():
    return ["DROP TRIGGER cya_session_event_bi", old.EVENT_INSERT]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_inspections", "0004_session_auto_close"),
        ("cya_checks", "0003_check_session"),
    ]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
