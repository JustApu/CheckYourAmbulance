"""Database guards on kit_assignment_event: append-only, receipt tie, chain, placement."""

from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.accounts.models import Organization
from cya.core.audit_repository import append_receipt
from cya.kits.models import KitAssignmentEvent
from cya.kits.tests.support import KitCase


class GuardTests(KitCase):
    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Kit A").body["kit_id"]
        self.first = KitAssignmentEvent.objects.get(kit_id=self.kit_id)

    def refused(self, operation):
        with self.assertRaises(DatabaseError), transaction.atomic():
            operation()

    def receipt(self, command_type="KIT_ASSIGNMENT_RECORD", actor=None):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=actor or self.supervisor,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=200,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def insert(self, receipt=None, **overrides):
        """The next event of the kit, valid unless an override breaks one rule."""
        receipt = receipt or self.receipt()
        fields = {
            "organization_id": self.org.pk,
            "kit_id": self.kit_id,
            "sequence": 2,
            "prior_event": self.first,
            "vehicle": self.vehicle_b,
            "compartment": self.cabinet_b,
            "active": True,
            "effective_at": receipt.recorded_at,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.supervisor,
            "actor_roles_at_time": ["SUPERVISOR"],
            **overrides,
        }
        return KitAssignmentEvent.objects.create(**fields)

    def test_a_valid_next_event_is_accepted_so_the_refusals_below_are_meaningful(self):
        self.insert()
        self.assertEqual(KitAssignmentEvent.objects.filter(kit_id=self.kit_id).count(), 2)

    def test_the_table_is_append_only(self):
        rows = KitAssignmentEvent.objects.filter(pk=self.first.pk)
        self.refused(lambda: rows.update(active=False))
        self.refused(rows.delete)
        self.assertTrue(KitAssignmentEvent.objects.get(pk=self.first.pk).active)

    def test_a_row_must_match_its_receipt_and_be_a_kit_command(self):
        self.refused(lambda: self.insert(receipt=self.receipt(command_type="ROLE_CHANGE")))
        self.refused(lambda: self.insert(actor_user=self.admin))
        self.refused(lambda: self.insert(recorded_at=self.first.recorded_at))
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        self.refused(lambda: self.insert(organization_id=other.pk))
        self.insert(receipt=self.receipt(command_type="KIT_ADD"))  # either kit command type

    def test_the_chain_is_consecutive_per_kit_and_strictly_later(self):
        self.refused(lambda: self.insert(sequence=3))  # gap
        self.refused(lambda: self.insert(sequence=1, prior_event=None))  # a second "first"
        self.refused(lambda: self.insert(prior_event=None))  # sequence 2 with no predecessor
        other_kit = self.add_kit("Kit B").body["kit_id"]
        theirs = KitAssignmentEvent.objects.get(kit_id=other_kit)
        self.refused(lambda: self.insert(prior_event=theirs))  # another kit's event
        receipt = self.receipt()
        self.refused(
            lambda: self.insert(
                receipt=receipt,
                effective_at=self.first.effective_at,
                recorded_at=receipt.recorded_at,
            )
        )  # not strictly later
        self.insert()
        self.refused(lambda: self.insert(sequence=3, prior_event=self.first))  # fork

    def test_an_assignment_is_never_backdated(self):
        receipt = self.receipt()
        self.refused(
            lambda: self.insert(
                receipt=receipt, effective_at=receipt.recorded_at - timedelta(seconds=1)
            )
        )

    def test_placement_rules(self):
        self.refused(lambda: self.insert(compartment=None))  # active without a compartment
        self.refused(lambda: self.insert(vehicle=None))  # active without a vehicle
        self.refused(lambda: self.insert(active=False))  # deactivated but still placed
        self.refused(
            lambda: self.insert(compartment=self.cabinet)
        )  # compartment of vehicle A, kit goes to B
        self.insert(active=False, vehicle=None, compartment=None)
