"""Paper-entered odometer and engine-hours readings (SPEC 8.0.0 ODO-6, LATE-5, LATE-7; issue #9 B3).

odometer_entry and engine_hours_entry gain paper_entry_batch_id (an organization-pair reference)
and performed_at, both null for a live reading. A reading's place in its vehicle's sequence is
now the key (COALESCE(root.performed_at, root.recorded_at), root.recorded_at, root.id): a live
reading's key is its root's recorded instant and id, exactly as before, and a paper reading
sits where its performance instant puts it. The insert trigger:

- computes the expected previous reading, and the next one, by that key, for a new reading and
  for a correction alike (a correction keeps its root's place);
- judges an anomaly against both neighbours (ODO-6): lower than the previous one or more than
  the threshold above it, and, against the next one, higher than it (any reading) or more than
  the threshold below it (a new reading). For a live reading nothing is after it, so only the
  previous-reading rule applies, as before; a correction keeps its existing next-reading rule;
- requires a paper row to match its batch (vehicle, entry actor, performance instant, not
  entered before the batch) and to be no later than its entry instant; a correction keeps its
  chain's batch and instant.

The odometer link trigger judges the early window at the reading's qualifying time and refuses
a paper link past the obligation's late limit (SCH-2). Reversal is refused once a paper reading
exists.
"""

from importlib import import_module

from django.db import migrations, models
from django.db.models.deletion import PROTECT

prior_downstream = import_module("cya.odometer.migrations.0004_downstream_anomaly")
prior_decisions = import_module("cya.odometer.migrations.0003_reading_decisions")
prior_guards = import_module("cya.odometer.migrations.0002_odometer_guards")

TABLES = ("odometer_entry", "engine_hours_entry")


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def _before(alias):
    """`alias` (a root row) comes before the key (key_at, key_rec, key_id) in the sequence."""
    at = f"COALESCE({alias}.performed_at,{alias}.recorded_at)"
    return (
        f"({at}<key_at OR ({at}=key_at AND {alias}.recorded_at<key_rec)"
        f" OR ({at}=key_at AND {alias}.recorded_at=key_rec AND {alias}.id<key_id))"
    )


def _after(alias):
    at = f"COALESCE({alias}.performed_at,{alias}.recorded_at)"
    return (
        f"({at}>key_at OR ({at}=key_at AND {alias}.recorded_at>key_rec)"
        f" OR ({at}=key_at AND {alias}.recorded_at=key_rec AND {alias}.id>key_id))"
    )


def _neighbour(table, root_event, direction):
    order = "DESC" if direction == "before" else "ASC"
    place = _before("candidate_root") if direction == "before" else _after("candidate_root")
    return f"""(SELECT e.id FROM {table} e JOIN {table} candidate_root
 ON candidate_root.id=e.root_entry_id WHERE e.organization_id=NEW.organization_id
 AND e.vehicle_id=NEW.vehicle_id AND {place}
 AND NOT EXISTS (SELECT 1 FROM {table} c WHERE c.predecessor_entry_id=e.id)
 AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.{root_event}=e.root_entry_id
  AND x.event_type='INVALIDATION')
 ORDER BY COALESCE(candidate_root.performed_at,candidate_root.recorded_at) {order},
  candidate_root.recorded_at {order},candidate_root.id {order} LIMIT 1)"""


def reading_insert(table, threshold):
    root_event = "root_odometer_id" if table == "odometer_entry" else "root_hours_id"
    max_up = (
        f"""SET max_up = IFNULL((SELECT t.max_increase FROM odometer_threshold t
 WHERE t.organization_id=NEW.organization_id AND t.unit=NEW.unit
 ORDER BY t.revision DESC LIMIT 1), {prior_guards.DEFAULTS});"""
        if threshold
        else "SET max_up = NULL;"
    )
    return f"""CREATE TRIGGER cya_{table}_bi BEFORE INSERT ON {table} FOR EACH ROW BEGIN
DECLARE prior_value DECIMAL(10,1);
DECLARE prior_unit VARCHAR(2);
DECLARE next_value DECIMAL(10,1);
DECLARE next_unit VARCHAR(2);
DECLARE next_entry VARCHAR(36);
DECLARE max_up DECIMAL(10,1);
DECLARE expected_prior VARCHAR(36);
DECLARE key_at DATETIME(6);
DECLARE key_rec DATETIME(6);
DECLARE key_id VARCHAR(36);
DECLARE anomalous BOOLEAN DEFAULT 0;
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.operation_id=NEW.operation_id
 AND r.command_type=IF(NEW.predecessor_entry_id IS NULL,'ODOMETER_RECORD','READING_CORRECTION'))
THEN {signal("reading receipt attribution mismatch")}; END IF;
IF (NEW.paper_entry_batch_id IS NULL) <> (NEW.performed_at IS NULL)
THEN {signal("paper reading needs its batch and performance time")}; END IF;
IF NEW.predecessor_entry_id IS NULL AND NEW.paper_entry_batch_id IS NOT NULL AND (
 NEW.performed_at>NEW.recorded_at
 OR NOT EXISTS (SELECT 1 FROM paper_entry_batch b WHERE b.id=NEW.paper_entry_batch_id
  AND b.organization_id=NEW.organization_id AND b.vehicle_id=NEW.vehicle_id
  AND b.actor_user_id=NEW.actor_user_id AND b.performed_at=NEW.performed_at
  AND b.recorded_at<=NEW.recorded_at))
THEN {signal("paper reading does not match its batch")}; END IF;
IF NEW.predecessor_entry_id IS NOT NULL THEN
 SELECT COALESCE(root.performed_at,root.recorded_at),root.recorded_at INTO key_at,key_rec
  FROM {table} root WHERE root.id=NEW.root_entry_id AND root.root_entry_id=root.id;
 SET key_id = NEW.root_entry_id;
ELSE
 SET key_at = COALESCE(NEW.performed_at,NEW.recorded_at);
 SET key_rec = NEW.recorded_at;
 SET key_id = NEW.id;
END IF;
SET expected_prior = {_neighbour(table, root_event, "before")};
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
  AND p.paper_entry_batch_id<=>NEW.paper_entry_batch_id AND p.performed_at<=>NEW.performed_at
  AND NOT EXISTS (SELECT 1 FROM {table} c WHERE c.predecessor_entry_id=p.id)
  AND NOT EXISTS (SELECT 1 FROM reading_event x WHERE x.{root_event}=root.id
   AND x.event_type='INVALIDATION'))
 THEN {signal("reading predecessor scope mismatch")}; END IF;
END IF;
{max_up}
IF NEW.prior_entry_id IS NOT NULL THEN
 SELECT e.value,e.unit INTO prior_value,prior_unit FROM {table} e WHERE e.id=NEW.prior_entry_id;
 IF prior_unit<>NEW.unit THEN {signal("reading unit differs from prior")}; END IF;
 SET anomalous = NEW.value < prior_value OR IFNULL(NEW.value - prior_value > max_up, 0);
END IF;
SET next_entry = {_neighbour(table, root_event, "after")};
IF next_entry IS NOT NULL THEN
 SELECT e.value,e.unit INTO next_value,next_unit FROM {table} e WHERE e.id=next_entry;
 IF next_unit<>NEW.unit THEN {signal("reading unit differs from next")}; END IF;
 SET anomalous = anomalous OR (NEW.value > next_value);
 IF NEW.predecessor_entry_id IS NULL THEN
  SET anomalous = anomalous OR IFNULL(next_value - NEW.value > max_up, 0);
 END IF;
END IF;
IF anomalous AND NOT NEW.anomaly_confirmed
THEN {signal("anomalous reading needs confirmation")}; END IF;
IF NEW.anomaly_confirmed AND NOT anomalous
THEN {signal("confirmation without an anomaly")}; END IF;
END"""


LINK_WINDOW = "AND e.recorded_at=NEW.recorded_at AND o.early_at<=e.recorded_at)"


def link_insert():
    existing = prior_decisions.link_insert()
    if existing.count(LINK_WINDOW) != 1:
        raise ValueError("odometer link window text moved")
    return existing.replace(
        LINK_WINDOW,
        "AND e.recorded_at=NEW.recorded_at AND o.early_at<=COALESCE(e.performed_at,e.recorded_at)"
        " AND (e.performed_at IS NULL OR e.performed_at<=o.late_limit_at))",
    )


def forward_sql():
    statements = []
    for table in TABLES:
        statements += [
            f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_paper_batch_org_fk` "
            "FOREIGN KEY (organization_id,paper_entry_batch_id) REFERENCES paper_entry_batch "
            "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
            f"DROP TRIGGER cya_{table}_bi",
            reading_insert(table, table == "odometer_entry"),
        ]
    return [*statements, "DROP TRIGGER cya_odometer_obligation_link_bi", link_insert()]


def reverse_sql():
    statements = ["DROP TRIGGER cya_odometer_obligation_link_bi", prior_decisions.link_insert()]
    for table in TABLES:
        statements += [
            f"DROP TRIGGER cya_{table}_bi",
            prior_downstream.reading_insert(table, table == "odometer_entry"),
            f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_paper_batch_org_fk`",
            f"ALTER TABLE `{table}` DROP INDEX `cya_{table}_paper_batch_org_fk`",
        ]
    return statements


def refuse_reverse_with_paper_readings(apps, schema_editor):
    for name in ("OdometerEntry", "EngineHoursEntry"):
        if (
            apps.get_model("cya_odometer", name)
            .objects.filter(paper_entry_batch__isnull=False)
            .exists()
        ):
            raise RuntimeError(
                "cannot reverse paper readings while append-only paper readings exist"
            )


def _fields(model_name):
    return [
        migrations.AddField(
            model_name=model_name,
            name="paper_entry_batch",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=PROTECT,
                related_name="+",
                to="cya_paper.paperentrybatch",
            ),
        ),
        migrations.AddField(
            model_name=model_name, name="performed_at", field=models.DateTimeField(null=True)
        ),
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_odometer", "0004_downstream_anomaly"),
        ("cya_paper", "0002_paper_entry_guards"),
    ]

    # The reverse guard is last, so on unapply it runs first, while the columns still exist.
    operations = [
        *_fields("odometerentry"),
        *_fields("enginehoursentry"),
        migrations.RunSQL(forward_sql(), reverse_sql=reverse_sql()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_paper_readings),
    ]
