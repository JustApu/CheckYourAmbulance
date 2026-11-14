"""An intact kit seal substitutes only for its own physical item's obligation."""

from uuid import uuid4

from django.db import transaction

from cya.checklists.models import ChecklistItemDef
from cya.checks.models import CheckRecord
from cya.core.audit_repository import append_receipt
from cya.kits.sealable import designate_kit_sealable
from cya.kits.tests.support import KitCase
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.seals.commands import SealConflict, SealError, verify_seal
from cya.seals.models import (
    SealEvent,
    SealInspection,
    SealInspectionItem,
    SealSubstitutionObligation,
)


class VerifySealTests(KitCase):
    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Synthetic kit").body["kit_id"]
        designate_kit_sealable(kit_id=self.kit_id, sealable=True, **self.context(self.supervisor))
        self.publish_schedule("test-0600")
        version = self.published_version([self.spec(self.kit_item, kit_id=self.kit_id)])
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5))
        with transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.crew,
                command_type="SEAL_RESEAL",
                request_digest="0" * 64,
                response_status=200,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )
            stamp = {
                "organization": self.org,
                "command": receipt,
                "recorded_at": receipt.recorded_at,
                "actor_user": self.crew,
                "actor_roles_at_time": ["CREW"],
            }
            self.seal = SealEvent.objects.create(
                **stamp,
                vehicle=self.vehicle,
                kit_id=self.kit_id,
                sequence=1,
                event_type="RESEAL",
                marking="sample",
                no_expiration=True,
            )
            inspection = SealInspection.objects.create(
                **stamp,
                reseal=self.seal,
                fresh_entry_attestation=True,
                written_marking_attestation=True,
            )
            SealInspectionItem.objects.create(
                **stamp,
                inspection=inspection,
                item=self.kit_item,
                kit_id=self.kit_id,
                instance_key=self.kit_id,
                item_def=ChecklistItemDef.objects.get(
                    checklist_version_id=version, item=self.kit_item
                ),
                outcome="PASSED",
            )

    def test_verification_is_attributed_replayable_and_refreshes_obligations(self):
        context = self.context(self.crew)
        kwargs = {
            "vehicle_id": str(self.vehicle.pk),
            "kit_id": self.kit_id,
            "expected_tip_id": str(self.seal.pk),
            "marking": "sample",
            "attested": True,
        }
        first = verify_seal(**kwargs, **context)
        second = verify_seal(**kwargs, **context)
        self.assertTrue(second.replayed)
        self.assertEqual(first.body, second.body)
        self.assertEqual(first.body["link_count"], 1)
        link = SealSubstitutionObligation.objects.get(substitution_id=first.body["substitution_id"])
        self.assertEqual(str(link.obligation.kit_instance_id), self.kit_id)
        self.assertEqual(link.actor_user_id, self.crew.pk)
        self.assertFalse(CheckRecord.objects.filter(organization=self.org).exists())
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), self.clock.now, organization_id=str(self.org.pk)
        )
        state = next(row for row in states if row.obligation.id == str(link.obligation_id))
        self.assertEqual((state.state, state.outcome), (State.SATISFIED, "SEAL_SUBSTITUTION"))

    def test_wrong_marking_and_stale_tip_are_refused(self):
        kwargs = {
            "vehicle_id": str(self.vehicle.pk),
            "kit_id": self.kit_id,
            "expected_tip_id": str(self.seal.pk),
            "marking": "wrong",
            "attested": True,
        }
        with self.assertRaises(SealError):
            verify_seal(**kwargs, **self.context(self.crew))
        with self.assertRaises(SealConflict):
            verify_seal(
                **(kwargs | {"marking": "sample", "expected_tip_id": None}),
                **self.context(self.crew),
            )
