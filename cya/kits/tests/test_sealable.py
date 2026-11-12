"""Sealable designations for compartments and kit instances (issue #131 slice 1; SPEC 10.1, 10.2)."""

from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction
from django.utils import timezone

from cya.checklists.models import Compartment
from cya.checklists.overlay import vehicle_slots
from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.kits.models import KitSealDesignation
from cya.kits.sealable import (
    SealableError,
    designate_compartment_sealable,
    designate_kit_sealable,
    kit_sealable,
)
from cya.kits.tests.support import KitCase


class CompartmentSealableTests(KitCase):
    def designate(self, sealable=True, actor=None, compartment=None, **over):
        return designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str((compartment or self.cabinet).pk),
            sealable=sealable,
            **{**self.context(actor or self.supervisor), **over},
        )

    def revisions(self):
        return list(
            Compartment.objects.filter(
                vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
            ).order_by("revision")
        )

    def in_force(self, at):
        return vehicle_slots("compartment", self.vehicle.pk, [self.cabinet.pk], at)[self.cabinet.pk]

    def test_a_designation_appends_a_revision_and_keeps_the_old_one(self):
        self.assertFalse(self.cabinet.sealable)  # existing rows default to not sealable
        result = self.designate()
        old, new = self.revisions()
        self.assertEqual(result.body["code"], "COMPARTMENT_SEALABLE_DESIGNATED")
        self.assertEqual(result.body["compartment_id"], str(new.pk))
        self.assertEqual((old.pk, old.sealable), (self.cabinet.pk, False))
        self.assertEqual(
            (new.revision, new.label, new.sealable), (old.revision + 1, old.label, True)
        )
        self.assertEqual(new.stable_slot_id, old.stable_slot_id)
        self.assertEqual(self.in_force(timezone.now()).pk, new.pk)

        self.designate(sealable=False, compartment=new)  # any revision of the slot names it
        self.assertEqual([r.sealable for r in self.revisions()], [False, True, False])

    def test_a_future_designation_takes_effect_at_its_instant(self):
        later = timezone.now() + timedelta(days=2)
        self.designate(effective_at=later)
        self.assertFalse(self.in_force(later - timedelta(seconds=1)).sealable)
        self.assertTrue(self.in_force(later).sealable)

    def test_backdated_repeated_and_out_of_order_designations_are_refused(self):
        with self.assertRaises(SealableError):
            self.designate(effective_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(len(self.revisions()), 1)
        with self.assertRaises(SealableError):
            self.designate(sealable=False)  # already not sealable
        self.designate(effective_at=timezone.now() + timedelta(days=3))
        with self.assertRaises(SealableError):  # a later revision is already scheduled
            self.designate(sealable=False, effective_at=timezone.now() + timedelta(days=1))
        self.assertEqual(len(self.revisions()), 2)

    def test_malformed_input_and_other_vehicles_compartments_are_refused(self):
        for label, over in (
            ("sealable not a bool", {"sealable": "yes"}),
            ("naive instant", {"effective_at": timezone.now().replace(tzinfo=None)}),
        ):
            with self.subTest(label), self.assertRaises(SealableError):
                designate_compartment_sealable(
                    vehicle_id=str(self.vehicle.pk),
                    compartment_id=str(self.cabinet.pk),
                    **{"sealable": True, **over},
                    **self.context(self.supervisor),
                )
        with self.assertRaises(SealableError):
            self.designate(compartment=self.cabinet_b)  # vehicle B's row of the slot
        with self.assertRaises(ValueError):
            designate_compartment_sealable(
                vehicle_id="not-a-uuid",
                compartment_id=str(self.cabinet.pk),
                sealable=True,
                **self.context(self.supervisor),
            )

    def test_only_supervisors_and_administrators_may_designate(self):
        for actor in (self.crew, self.officer):
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.designate(actor=actor)
        self.assertEqual(len(self.revisions()), 1)
        self.designate(actor=self.admin)
        self.assertEqual(len(self.revisions()), 2)

    def test_a_replayed_operation_returns_the_original_result_once(self):
        context = self.context(self.supervisor)
        first = self.designate(**context)
        again = self.designate(**context)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(len(self.revisions()), 2)

    def test_compartment_revisions_stay_immutable(self):
        self.designate()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Compartment.objects.filter(pk=self.cabinet.pk).update(sealable=True)


class KitSealableTests(KitCase):
    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Kit A").body["kit_id"]

    def designate(self, sealable=True, actor=None, **over):
        return designate_kit_sealable(
            kit_id=self.kit_id,
            sealable=sealable,
            **{**self.context(actor or self.supervisor), **over},
        )

    def test_designations_form_a_chain_and_answer_as_of_an_instant(self):
        before = timezone.now()
        self.assertEqual(kit_sealable(str(self.org.pk), [self.kit_id], before), {})
        first = self.designate()
        between = timezone.now()
        second = self.designate(sealable=False)
        rows = list(KitSealDesignation.objects.filter(kit_id=self.kit_id).order_by("sequence"))
        self.assertEqual([(r.sequence, r.sealable) for r in rows], [(1, True), (2, False)])
        self.assertIsNone(rows[0].prior_designation_id)
        self.assertEqual(rows[1].prior_designation_id, rows[0].pk)
        self.assertEqual(first.body["code"], "KIT_SEAL_DESIGNATED")
        self.assertEqual(second.body["sequence"], 2)
        self.assertEqual(
            kit_sealable(str(self.org.pk), [self.kit_id], between), {self.kit_id: True}
        )
        self.assertEqual(kit_sealable(str(self.org.pk), [self.kit_id], timezone.now()), {})
        self.assertEqual(rows[0].actor_user_id, self.supervisor.pk)
        self.assertEqual(rows[0].effective_at, rows[0].recorded_at)

    def test_repeated_unknown_and_malformed_designations_are_refused(self):
        with self.assertRaises(SealableError):
            self.designate(sealable=False)  # no row yet means not sealed
        with self.assertRaises(SealableError):
            designate_kit_sealable(
                kit_id=str(uuid4()), sealable=True, **self.context(self.supervisor)
            )
        with self.assertRaises(SealableError):
            self.designate(sealable=1)
        self.designate()
        with self.assertRaises(SealableError):
            self.designate()
        self.assertEqual(KitSealDesignation.objects.filter(kit_id=self.kit_id).count(), 1)

    def test_only_supervisors_and_administrators_may_designate(self):
        for actor in (self.crew, self.officer):
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.designate(actor=actor)
        self.designate(actor=self.admin)
        self.assertEqual(KitSealDesignation.objects.filter(kit_id=self.kit_id).count(), 1)

    def test_lookups_are_bounded_and_scoped_to_the_organization(self):
        self.designate()
        with self.assertRaises(ValueError):
            kit_sealable(str(self.org.pk), [str(uuid4()) for _ in range(2001)], timezone.now())
        self.assertEqual(kit_sealable(str(uuid4()), [self.kit_id], timezone.now()), {})


class KitSealGuardTests(KitCase):
    """The table refuses rows the command would never write, and is append-only."""

    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Kit A").body["kit_id"]
        designate_kit_sealable(kit_id=self.kit_id, sealable=True, **self.context(self.supervisor))
        self.first = KitSealDesignation.objects.get(kit_id=self.kit_id)

    def refused(self, operation):
        with self.assertRaises(DatabaseError), transaction.atomic():
            operation()

    def receipt(self, command_type="KIT_SEAL_DESIGNATE"):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.supervisor,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=200,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def insert(self, receipt=None, **overrides):
        receipt = receipt or self.receipt()
        fields = {
            "organization": self.org,
            "kit_id": self.kit_id,
            "sequence": 2,
            "prior_designation": self.first,
            "sealable": False,
            "effective_at": receipt.recorded_at,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.supervisor,
            "actor_roles_at_time": ["SUPERVISOR"],
            **overrides,
        }
        return KitSealDesignation.objects.create(**fields)

    def test_rows_cannot_be_changed_or_deleted(self):
        self.refused(
            lambda: KitSealDesignation.objects.filter(pk=self.first.pk).update(sealable=False)
        )
        self.refused(lambda: KitSealDesignation.objects.filter(pk=self.first.pk).delete())

    def test_inserts_must_match_a_designation_receipt_and_continue_the_chain(self):
        self.refused(lambda: self.insert(receipt=self.receipt("KIT_ASSIGNMENT_RECORD")))
        self.refused(lambda: self.insert(sequence=3))
        self.refused(lambda: self.insert(actor_user=self.admin))
        self.refused(lambda: self.insert(actor_roles_at_time=["NOT_A_ROLE"]))
        receipt = self.receipt()
        self.refused(
            lambda: self.insert(
                receipt=receipt, effective_at=receipt.recorded_at + timedelta(seconds=1)
            )
        )
        row = self.insert()  # the valid next row is accepted
        self.assertEqual(row.sequence, 2)
