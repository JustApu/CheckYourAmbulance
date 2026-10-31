"""Kit assignment commands: authorization, validation, history, projection, replay, races."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock
from uuid import uuid4

from django.db import connection

from cya.accounts.models import Organization
from cya.checklists.models import Compartment, KitInstance, Vehicle
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.kits.commands import KitError, add_kit_instance, record_kit_assignment
from cya.kits.models import KitAssignmentEvent
from cya.kits.tests.support import KitCase


class AddKitTests(KitCase):
    def test_adding_a_kit_records_its_first_event_and_the_projection_together(self):
        result = self.add_kit("Kit A")
        kit = KitInstance.objects.get(pk=result.body["kit_id"])
        event = KitAssignmentEvent.objects.get()
        self.assertEqual(
            (result.status, result.body["code"], result.body["sequence"]), (200, "KIT_ADDED", 1)
        )
        self.assertEqual(
            (event.pk, event.sequence, event.prior_event_id), (result.body["event_id"], 1, None)
        )
        self.assertEqual(
            (event.kit_id, event.vehicle_id, event.compartment_id, event.active),
            (kit.pk, self.vehicle.pk, self.cabinet.pk, True),
        )
        self.assertEqual(
            (kit.label, kit.active, kit.vehicle_id, kit.compartment_id),
            ("Kit A", True, self.vehicle.pk, self.cabinet.pk),
        )
        self.assertEqual(event.effective_at, event.recorded_at)  # takes effect when recorded
        audit = AuditEvent.objects.get(action="KIT_ADD")
        self.assertEqual((audit.outcome, audit.actor_user_id), ("SUCCEEDED", self.supervisor.pk))
        self.assertEqual(audit.new_snapshot["values"]["kit_id"], kit.pk)

    def test_supervisors_supply_officers_and_administrators_may_but_crew_may_not(self):
        for actor in (self.supervisor, self.officer, self.admin):
            with self.subTest(actor.idp_subject):
                self.add_kit(f"Kit {actor.idp_subject}", actor=actor)
        before = KitAssignmentEvent.objects.count()
        with self.assertRaises(CommandDenied):
            self.add_kit("Crew kit", actor=self.crew)
        self.assertEqual(KitAssignmentEvent.objects.count(), before)
        self.assertTrue(AuditEvent.objects.filter(action="KIT_ADD", outcome="DENIED").exists())
        self.assertFalse(KitInstance.objects.filter(label="Crew kit").exists())

    def test_a_disabled_supervisor_is_denied(self):
        self.supervisor.active = False
        self.supervisor.save(update_fields=["active"])
        with self.assertRaises(CommandDenied):
            self.add_kit()
        self.assertFalse(KitInstance.objects.exists())

    def test_invalid_input_writes_nothing(self):
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign = Vehicle.objects.create(organization=other, display_name="Foreign")
        retired = Vehicle.objects.create(
            organization=self.org, display_name="Retired", active=False
        )
        retired_slot = Compartment.objects.create(
            organization=self.org,
            vehicle=retired,
            stable_slot_id=str(uuid4()),
            revision=1,
            label="Retired cabinet",
            effective_at=self.cabinet.effective_at,
        )
        cases = {
            "blank label": dict(
                label="  ", vehicle_id=str(self.vehicle.pk), compartment_id=str(self.cabinet.pk)
            ),
            "oversize label": dict(
                label="x" * 201,
                vehicle_id=str(self.vehicle.pk),
                compartment_id=str(self.cabinet.pk),
            ),
            "compartment of another vehicle": dict(
                label="K", vehicle_id=str(self.vehicle.pk), compartment_id=str(self.cabinet_b.pk)
            ),
            "unknown vehicle": dict(
                label="K", vehicle_id=str(uuid4()), compartment_id=str(self.cabinet.pk)
            ),
            "vehicle of another organization": dict(
                label="K", vehicle_id=str(foreign.pk), compartment_id=str(self.cabinet.pk)
            ),
            "inactive vehicle": dict(
                label="K", vehicle_id=str(retired.pk), compartment_id=str(retired_slot.pk)
            ),
        }
        for label, fields in cases.items():
            with self.subTest(label), self.assertRaises(KitError):
                add_kit_instance(**fields, **self.context(self.supervisor))
        with self.assertRaises(ValueError):
            add_kit_instance(
                label="K", vehicle_id="x", compartment_id="y", **self.context(self.supervisor)
            )
        self.assertFalse(KitInstance.objects.exists())
        self.assertFalse(KitAssignmentEvent.objects.exists())

    def test_a_replay_returns_the_original_result_and_adds_nothing(self):
        context = self.context(self.supervisor)
        first = self.add_kit("Kit A", **context)
        again = self.add_kit("Kit A", **context)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual((KitInstance.objects.count(), KitAssignmentEvent.objects.count()), (1, 1))


class AssignKitTests(KitCase):
    def setUp(self):
        super().setUp()
        self.kit_id = self.add_kit("Kit A").body["kit_id"]

    def events(self):
        return list(KitAssignmentEvent.objects.filter(kit_id=self.kit_id).order_by("sequence"))

    def test_moving_to_another_vehicle_extends_the_chain_and_updates_the_projection(self):
        result = self.move(self.kit_id, self.vehicle_b)
        first, second = self.events()
        kit = KitInstance.objects.get(pk=self.kit_id)
        self.assertEqual(
            (second.sequence, second.prior_event_id, second.vehicle_id, second.active),
            (2, first.pk, self.vehicle_b.pk, True),
        )
        self.assertEqual(
            (kit.vehicle_id, kit.compartment_id, kit.active),
            (self.vehicle_b.pk, self.cabinet_b.pk, True),
        )
        self.assertEqual(
            result.body["vehicle_ids"], sorted([str(self.vehicle.pk), str(self.vehicle_b.pk)])
        )
        audit = AuditEvent.objects.get(action="KIT_ASSIGNMENT_RECORD")
        self.assertEqual(audit.prior_snapshot["values"]["vehicle_id"], str(self.vehicle.pk))
        self.assertEqual(audit.new_snapshot["values"]["vehicle_id"], str(self.vehicle_b.pk))

    def test_deactivating_and_reactivating(self):
        self.deactivate(self.kit_id)
        kit = KitInstance.objects.get(pk=self.kit_id)
        self.assertEqual((kit.active, kit.vehicle_id, kit.compartment_id), (False, None, None))
        gone = self.events()[-1]
        self.assertEqual((gone.active, gone.vehicle_id, gone.compartment_id), (False, None, None))
        self.move(self.kit_id, self.vehicle)
        self.assertEqual([e.sequence for e in self.events()], [1, 2, 3])
        self.assertTrue(KitInstance.objects.get(pk=self.kit_id).active)

    def test_refusals_leave_history_untouched(self):
        cases = {
            "already in that state": lambda: self.move(self.kit_id, self.vehicle),
            "deactivated kit naming a vehicle": lambda: record_kit_assignment(
                kit_id=self.kit_id,
                active=False,
                vehicle_id=str(self.vehicle.pk),
                **self.context(self.supervisor),
            ),
            "compartment of another vehicle": lambda: record_kit_assignment(
                kit_id=self.kit_id,
                active=True,
                vehicle_id=str(self.vehicle.pk),
                compartment_id=str(self.cabinet_b.pk),
                **self.context(self.supervisor),
            ),
            "unknown kit": lambda: self.move(str(uuid4()), self.vehicle),
            "active is not a boolean": lambda: record_kit_assignment(
                kit_id=self.kit_id, active="yes", **self.context(self.supervisor)
            ),
        }
        for label, call in cases.items():
            with self.subTest(label), self.assertRaises(KitError):
                call()
        self.deactivate(self.kit_id)
        with self.assertRaises(KitError):  # deactivating twice
            self.deactivate(self.kit_id)
        self.assertEqual([e.sequence for e in self.events()], [1, 2])

    def test_crew_may_not_and_nothing_is_written(self):
        with self.assertRaises(CommandDenied):
            self.move(self.kit_id, self.vehicle_b, actor=self.crew)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(KitInstance.objects.get(pk=self.kit_id).vehicle_id, self.vehicle.pk)

    def test_another_organizations_kit_is_not_found(self):
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign = KitInstance.objects.create(organization=other, label="Foreign")
        with self.assertRaises(KitError):
            self.move(str(foreign.pk), self.vehicle)

    def test_a_replay_changes_nothing_and_triggers_no_reconciliation(self):
        context = self.context(self.supervisor)
        with mock.patch("cya.kits.commands.reconcile_vehicle") as reconcile:
            first = self.move(self.kit_id, self.vehicle_b, **context)
            self.assertEqual(reconcile.call_count, 2)  # the vehicle it left and the one it joined
            again = self.move(self.kit_id, self.vehicle_b, **context)
            self.assertEqual(reconcile.call_count, 2)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(len(self.events()), 2)

    def test_concurrent_moves_of_one_kit_serialize_into_a_gapless_chain(self):
        meeting = Barrier(2)
        targets = [
            self.vehicle_b,
            self.vehicle,
        ]  # A -> B and (after it) B -> A, or the reverse refusal
        contexts = [self.context(self.supervisor), self.context(self.admin)]

        def run(index):
            meeting.wait(timeout=10)
            try:
                return self.move(self.kit_id, targets[index], **contexts[index])
            except KitError as error:  # the loser may find the kit already where it asked
                return error
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, range(2)))
        rows = self.events()
        self.assertEqual([e.sequence for e in rows], list(range(1, len(rows) + 1)))
        self.assertEqual([e.prior_event_id for e in rows[1:]], [e.pk for e in rows[:-1]])
        self.assertGreaterEqual(sum(not isinstance(o, KitError) for o in outcomes), 1)
        self.assertEqual(KitInstance.objects.get(pk=self.kit_id).vehicle_id, rows[-1].vehicle_id)
