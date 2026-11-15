"""A fresh kit inspection and reseal commits as attributed, replayable evidence."""

from datetime import timedelta

from cya.accounts.fields import new_id
from cya.deficiencies.models import DeficiencyCategory, DeficiencyEvidence
from cya.deficiencies.severity import ensure_default_severities
from cya.kits.sealable import designate_compartment_sealable, designate_kit_sealable
from cya.kits.tests.support import KitCase
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.seals.commands import break_seal, request_seal_audit, start_inspection, verify_seal
from cya.seals.models import SealEvent, SealInspectionKitVerification, SealInspectionObligation
from cya.seals.reseal import reseal


class ResealTests(KitCase):
    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Synthetic kit").body["kit_id"]
        designate_kit_sealable(kit_id=self.kit_id, sealable=True, **self.context(self.supervisor))
        self.publish_schedule("test-0600")
        self.published_version(
            [self.spec(self.kit_item, kit_id=self.kit_id, kind="QUANTITY", expected_quantity=2)]
        )
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5))

    def test_inspection_start_is_attributed_replayable_and_writes_no_seal_event(self):
        context = self.context(self.crew)
        kwargs = {"vehicle_id": str(self.vehicle.pk), "kit_id": self.kit_id}
        first = start_inspection(**kwargs, **context)
        second = start_inspection(**kwargs, **context)
        self.assertTrue(second.replayed)
        self.assertEqual(first.body["start_operation_id"], context["operation_id"])
        self.assertFalse(SealEvent.objects.filter(kit_id=self.kit_id).exists())

    def test_full_fresh_inspection_is_recorded_once_and_satisfies_one_physical_item(self):
        context = self.context(self.crew)
        kwargs = {
            "vehicle_id": str(self.vehicle.pk),
            "kit_id": self.kit_id,
            "marking": "sample",
            "inspected_attestation": True,
            "written_attestation": True,
            "observations": [
                {
                    "item_id": str(self.kit_item.pk),
                    "kit_id": self.kit_id,
                    "outcome": "PASSED",
                    "count": 2,
                    "expiration": None,
                    "measurement": None,
                }
            ],
        }
        first = reseal(**kwargs, **context)
        second = reseal(**kwargs, **context)
        self.assertTrue(second.replayed)
        self.assertEqual(first.body, second.body)
        self.assertTrue(first.body["no_expiration"])
        event = SealEvent.objects.get(pk=first.body["event_id"])
        self.assertEqual((event.event_type, event.actor_user_id), ("RESEAL", self.crew.pk))
        link = SealInspectionObligation.objects.get(inspection_item__inspection__reseal=event)
        self.assertEqual(str(link.obligation.kit_instance_id), self.kit_id)
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), self.clock.now, organization_id=str(self.org.pk)
        )
        state = next(row for row in states if row.obligation.id == str(link.obligation_id))
        self.assertEqual((state.state, state.outcome), (State.SATISFIED, "PASSED"))

    def test_incomplete_inspection_writes_no_seal(self):
        with self.assertRaises(ValueError):
            reseal(
                vehicle_id=str(self.vehicle.pk),
                kit_id=self.kit_id,
                marking="sample",
                inspected_attestation=True,
                written_attestation=True,
                observations=[],
                **self.context(self.crew),
            )
        self.assertFalse(SealEvent.objects.filter(kit_id=self.kit_id).exists())

    def test_quantity_mismatch_opens_attributed_deficiency_without_blocking_reseal(self):
        effective_at = self.clock_start - timedelta(days=1)
        ensure_default_severities(self.org.pk, effective_at)
        category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=new_id(),
            revision=1,
            label="Synthetic supply",
            supply_related=True,
            effective_at=effective_at,
        )
        result = reseal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            marking="sample",
            inspected_attestation=True,
            written_attestation=True,
            observations=[
                {
                    "item_id": str(self.kit_item.pk),
                    "kit_id": self.kit_id,
                    "outcome": None,
                    "count": 1,
                    "expiration": None,
                    "measurement": None,
                    "deficiency": {
                        "create": {
                            "category_id": str(category.stable_category_id),
                            "severity": "MEDIUM",
                            "summary": "Synthetic shortfall",
                            "description": "One item is missing.",
                        }
                    },
                }
            ],
            **self.context(self.crew),
        )
        self.assertEqual(len(result.body["deficiencies"]), 1)
        evidence = DeficiencyEvidence.objects.get(
            deficiency_id=result.body["deficiencies"][0]["id"]
        )
        self.assertEqual(evidence.seal_inspection_item.outcome, "FAILED")
        self.assertEqual(evidence.actor_user_id, self.crew.pk)

    def test_cabinet_inspection_records_a_fresh_nested_seal_verification(self):
        kit = reseal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            marking="kit-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=[
                {
                    "item_id": str(self.kit_item.pk),
                    "kit_id": self.kit_id,
                    "outcome": "PASSED",
                    "count": 2,
                    "expiration": None,
                    "measurement": None,
                }
            ],
            **self.context(self.crew),
        )
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            **self.context(self.supervisor),
        )
        start_context = self.context(self.crew)
        start_inspection(vehicle_id=str(self.vehicle.pk), slot_id=self.slot, **start_context)
        nested = verify_seal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            marking="kit-id",
            attested=True,
            expected_tip_id=kit.body["event_id"],
            **self.context(self.crew),
        )
        cabinet = reseal(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            marking="cabinet-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=[],
            start_operation_id=start_context["operation_id"],
            nested_substitution_ids=[nested.body["substitution_id"]],
            **self.context(self.crew),
        )
        self.assertTrue(cabinet.body["no_expiration"])
        saved = SealInspectionKitVerification.objects.get(
            inspection_id=cabinet.body["inspection_id"]
        )
        self.assertEqual(str(saved.kit_id), self.kit_id)
        self.assertEqual(str(saved.substitution_id), nested.body["substitution_id"])
        verified = verify_seal(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            marking="cabinet-id",
            attested=True,
            expected_tip_id=cabinet.body["event_id"],
            **self.context(self.crew),
        )
        self.assertEqual(verified.body["code"], "SEAL_SUBSTITUTED")

    def test_pending_cabinet_audit_cannot_be_fulfilled_by_nested_seal_verification(self):
        kit = reseal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            marking="kit-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=[
                {
                    "item_id": str(self.kit_item.pk),
                    "kit_id": self.kit_id,
                    "outcome": "PASSED",
                    "count": 2,
                    "expiration": None,
                    "measurement": None,
                }
            ],
            **self.context(self.crew),
        )
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            **self.context(self.supervisor),
        )
        initial_start = self.context(self.crew)
        start_inspection(vehicle_id=str(self.vehicle.pk), slot_id=self.slot, **initial_start)
        initial_nested = verify_seal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            marking="kit-id",
            attested=True,
            expected_tip_id=kit.body["event_id"],
            **self.context(self.crew),
        )
        cabinet = reseal(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            marking="cabinet-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=[],
            start_operation_id=initial_start["operation_id"],
            nested_substitution_ids=[initial_nested.body["substitution_id"]],
            **self.context(self.crew),
        )
        audit = request_seal_audit(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            expected_tip_id=cabinet.body["event_id"],
            **self.context(self.supervisor),
        )
        broken = break_seal(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            expected_tip_id=audit.body["event_id"],
            reason="Synthetic audit",
            **self.context(self.crew),
        )
        audit_start = self.context(self.crew)
        start_inspection(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            expected_tip_id=broken.body["event_id"],
            **audit_start,
        )
        audit_nested = verify_seal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            marking="kit-id",
            attested=True,
            expected_tip_id=kit.body["event_id"],
            **self.context(self.crew),
        )

        with self.assertRaisesRegex(ValueError, "full-content audit requires inspecting"):
            reseal(
                vehicle_id=str(self.vehicle.pk),
                slot_id=self.slot,
                marking="cabinet-id",
                inspected_attestation=True,
                written_attestation=True,
                observations=[],
                start_operation_id=audit_start["operation_id"],
                nested_substitution_ids=[audit_nested.body["substitution_id"]],
                **self.context(self.crew),
            )

        events = list(
            SealEvent.objects.filter(vehicle_id=self.vehicle.pk, compartment_slot_id=self.slot)
            .order_by("sequence")
            .values_list("event_type", flat=True)
        )
        self.assertEqual(events[-1], "BREAK")
        self.assertNotIn("AUDIT_FULFILLED", events)
