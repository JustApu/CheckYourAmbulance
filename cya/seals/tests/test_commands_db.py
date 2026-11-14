"""Database checks for attributed seal break and audit events."""

from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.kits.sealable import designate_kit_sealable
from cya.kits.tests.support import KitCase
from cya.seals.commands import SealConflict, SealError, break_seal, request_seal_audit
from cya.seals.models import SealEvent


class SealEventCommandTests(KitCase):
    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Synthetic kit").body["kit_id"]
        designate_kit_sealable(kit_id=self.kit_id, sealable=True, **self.context(self.supervisor))
        with transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.supervisor,
                command_type="SEAL_RESEAL",
                request_digest="0" * 64,
                response_status=200,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )
            self.seal = SealEvent.objects.create(
                organization=self.org,
                vehicle=self.vehicle,
                kit_id=self.kit_id,
                sequence=1,
                event_type="RESEAL",
                marking="crew-1",
                no_expiration=True,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.supervisor,
                actor_roles_at_time=["SUPERVISOR"],
            )

    def test_break_is_attributed_and_replays_without_duplicate(self):
        context = self.context(self.crew)
        kwargs = {
            "vehicle_id": str(self.vehicle.pk),
            "kit_id": self.kit_id,
            "expected_tip_id": str(self.seal.pk),
            "reason": "inspect contents",
        }
        with patch("cya.live_updates.publish.publish_vehicle_update") as publish:
            first = break_seal(**kwargs, **context)
            second = break_seal(**kwargs, **context)
        publish.assert_called_once_with(
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle.pk),
            event_id=first.body["event_id"],
            kind="seal.changed",
        )
        self.assertTrue(second.replayed)
        self.assertEqual(first.body, second.body)
        event = SealEvent.objects.get(pk=first.body["event_id"])
        self.assertEqual(
            (event.event_type, event.marking, event.actor_user_id),
            ("BREAK", "crew-1", self.crew.pk),
        )
        with self.assertRaises(SealConflict):
            break_seal(**kwargs, **self.context(self.crew))

    def test_audit_requires_supervisor_and_cannot_be_requested_twice(self):
        kwargs = {
            "vehicle_id": str(self.vehicle.pk),
            "kit_id": self.kit_id,
            "expected_tip_id": str(self.seal.pk),
        }
        with self.assertRaises(CommandDenied):
            request_seal_audit(**kwargs, **self.context(self.crew))
        first = request_seal_audit(**kwargs, **self.context(self.supervisor))
        with self.assertRaises(SealError):
            request_seal_audit(
                **(kwargs | {"expected_tip_id": first.body["event_id"]}),
                **self.context(self.supervisor),
            )

    def test_seal_events_are_append_only(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            SealEvent.objects.filter(pk=self.seal.pk).update(marking="changed")
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.seal.delete()

    def test_audit_cannot_be_requested_after_the_seal_is_already_open(self):
        broken = break_seal(
            vehicle_id=str(self.vehicle.pk),
            kit_id=self.kit_id,
            expected_tip_id=str(self.seal.pk),
            reason="Synthetic inspection",
            **self.context(self.crew),
        )
        with self.assertRaises(SealError):
            request_seal_audit(
                vehicle_id=str(self.vehicle.pk),
                kit_id=self.kit_id,
                expected_tip_id=broken.body["event_id"],
                **self.context(self.supervisor),
            )
        self.assertFalse(
            SealEvent.objects.filter(kit_id=self.kit_id, event_type="AUDIT_REQUEST").exists()
        )
