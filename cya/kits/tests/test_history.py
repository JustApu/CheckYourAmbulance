"""active_kits: which kit instances a vehicle had in a slot, as the records stood."""

from datetime import timedelta
from uuid import uuid4

from cya.checklists.models import Compartment
from cya.kits.history import active_kits, slot_of_reference_kit
from cya.kits.models import KitAssignmentEvent
from cya.kits.tests.support import KitCase


class ActiveKitsTests(KitCase):
    def active(self, vehicle=None, at=None, **kw):
        return active_kits(
            str(self.org.pk),
            str((vehicle or self.vehicle).pk),
            self.slot,
            at or self.clock.now,
            **kw,
        )

    def ids(self, **kw):
        return [k.kit_id for k in self.active(**kw)]

    def test_membership_follows_the_recorded_history_in_time(self):
        before = self.clock.now
        self.clock.set(self.at(hours=1))
        kit_id = self.add_kit("A").body["kit_id"]
        added = KitAssignmentEvent.objects.get(kit_id=kit_id, sequence=1).effective_at
        self.clock.set(self.at(hours=2))
        self.deactivate(kit_id)
        removed = KitAssignmentEvent.objects.get(kit_id=kit_id, sequence=2).effective_at
        self.assertEqual(self.ids(at=before), [])
        self.assertEqual(self.ids(at=added), [kit_id])
        self.assertEqual(self.ids(at=removed - timedelta(microseconds=1)), [kit_id])
        self.assertEqual(self.ids(at=removed), [])
        self.assertEqual(self.ids(), [])

    def test_a_kit_that_leaves_and_returns_starts_a_new_run(self):
        self.clock.set(self.at(hours=1))
        kit_id = self.add_kit("A").body["kit_id"]
        first_run = self.active()[0].since_event_id
        self.clock.set(self.at(hours=2))
        self.deactivate(kit_id)
        self.clock.set(self.at(hours=3))
        self.move(kit_id, self.vehicle)
        second_run = self.active()[0].since_event_id
        self.assertNotEqual(first_run, second_run)
        self.assertEqual(
            second_run, str(KitAssignmentEvent.objects.get(kit_id=kit_id, sequence=3).pk)
        )

    def test_a_move_inside_the_same_slot_does_not_start_a_new_run(self):
        kit_id = self.add_kit("A").body["kit_id"]
        run = self.active()[0].since_event_id
        newer = Compartment.objects.create(
            organization=self.org,
            vehicle=self.vehicle,
            stable_slot_id=self.cabinet.stable_slot_id,
            revision=2,
            label="Cabinet r2",
            effective_at=self.cabinet.effective_at,
        )
        from cya.kits.commands import record_kit_assignment

        record_kit_assignment(
            kit_id=kit_id,
            active=True,
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(newer.pk),
            **self.context(self.supervisor),
        )
        self.assertEqual(self.active()[0].since_event_id, run)

    def test_moving_to_another_vehicle_is_a_removal_plus_an_addition(self):
        kit_id = self.add_kit("A").body["kit_id"]
        self.clock.set(self.at(hours=1))
        self.move(kit_id, self.vehicle_b)
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.ids(vehicle=self.vehicle_b), [kit_id])
        self.assertEqual(self.ids(at=self.at(minutes=1)), [kit_id])  # as it was before the move

    def test_several_identical_kits_are_each_reported_and_ordered(self):
        ids = sorted(self.add_kit(f"K{n}").body["kit_id"] for n in range(3))
        self.assertEqual(self.ids(), ids)

    def test_records_made_after_the_cutoff_are_ignored(self):
        kit_id = self.add_kit("A").body["kit_id"]
        recorded = KitAssignmentEvent.objects.get(kit_id=kit_id).recorded_at
        self.assertEqual(self.ids(recorded_before=recorded), [])  # strictly before
        self.assertEqual(self.ids(recorded_before=recorded + timedelta(microseconds=1)), [kit_id])

    def test_another_slot_or_vehicle_does_not_match(self):
        self.add_kit("A")
        self.assertEqual(
            active_kits(str(self.org.pk), str(self.vehicle.pk), str(uuid4()), self.clock.now), ()
        )
        self.assertEqual(self.ids(vehicle=self.vehicle_b), [])


class ReferenceSlotTests(KitCase):
    def test_the_slot_a_definition_names_survives_the_reference_kit_moving(self):
        kit_id = self.add_kit("Reference").body["kit_id"]
        self.assertEqual(slot_of_reference_kit(kit_id), self.slot)
        self.deactivate(kit_id)  # the mutable row now has no compartment
        self.assertEqual(slot_of_reference_kit(kit_id), self.slot)

    def test_a_kit_with_no_history_falls_back_to_its_row(self):
        from cya.checklists.models import KitInstance

        legacy = KitInstance.objects.create(
            organization=self.org, label="Legacy", vehicle=self.vehicle, compartment=self.cabinet
        )
        self.assertEqual(slot_of_reference_kit(str(legacy.pk)), self.slot)
        bare = KitInstance.objects.create(organization=self.org, label="Bare")
        self.assertIsNone(slot_of_reference_kit(str(bare.pk)))
