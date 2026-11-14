"""Bind cabinet seal counts to fresh, individually attributed presence checks."""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.seals.migrations.0002_evidence_guards")
START_GUARD = """IF NEW.start_receipt_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM command_receipt r JOIN seal_event e ON e.id=NEW.reseal_id
 WHERE r.id=NEW.start_receipt_id AND r.organization_id=NEW.organization_id
 AND r.actor_user_id=NEW.actor_user_id AND r.command_type='SEAL_INSPECTION_START'
 AND r.recorded_at<=NEW.recorded_at AND e.organization_id=NEW.organization_id
 AND JSON_CONTAINS(r.response_body, JSON_OBJECT('vehicle_id', e.vehicle_id,
  'kit_id', e.kit_id, 'slot_id', e.compartment_slot_id, 'expected_tip_id', e.prior_event_id)))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal inspection start mismatch'; END IF;"""
COUNT_GUARD = """IF NEW.check_record_id IS NULL OR NOT EXISTS
 (SELECT 1 FROM seal_inspection i JOIN command_receipt start ON start.id=i.start_receipt_id
 JOIN seal_event e ON e.id=i.reseal_id JOIN check_record c ON c.id=NEW.check_record_id
 WHERE i.id=NEW.inspection_id AND i.organization_id=NEW.organization_id
 AND start.organization_id=NEW.organization_id AND c.organization_id=NEW.organization_id
 AND c.actor_user_id=NEW.actor_user_id AND c.vehicle_id=e.vehicle_id
 AND c.kit_requirement_id=NEW.kit_requirement_id AND c.quantity=NEW.count
 AND c.outcome IN ('PASSED','FAILED') AND c.recorded_at>=start.recorded_at
 AND c.recorded_at<=NEW.recorded_at
 AND NOT EXISTS (SELECT 1 FROM check_event invalidation
  WHERE invalidation.organization_id=NEW.organization_id AND invalidation.root_check_id=c.root_check_id
  AND invalidation.event_type='INVALIDATION' AND invalidation.recorded_at<=NEW.recorded_at)
 AND NOT EXISTS (SELECT 1 FROM check_record successor
  WHERE successor.organization_id=NEW.organization_id AND successor.predecessor_check_id=c.id
  AND successor.recorded_at<=NEW.recorded_at))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal kit count needs fresh check'; END IF;"""


class Migration(migrations.Migration):
    dependencies = [("cya_seals", "0005_inspection_count_checks")]
    operations = [
        migrations.RunSQL(
            [
                "ALTER TABLE seal_inspection ADD CONSTRAINT cya_seal_inspection_start_org_fk "
                "FOREIGN KEY (organization_id,start_receipt_id) REFERENCES command_receipt "
                "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE seal_inspection_kit_count ADD CONSTRAINT cya_sealkitcount_check_org_fk "
                "FOREIGN KEY (organization_id,check_record_id) REFERENCES check_record "
                "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "DROP TRIGGER cya_seal_inspection_bi",
                previous.insert_guard(
                    "seal_inspection", "'SEAL_RESEAL'", previous.INSPECTION_GUARD + START_GUARD
                ),
                "DROP TRIGGER cya_seal_inspection_kit_count_bi",
                previous.insert_guard(
                    "seal_inspection_kit_count",
                    "'SEAL_RESEAL'",
                    previous.KIT_COUNT_GUARD + COUNT_GUARD,
                ),
            ],
            reverse_sql=[
                "DROP TRIGGER cya_seal_inspection_kit_count_bi",
                previous.insert_guard(
                    "seal_inspection_kit_count", "'SEAL_RESEAL'", previous.KIT_COUNT_GUARD
                ),
                "DROP TRIGGER cya_seal_inspection_bi",
                previous.insert_guard(
                    "seal_inspection", "'SEAL_RESEAL'", previous.INSPECTION_GUARD
                ),
                "ALTER TABLE seal_inspection_kit_count DROP FOREIGN KEY cya_sealkitcount_check_org_fk",
                "ALTER TABLE seal_inspection_kit_count DROP INDEX cya_sealkitcount_check_org_fk",
                "ALTER TABLE seal_inspection DROP FOREIGN KEY cya_seal_inspection_start_org_fk",
                "ALTER TABLE seal_inspection DROP INDEX cya_seal_inspection_start_org_fk",
            ],
        )
    ]
