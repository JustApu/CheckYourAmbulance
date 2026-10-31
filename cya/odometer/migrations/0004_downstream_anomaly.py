"""Check downstream readings on correction to enforce confirmation when replacement exceeds next (ODO-3, OQ-58)."""

from importlib import import_module

from django.db import migrations

prior_decisions = import_module("cya.odometer.migrations.0003_reading_decisions")
prior_guards = import_module("cya.odometer.migrations.0002_odometer_guards")


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def reading_insert(table, threshold):
    """The prior is the latest effective tip; a correction has the same author and root.

    A correction whose replacement exceeds the next effective reading in sequence is also anomalous (ODO-3, OQ-58).
    """
    root_event = "root_odometer_id" if table == "odometer_entry" else "root_hours_id"
    limit = (
        f"""SET max_up = IFNULL((SELECT t.max_increase FROM odometer_threshold t
 WHERE t.organization_id=NEW.organization_id AND t.unit=NEW.unit
 ORDER BY t.revision DESC LIMIT 1), {prior_guards.DEFAULTS});
SET anomalous = anomalous OR (NEW.value - prior_value > max_up);"""
        if threshold
        else ""
    )
    return f"""CREATE TRIGGER cya_{table}_bi BEFORE INSERT ON {table} FOR EACH ROW BEGIN
DECLARE prior_value DECIMAL(10,1);
DECLARE prior_unit VARCHAR(2);
DECLARE next_value DECIMAL(10,1);
DECLARE next_unit VARCHAR(2);
DECLARE next_entry VARCHAR(36);
DECLARE max_up DECIMAL(10,1);
DECLARE expected_prior VARCHAR(36);
DECLARE correction_root_recorded_at DATETIME(6);
DECLARE anomalous BOOLEAN DEFAULT 0;
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.operation_id=NEW.operation_id
 AND r.command_type=IF(NEW.predecessor_entry_id IS NULL,'ODOMETER_RECORD','READING_CORRECTION'))
THEN {signal("reading receipt attribution mismatch")}; END IF;
IF NEW.predecessor_entry_id IS NOT NULL THEN
 SELECT root.recorded_at INTO correction_root_recorded_at FROM {table} root
  WHERE root.id=NEW.root_entry_id AND root.root_entry_id=root.id;
END IF;
SET expected_prior = (SELECT e.id FROM {table} e JOIN {table} candidate_root
 ON candidate_root.id=e.root_entry_id WHERE e.organization_id=NEW.organization_id
 AND e.vehicle_id=NEW.vehicle_id
 AND (NEW.predecessor_entry_id IS NULL OR candidate_root.recorded_at<correction_root_recorded_at
  OR (candidate_root.recorded_at=correction_root_recorded_at
   AND candidate_root.id<NEW.root_entry_id))
 AND NOT EXISTS (SELECT 1 FROM {table} c WHERE c.predecessor_entry_id=e.id)
 AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.{root_event}=e.root_entry_id
  AND x.event_type='INVALIDATION')
 ORDER BY candidate_root.recorded_at DESC,candidate_root.id DESC LIMIT 1);
IF NOT (NEW.prior_entry_id <=> expected_prior)
THEN {signal("previous reading is not the latest valid reading")}; END IF;
IF NEW.predecessor_entry_id IS NULL THEN
 IF NEW.root_entry_id<>NEW.id THEN {signal("first reading must be root")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM {table} p JOIN {table} root ON root.id=NEW.root_entry_id
  WHERE p.id=NEW.predecessor_entry_id AND p.root_entry_id=root.id
  AND p.organization_id=NEW.organization_id AND root.organization_id=NEW.organization_id
  AND p.vehicle_id=NEW.vehicle_id AND p.actor_user_id=NEW.actor_user_id
  AND root.actor_user_id=NEW.actor_user_id AND p.unit=NEW.unit AND root.unit=NEW.unit
  AND NOT EXISTS (SELECT 1 FROM {table} c WHERE c.predecessor_entry_id=p.id)
  AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.{root_event}=root.id
   AND x.event_type='INVALIDATION'))
 THEN {signal("reading predecessor scope mismatch")}; END IF;
END IF;
IF NEW.prior_entry_id IS NOT NULL THEN
 SELECT e.value,e.unit INTO prior_value,prior_unit FROM {table} e WHERE e.id=NEW.prior_entry_id;
 IF prior_unit<>NEW.unit THEN {signal("reading unit differs from prior")}; END IF;
 SET anomalous = NEW.value < prior_value;
 {limit}
END IF;
IF NEW.predecessor_entry_id IS NOT NULL THEN
 SET next_entry = (SELECT e.id FROM {table} e JOIN {table} candidate_root
  ON candidate_root.id=e.root_entry_id WHERE e.organization_id=NEW.organization_id
  AND e.vehicle_id=NEW.vehicle_id
  AND (candidate_root.recorded_at>correction_root_recorded_at
   OR (candidate_root.recorded_at=correction_root_recorded_at
    AND candidate_root.id>NEW.root_entry_id))
  AND NOT EXISTS (SELECT 1 FROM {table} c WHERE c.predecessor_entry_id=e.id)
  AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.{root_event}=e.root_entry_id
   AND x.event_type='INVALIDATION')
  ORDER BY candidate_root.recorded_at ASC,candidate_root.id ASC LIMIT 1);
 IF next_entry IS NOT NULL THEN
  SELECT e.value,e.unit INTO next_value,next_unit FROM {table} e WHERE e.id=next_entry;
  IF next_unit<>NEW.unit THEN {signal("reading unit differs from next")}; END IF;
  SET anomalous = anomalous OR (NEW.value > next_value);
 END IF;
END IF;
IF anomalous AND NOT NEW.anomaly_confirmed
THEN {signal("anomalous reading needs confirmation")}; END IF;
IF NEW.anomaly_confirmed AND NOT anomalous
THEN {signal("confirmation without an anomaly")}; END IF;
END"""


def forward_sql():
    statements = []
    for table in ("odometer_entry", "engine_hours_entry"):
        statements += [
            f"DROP TRIGGER cya_{table}_bi",
            reading_insert(table, table == "odometer_entry"),
        ]
    return statements


def reverse_sql():
    statements = []
    for table in ("odometer_entry", "engine_hours_entry"):
        statements += [
            f"DROP TRIGGER cya_{table}_bi",
            prior_decisions.reading_insert(table, table == "odometer_entry"),
        ]
    return statements


class Migration(migrations.Migration):
    dependencies = [
        ("cya_odometer", "0003_reading_decisions"),
    ]

    operations = [
        migrations.RunSQL(forward_sql(), reverse_sql=reverse_sql()),
    ]
