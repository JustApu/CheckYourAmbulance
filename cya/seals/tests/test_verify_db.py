"""An intact kit seal substitutes only for its own physical item's obligation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from django.db import connection, transaction

from cya.checklists.models import ChecklistItemDef
from cya.checks.models import CheckRecord
from cya.core.audit_repository import append_receipt
from cya.kits.sealable import designate_kit_sealable
from cya.kits.tests.support import KitCase
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.seals.commands import SealConflict, SealError, break_seal, verify_seal
from cya.seals.models import (
    SealEvent,
    SealInspection,
    SealInspectionItem,
    SealSubstitution,
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

    def test_concurrent_breaks_of_the_same_tip_have_one_winner(self):
        barrier = Barrier(2)
        contexts = [self.context(self.crew), self.context(self.supervisor)]

        def attempt(context):
            try:
                barrier.wait(timeout=10)
                return break_seal(
                    vehicle_id=str(self.vehicle.pk),
                    kit_id=self.kit_id,
                    expected_tip_id=str(self.seal.pk),
                    reason="Synthetic inspection",
                    **context,
                )
            except SealConflict as error:
                return error
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, contexts))
        self.assertEqual(sum(isinstance(result, SealConflict) for result in outcomes), 1)
        events = list(SealEvent.objects.filter(kit_id=self.kit_id).order_by("sequence"))
        self.assertEqual([event.event_type for event in events], ["RESEAL", "BREAK"])
        self.assertEqual(events[1].prior_event_id, events[0].pk)

    def test_concurrent_verification_never_commits_after_a_break(self):
        barrier = Barrier(2)
        contexts = [self.context(self.crew), self.context(self.supervisor)]

        def attempt(index):
            try:
                barrier.wait(timeout=10)
                common = {
                    "vehicle_id": str(self.vehicle.pk),
                    "kit_id": self.kit_id,
                    "expected_tip_id": str(self.seal.pk),
                }
                if index:
                    return break_seal(reason="Synthetic inspection", **common, **contexts[index])
                return verify_seal(marking="sample", attested=True, **common, **contexts[index])
            except SealConflict as error:
                return error
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            verify_result, break_result = list(pool.map(attempt, range(2)))
        self.assertNotIsInstance(break_result, SealConflict)
        broken = SealEvent.objects.get(kit_id=self.kit_id, event_type="BREAK")
        substitutions = SealSubstitution.objects.filter(seal_event=self.seal)
        if isinstance(verify_result, SealConflict):
            self.assertFalse(substitutions.exists())
        else:
            self.assertEqual(substitutions.count(), 1)
            self.assertLess(substitutions.get().recorded_at, broken.recorded_at)
