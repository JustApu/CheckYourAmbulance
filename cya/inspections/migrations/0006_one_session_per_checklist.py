"""Database backstop for one session in progress per vehicle checklist (SPEC 6.0.0 SES-7).

Rewrites the 0005 insert trigger on session_event by exact-text replacement of 0005's builder.
Everything it checked still holds. In addition, an OPEN or REOPEN event is refused while another
session of the same vehicle checklist is in progress at the event's recorded instant. A vehicle
checklist is (vehicle, checklist template): its identity, not a version, so a session pinned to
an earlier version counts. In progress means the session's latest event is OPEN, JOIN or REOPEN
and the recorded instant is before its last persisted activity plus six hours (SES-1), where
activity is an OPEN or REOPEN event or a check recorded in the session, as in 0005's auto-close
instant. JOIN and END events are not judged, and sessions already in progress together before
this migration are left as they are: history is kept (SPEC 18.3).

Serialization. Before it checks, the trigger takes the session's vehicle row lock itself
(SELECT ... FOR UPDATE), the same lock every session writer takes first: open, join and reopen
(cya.inspections.commands._vehicle), end (_vehicle), the auto-close job and the close a caller
settles first (_lock_vehicle), and a check recorded in a session (cya.checks.commands._vehicle
with lock=True). The connection runs at READ COMMITTED (settings DATABASES OPTIONS), where each
consistent read takes a fresh snapshot, so once the lock is granted the check sees every session
event committed by the previous holder. Two concurrent OPEN or REOPEN inserts of one vehicle
checklist that skip the commands therefore serialize on the vehicle row (or, when both already
hold the shared lock a session insert's foreign key check takes, one is chosen as the deadlock
victim and rolled back): either way at most one commits. The commands already hold the lock, so
for them the trigger's lock is a no-op; under it they refuse a second session themselves
(SESSION_IN_PROGRESS).
"""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.inspections.migrations.0005_auto_close_guards")
signal = previous.signal

FLOOR = previous.FLOOR

ONE_IN_PROGRESS = f"""IF NEW.event_type IN ('OPEN','REOPEN') THEN
 SELECT v.id INTO @cya_session_vehicle_lock FROM vehicle v
  WHERE v.id=(SELECT s.vehicle_id FROM inspection_session s WHERE s.id=NEW.session_id)
  FOR UPDATE;
 IF EXISTS (
 SELECT 1 FROM inspection_session me
 JOIN checklist_version mv ON mv.id=me.checklist_version_id
 JOIN inspection_session o ON o.organization_id=me.organization_id
  AND o.vehicle_id=me.vehicle_id AND o.id<>me.id
 JOIN checklist_version ov ON ov.id=o.checklist_version_id AND ov.template_id=mv.template_id
 WHERE me.id=NEW.session_id AND me.organization_id=NEW.organization_id
 AND (SELECT e.event_type FROM session_event e WHERE e.session_id=o.id
  ORDER BY e.sequence DESC LIMIT 1) IN ('OPEN','JOIN','REOPEN')
 AND NEW.recorded_at < GREATEST(
  COALESCE((SELECT MAX(a.effective_at) FROM session_event a WHERE a.session_id=o.id
   AND a.persisted_activity_command_id IS NOT NULL), {FLOOR}),
  COALESCE((SELECT MAX(c.recorded_at) FROM check_record c WHERE c.session_id=o.id),
   {FLOOR})) + INTERVAL {{hours}} HOUR)
 THEN {signal("another session of this vehicle checklist is in progress")}; END IF;
END IF;
"""


def event_insert(hours):
    trigger = previous.event_insert(hours)
    marker = "IF NEW.effective_at > NEW.recorded_at THEN"
    assert trigger.count(marker) == 1
    return trigger.replace(marker, ONE_IN_PROGRESS.format(hours=hours) + marker)


def forward_statements():
    # A migration states its own constant: it must not move if the model's changes later.
    return ["DROP TRIGGER cya_session_event_bi", event_insert(6)]


def reverse_statements():
    return ["DROP TRIGGER cya_session_event_bi", previous.event_insert(6)]


class Migration(migrations.Migration):
    dependencies = [("cya_inspections", "0005_auto_close_guards")]
    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
