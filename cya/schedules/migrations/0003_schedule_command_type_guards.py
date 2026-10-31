"""Enforce command types in schedule_rule insert trigger.

Extends cya_schedule_rule_bi to require that the referenced command_receipt
has command_type IN ('SCHEDULE_PUBLISH', 'SCHEDULE_SEED').
"""

from django.db import migrations

SCHEDULE_RULE_INSERT_NEW = """CREATE TRIGGER cya_schedule_rule_bi BEFORE INSERT ON schedule_rule FOR EACH ROW
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
        AND c.recorded_at=NEW.recorded_at
        AND c.command_type IN ('SCHEDULE_PUBLISH', 'SCHEDULE_SEED'))
      THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'schedule evidence mismatch'; END IF;
      IF NOT EXISTS (SELECT 1 FROM organization o WHERE o.id=NEW.organization_id
        AND BINARY o.station_zone=BINARY NEW.zone)
      THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'schedule station zone mismatch'; END IF;
    END"""

SCHEDULE_RULE_INSERT_OLD = """CREATE TRIGGER cya_schedule_rule_bi BEFORE INSERT ON schedule_rule FOR EACH ROW
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
    END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_schedules", "0002_schedule_guards"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TRIGGER IF EXISTS cya_schedule_rule_bi",
                SCHEDULE_RULE_INSERT_NEW,
            ],
            reverse_sql=[
                "DROP TRIGGER IF EXISTS cya_schedule_rule_bi",
                SCHEDULE_RULE_INSERT_OLD,
            ],
        ),
    ]
