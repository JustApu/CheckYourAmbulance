"""A check may name the inspection session it was made in (issue 78 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) Part B).

AddField creates the nullable column; the statements below add the organization-pair key and
extend the 0002 insert trigger. A check that names a session must: belong to this vehicle and
this checklist version (a session pins both), come from a recorded participant, and arrive while
the session is open and not yet idle for six hours. The database repeats these so that a writer
bypassing the command still cannot extend a session that has already gone idle or close under a
check (the auto-close trigger in cya_inspections 0005 is the other half of the same rule).
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

old = import_module("cya.checks.migrations.0002_check_guards")
signal, ORG = old.signal, old.ORG
FLOOR = "'1000-01-01 00:00:00.000000'"

SESSION_CHECKS = f"""IF NEW.session_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM inspection_session s
 WHERE s.id=NEW.session_id AND s.{ORG}=NEW.{ORG} AND s.vehicle_id=NEW.vehicle_id
 AND s.checklist_version_id=NEW.checklist_version_id)
THEN {signal("check session is not this vehicle and version")}; END IF;
IF NEW.session_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM session_participant p
 WHERE p.session_id=NEW.session_id AND p.{ORG}=NEW.{ORG} AND p.user_id=NEW.actor_user_id)
THEN {signal("check actor is not a participant of the session")}; END IF;
IF NEW.session_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM session_event e
 WHERE e.session_id=NEW.session_id AND e.event_type IN ('OPEN','JOIN','REOPEN')
 AND e.sequence=(SELECT MAX(x.sequence) FROM session_event x WHERE x.session_id=NEW.session_id))
THEN {signal("check session is not open")}; END IF;
IF NEW.session_id IS NOT NULL AND NEW.recorded_at >= (SELECT GREATEST(
 COALESCE((SELECT MAX(a.effective_at) FROM session_event a WHERE a.session_id=NEW.session_id
  AND a.persisted_activity_command_id IS NOT NULL), {FLOOR}),
 COALESCE((SELECT MAX(c.recorded_at) FROM check_record c WHERE c.session_id=NEW.session_id),
  {FLOOR})) + INTERVAL 6 HOUR)
THEN {signal("check session has been idle for the inactivity limit")}; END IF;
"""

CHECK_INSERT = old.CHECK_INSERT.rstrip()
assert CHECK_INSERT.endswith("END")
CHECK_INSERT = CHECK_INSERT[: -len("END")] + SESSION_CHECKS + "END"

FORWARD = [
    f"ALTER TABLE `check_record` ADD CONSTRAINT `cya_check_record_session_id_org_fk` "
    f"FOREIGN KEY ({ORG},session_id) REFERENCES `inspection_session` ({ORG},id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    "DROP TRIGGER cya_check_record_bi",
    CHECK_INSERT,
]
REVERSE = [
    "DROP TRIGGER cya_check_record_bi",
    old.CHECK_INSERT,
    "ALTER TABLE `check_record` DROP FOREIGN KEY `cya_check_record_session_id_org_fk`",
]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0002_check_guards"),
        ("cya_inspections", "0004_session_auto_close"),
    ]

    operations = [
        migrations.AddField(
            model_name="checkrecord",
            name="session",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_inspections.inspectionsession",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
