"""Database safeguards for attributed full-inspection evidence."""

from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.checklists.models import ChecklistItemDef
from cya.core.audit_repository import append_receipt
from cya.kits.sealable import designate_compartment_sealable
from cya.obligations.models import Obligation
from cya.obligations.tests.support import ObligationCase
from cya.seals.models import SealEvent, SealInspection, SealInspectionItem, SealInspectionObligation


class InspectionObligationGuardTests(ObligationCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600")
        version = self.published_version(self.base_items())
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            **self.context(self.supervisor),
        )
        self.clock.set(self.at(hours=5))
        with transaction.atomic():
            self.receipt = append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.crew,
                command_type="SEAL_RESEAL",
                request_digest="0" * 64,
                response_status=200,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )
            evidence = {
                "organization": self.org,
                "command": self.receipt,
                "recorded_at": self.receipt.recorded_at,
                "actor_user": self.crew,
                "actor_roles_at_time": ["CREW"],
            }
            event = SealEvent.objects.create(
                **evidence,
                vehicle=self.vehicle,
                compartment_slot_id=self.cabinet.stable_slot_id,
                sequence=1,
                event_type="RESEAL",
                marking="sample",
                no_expiration=True,
            )
            inspection = SealInspection.objects.create(
                **evidence,
                reseal=event,
                fresh_entry_attestation=True,
                written_marking_attestation=True,
            )
            self.item = SealInspectionItem.objects.create(
                **evidence,
                inspection=inspection,
                item=self.suction,
                instance_key="00000000-0000-0000-0000-000000000000",
                item_def=ChecklistItemDef.objects.get(
                    checklist_version_id=version, item=self.suction
                ),
                outcome="PASSED",
            )
        self.evidence = evidence
        self.obligation = (
            Obligation.objects.filter(
                organization=self.org, vehicle=self.vehicle, item=self.suction
            )
            .order_by("due_at")
            .first()
        )
        self.assertIsNotNone(self.obligation)

    def test_matching_link_is_append_only(self):
        link = SealInspectionObligation.objects.create(
            **self.evidence, inspection_item=self.item, obligation=self.obligation, ordinal=0
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            SealInspectionObligation.objects.filter(pk=link.pk).update(late_eligible=True)
        with self.assertRaises(DatabaseError), transaction.atomic():
            link.delete()

    def test_other_item_and_receipt_are_rejected(self):
        other = (
            Obligation.objects.filter(organization=self.org, vehicle=self.vehicle, item=self.masks)
            .order_by("due_at")
            .first()
        )
        self.assertIsNotNone(other)
        with self.assertRaises(DatabaseError), transaction.atomic():
            SealInspectionObligation.objects.create(
                **self.evidence, inspection_item=self.item, obligation=other, ordinal=0
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            SealInspectionObligation.objects.create(
                **(self.evidence | {"actor_user": self.supervisor}),
                inspection_item=self.item,
                obligation=self.obligation,
                ordinal=0,
            )
