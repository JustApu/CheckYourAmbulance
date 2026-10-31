"""Enforce command types in vehicle status event and status request insert triggers.

Extends cya_vehicle_status_event_bi and cya_status_request_bi to require that
the referenced command_receipt row has an authorized command_type.
"""

from django.db import migrations

ORG = "organization_id"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


def receipt_predicate(allowed_types):
    types_str = ", ".join(f"'{t}'" for t in allowed_types)
    return (
        "NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id\n"
        f" AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id\n"
        f" AND r.recorded_at=NEW.recorded_at AND r.command_type IN ({types_str}))"
    )


# New triggers
EVENT_INSERT_NEW = f"""CREATE TRIGGER cya_vehicle_status_event_bi BEFORE INSERT ON vehicle_status_event
FOR EACH ROW BEGIN
IF {receipt_predicate(("VEHICLE_STATUS_SET", "STATUS_REQUEST_DECIDE"))} THEN
 {signal("status receipt attribution mismatch")};
END IF;
IF NEW.prior_event_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM vehicle_status_event e WHERE e.id=NEW.prior_event_id AND e.{ORG}=NEW.{ORG}
 AND e.vehicle_id=NEW.vehicle_id AND e.sequence=NEW.sequence-1 AND e.recorded_at<=NEW.recorded_at)
THEN {signal("status chain mismatch")}; END IF;
END"""

REQUEST_INSERT_NEW = f"""CREATE TRIGGER cya_status_request_bi BEFORE INSERT ON status_request
FOR EACH ROW BEGIN
IF {receipt_predicate(("STATUS_REQUEST", "STATUS_REQUEST_DECIDE"))} THEN
 {signal("request receipt attribution mismatch")};
END IF;
IF NEW.request_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM status_request q WHERE q.id=NEW.request_id AND q.{ORG}=NEW.{ORG}
 AND q.vehicle_id=NEW.vehicle_id AND q.action='REQUEST'
 AND q.requested_status=NEW.requested_status AND q.recorded_at<=NEW.recorded_at)
THEN {signal("request decision mismatch")}; END IF;
IF NEW.action='APPROVE' AND NOT EXISTS (
 SELECT 1 FROM vehicle_status_event e WHERE e.command_id=NEW.command_id
 AND e.vehicle_id=NEW.vehicle_id AND e.status=NEW.requested_status)
THEN {signal("approval without matching status event")}; END IF;
END"""

# Old triggers from 0002_status_guards
RECEIPT_MATCH_OLD = """NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at)"""

EVENT_INSERT_OLD = f"""CREATE TRIGGER cya_vehicle_status_event_bi BEFORE INSERT ON vehicle_status_event
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH_OLD} THEN {signal("status receipt attribution mismatch")}; END IF;
IF NEW.prior_event_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM vehicle_status_event e WHERE e.id=NEW.prior_event_id AND e.{ORG}=NEW.{ORG}
 AND e.vehicle_id=NEW.vehicle_id AND e.sequence=NEW.sequence-1 AND e.recorded_at<=NEW.recorded_at)
THEN {signal("status chain mismatch")}; END IF;
END"""

REQUEST_INSERT_OLD = f"""CREATE TRIGGER cya_status_request_bi BEFORE INSERT ON status_request
FOR EACH ROW BEGIN
IF {RECEIPT_MATCH_OLD} THEN {signal("request receipt attribution mismatch")}; END IF;
IF NEW.request_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM status_request q WHERE q.id=NEW.request_id AND q.{ORG}=NEW.{ORG}
 AND q.vehicle_id=NEW.vehicle_id AND q.action='REQUEST'
 AND q.requested_status=NEW.requested_status AND q.recorded_at<=NEW.recorded_at)
THEN {signal("request decision mismatch")}; END IF;
IF NEW.action='APPROVE' AND NOT EXISTS (
 SELECT 1 FROM vehicle_status_event e WHERE e.command_id=NEW.command_id
 AND e.vehicle_id=NEW.vehicle_id AND e.status=NEW.requested_status)
THEN {signal("approval without matching status event")}; END IF;
END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_operations", "0002_status_guards"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TRIGGER IF EXISTS cya_vehicle_status_event_bi",
                "DROP TRIGGER IF EXISTS cya_status_request_bi",
                EVENT_INSERT_NEW,
                REQUEST_INSERT_NEW,
            ],
            reverse_sql=[
                "DROP TRIGGER IF EXISTS cya_vehicle_status_event_bi",
                "DROP TRIGGER IF EXISTS cya_status_request_bi",
                EVENT_INSERT_OLD,
                REQUEST_INSERT_OLD,
            ],
        ),
    ]
