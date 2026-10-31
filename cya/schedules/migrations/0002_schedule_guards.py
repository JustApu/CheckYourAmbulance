"""Tenant-safe evidence and immutable effective-dated schedule revisions."""

from django.db import migrations

SQL = [
    "ALTER TABLE schedule_rule ADD CONSTRAINT schedule_command_org FOREIGN KEY (organization_id, command_id) REFERENCES command_receipt (organization_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT",
    "ALTER TABLE schedule_rule ADD CONSTRAINT schedule_actor_org FOREIGN KEY (organization_id, actor_user_id) REFERENCES user (organization_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT",
    "CREATE TRIGGER cya_schedule_rule_bu BEFORE UPDATE ON schedule_rule FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'immutable schedule revision'",
    "CREATE TRIGGER cya_schedule_rule_bd BEFORE DELETE ON schedule_rule FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'immutable schedule revision'",
    """CREATE TRIGGER cya_schedule_rule_bi BEFORE INSERT ON schedule_rule FOR EACH ROW
    BEGIN
      IF NEW.revision <> (SELECT COALESCE(MAX(s.revision),0)+1 FROM schedule_rule s
        WHERE s.organization_id=NEW.organization_id AND BINARY s.slot=BINARY NEW.slot)
        OR EXISTS (SELECT 1 FROM schedule_rule s WHERE s.organization_id=NEW.organization_id
          AND BINARY s.slot=BINARY NEW.slot AND s.effective_at >= NEW.effective_at)
      THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'schedule revision order'; END IF;
      IF BINARY NEW.slot NOT REGEXP '^[a-z0-9][a-z0-9-]{0,99}$'
        OR BINARY NEW.id NOT REGEXP '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        OR JSON_TYPE(NEW.actor_roles_at_time) <> 'ARRAY'
        OR JSON_LENGTH(NEW.actor_roles_at_time) > 4
        OR NOT JSON_CONTAINS('["CREW","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]', NEW.actor_roles_at_time)
        OR (NEW.actor_user_id IS NULL AND JSON_LENGTH(NEW.actor_roles_at_time) <> 0)
      THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid schedule identity'; END IF;
      IF BINARY NEW.recurrence NOT IN ('DAILY','WEEKDAY','WEEKEND','INTERVAL')
        OR NEW.wall_time < '00:00:00' OR NEW.wall_time >= '24:00:00'
        OR NEW.early_seconds > 2147483647 OR NEW.grace_seconds > 2147483647
        OR NEW.interval_days > 2147483647
      THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid schedule parameters'; END IF;
      IF NOT EXISTS (SELECT 1 FROM command_receipt c WHERE c.id=NEW.command_id
        AND c.organization_id=NEW.organization_id
        AND c.actor_user_id <=> NEW.actor_user_id
        AND c.recorded_at=NEW.recorded_at)
      THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'schedule evidence mismatch'; END IF;
      IF NOT EXISTS (SELECT 1 FROM organization o WHERE o.id=NEW.organization_id
        AND BINARY o.station_zone=BINARY NEW.zone)
      THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'schedule station zone mismatch'; END IF;
    END""",
]


class Migration(migrations.Migration):
    dependencies = [("cya_schedules", "0001_initial")]
    operations = [
        migrations.RunSQL(sql, reverse_sql=reverse)
        for sql, reverse in zip(
            SQL,
            [
                "ALTER TABLE schedule_rule DROP FOREIGN KEY schedule_command_org",
                "ALTER TABLE schedule_rule DROP FOREIGN KEY schedule_actor_org",
                "DROP TRIGGER cya_schedule_rule_bu",
                "DROP TRIGGER cya_schedule_rule_bd",
                "DROP TRIGGER cya_schedule_rule_bi",
            ],
            strict=True,
        )
    ]
