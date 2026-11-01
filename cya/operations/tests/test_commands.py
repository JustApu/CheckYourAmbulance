"""Status commands: authorization, validation, request/decision flow, replay, concurrency."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection

from cya.accounts.models import Organization
from cya.checklists.models import Vehicle
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.notifications.models import NotificationOutbox
from cya.operations import commands
from cya.operations.commands import StatusError
from cya.operations.models import StatusRequest, VehicleStatusEvent
from cya.operations.status import current_status
from cya.operations.tests.support import StatusCase
from cya.testing.notifications import notified


class SetStatusTests(StatusCase):
    def test_events_form_a_per_vehicle_chain_and_status_is_derived(self):
        self.assertIsNone(current_status(self.org.pk, self.vehicle.pk))
        first = self.set_status("OUT_OF_SERVICE")
        second = self.set_status("LIMITED_SERVICE", restrictions="No lights and siren.")
        third = self.set_status("IN_SERVICE", actor=self.admin)
        rows = list(self.events())
        self.assertEqual([r.sequence for r in rows], [1, 2, 3])
        self.assertEqual([r.prior_event_id for r in rows], [None, rows[0].pk, rows[1].pk])
        self.assertEqual(
            [first.body["sequence"], second.body["sequence"], third.body["sequence"]], [1, 2, 3]
        )
        self.assertEqual(current_status(self.org.pk, self.vehicle.pk), rows[2])
        self.assertEqual(rows[2].actor_user_id, self.admin.pk)
        self.assertEqual(self.events(self.vehicles[1]).count(), 0)
        self.assertFalse(hasattr(Vehicle, "status"))
        self.assertEqual(AuditEvent.objects.filter(action="VEHICLE_STATUS_SET").count(), 3)

    def test_only_supervisor_and_administrator_may_set_status(self):
        for actor in (self.crew,):
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.set_status("OUT_OF_SERVICE", actor=actor)
        self.grant(self.crew, "SUPPLY_OFFICER")
        with self.assertRaises(CommandDenied):
            self.set_status("OUT_OF_SERVICE", actor=self.crew)
        self.assertFalse(VehicleStatusEvent.objects.exists())
        self.set_status("OUT_OF_SERVICE", actor=self.admin)

    def test_invalid_input_writes_nothing(self):
        cases = {
            "unknown status": dict(status="DEFECT_PENDING"),
            "blank explanation": dict(status="OUT_OF_SERVICE", explanation="  "),
            "huge explanation": dict(status="OUT_OF_SERVICE", explanation="x" * 4001),
            "limited without restrictions": dict(status="LIMITED_SERVICE"),
            "blank restrictions": dict(status="LIMITED_SERVICE", restrictions=" "),
            "restrictions on in service": dict(status="IN_SERVICE", restrictions="No."),
        }
        for label, kwargs in cases.items():
            with self.subTest(label), self.assertRaises(StatusError):
                self.set_status(**{"explanation": "Synthetic.", **kwargs})
        self.assertFalse(VehicleStatusEvent.objects.exists())

    def test_repeating_the_current_status_is_refused_but_new_restrictions_are_not(self):
        self.set_status("OUT_OF_SERVICE")
        with self.assertRaises(StatusError):
            self.set_status("OUT_OF_SERVICE")
        self.set_status("LIMITED_SERVICE", restrictions="No siren.")
        with self.assertRaises(StatusError):
            self.set_status("LIMITED_SERVICE", restrictions="No siren.")
        self.set_status("LIMITED_SERVICE", restrictions="No siren or stretcher.")
        self.assertEqual(self.events().count(), 3)

    def test_unknown_other_organization_and_inactive_vehicles_are_refused(self):
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign = Vehicle.objects.create(organization=other, display_name="Foreign")
        retired = Vehicle.objects.create(
            organization=self.org, display_name="Retired", active=False
        )
        for vehicle_id in (str(uuid4()), str(foreign.pk), str(retired.pk)):
            with self.subTest(vehicle_id), self.assertRaises(StatusError):
                commands.set_vehicle_status(
                    vehicle_id=vehicle_id,
                    status="OUT_OF_SERVICE",
                    explanation="Synthetic.",
                    **self.context(self.supervisor),
                )

    def test_same_operation_id_replays_without_a_second_event(self):
        context = self.context(self.supervisor)
        args = dict(
            vehicle_id=str(self.vehicle.pk), status="OUT_OF_SERVICE", explanation="Synthetic."
        )
        first = commands.set_vehicle_status(**args, **context)
        again = commands.set_vehicle_status(**args, **context)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(self.events().count(), 1)

    def test_concurrent_writers_on_one_vehicle_get_distinct_sequences(self):
        meeting = Barrier(2)
        original = commands._latest

        def meet_after_read(*args):
            latest = original(*args)
            try:
                meeting.wait(timeout=3)
            except BrokenBarrierError:
                pass  # the other writer is held back by the vehicle lock
            return latest

        def run(status, actor, **kw):
            try:
                return self.set_status(status, actor=actor, **kw)
            finally:
                connection.close()

        with (
            mock.patch.object(commands, "_latest", meet_after_read),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [
                pool.submit(run, "OUT_OF_SERVICE", self.supervisor),
                pool.submit(run, "LIMITED_SERVICE", self.admin, restrictions="No siren."),
            ]
            for future in futures:
                future.result(timeout=30)
        rows = list(self.events())
        self.assertEqual([r.sequence for r in rows], [1, 2])
        self.assertEqual(rows[1].prior_event_id, rows[0].pk)


class RequestFlowTests(StatusCase):
    def test_crew_requests_and_a_supervisor_approves_into_an_event(self):
        requested = self.request("OUT_OF_SERVICE")
        request_id = requested.body["request_id"]
        self.assertFalse(VehicleStatusEvent.objects.exists())  # a request changes nothing
        approved = self.decide(request_id, "APPROVE", "Confirmed brake fault.")
        event = VehicleStatusEvent.objects.get(pk=approved.body["event_id"])
        decision = StatusRequest.objects.get(pk=approved.body["decision_id"])
        self.assertEqual(
            (event.status, event.sequence, event.explanation),
            ("OUT_OF_SERVICE", 1, "Confirmed brake fault."),
        )
        self.assertEqual(decision.command_id, event.command_id)
        self.assertEqual(
            (decision.action, str(decision.request_id), decision.decision_explanation),
            ("APPROVE", request_id, "Confirmed brake fault."),
        )
        self.assertEqual(event.actor_user_id, self.supervisor.pk)
        # NOT-3 (issue 14), each in its causing transaction and never to the actor: the request
        # went to supervisors, the decision to the requester, and the change to supervisors and
        # administrators.
        self.assertEqual(
            notified("vehicle.status_change_requested", request_id), [self.supervisor.pk]
        )
        self.assertEqual(notified("vehicle.status_request_decided", decision.pk), [self.crew.pk])
        self.assertEqual(notified("vehicle.status_changed", event.pk), [self.admin.pk])

    def test_an_outbox_write_failure_rolls_back_the_status_change(self):
        # Issue 14: the notification is written in the causing transaction or not at all.
        with (
            mock.patch.object(
                NotificationOutbox.objects, "bulk_create", side_effect=DatabaseError("synthetic")
            ),
            self.assertRaises(DatabaseError),
        ):
            self.set_status("OUT_OF_SERVICE")
        self.assertFalse(VehicleStatusEvent.objects.exists())
        self.assertFalse(NotificationOutbox.objects.exists())

    def test_approving_a_limited_request_needs_restrictions(self):
        request_id = self.request("LIMITED_SERVICE").body["request_id"]
        with self.assertRaises(StatusError):
            self.decide(request_id, "APPROVE")
        approved = self.decide(request_id, "APPROVE", restrictions="No lights and siren.")
        self.assertEqual(self.events().get().restrictions, "No lights and siren.")
        self.assertIsNotNone(approved.body["event_id"])

    def test_denial_needs_an_explanation_and_creates_no_event(self):
        request_id = self.request("OUT_OF_SERVICE").body["request_id"]
        for explanation in (None, "", "  "):
            with self.subTest(explanation), self.assertRaises(StatusError):
                self.decide(request_id, "DENY", explanation)
        with self.assertRaises(StatusError):
            self.decide(request_id, "DENY", restrictions="No.")
        denied = self.decide(request_id, "DENY", "Seen by a mechanic; fit to run.")
        self.assertIsNone(denied.body["event_id"])
        self.assertFalse(VehicleStatusEvent.objects.exists())
        self.assertEqual(StatusRequest.objects.filter(action="DENY").count(), 1)

    def test_a_request_is_decided_once_and_only_requests_are_decidable(self):
        request_id = self.request("OUT_OF_SERVICE").body["request_id"]
        decision_id = self.decide(request_id, "DENY", "No.").body["decision_id"]
        for target in (request_id, decision_id, str(uuid4())):
            with self.subTest(target), self.assertRaises(StatusError):
                self.decide(target, "APPROVE")
        with self.assertRaises(StatusError):
            self.decide(self.request("OUT_OF_SERVICE").body["request_id"], "MAYBE")

    def test_an_approval_that_no_longer_changes_anything_is_refused(self):
        request_id = self.request("OUT_OF_SERVICE").body["request_id"]
        self.set_status("OUT_OF_SERVICE")
        with self.assertRaises(StatusError):
            self.decide(request_id, "APPROVE")
        self.decide(request_id, "DENY", "Already out of service.")

    def test_request_permissions_and_validation(self):
        self.request("OUT_OF_SERVICE", actor=self.supervisor)
        self.request("LIMITED_SERVICE", actor=self.admin)
        self.grant(self.crew, "SUPPLY_OFFICER")
        # Crew keeps request rights through its CREW role, but a request cannot be decided.
        request_id = self.request("OUT_OF_SERVICE").body["request_id"]
        with self.assertRaises(CommandDenied):
            self.decide(request_id, "APPROVE", actor=self.crew)
        for kwargs in ({"status": "SIDELINED"}, {"status": "IN_SERVICE", "reason": " "}):
            with self.subTest(kwargs), self.assertRaises(StatusError):
                self.request(**{"reason": "Synthetic.", **kwargs})
        self.set_status("IN_SERVICE")
        with self.assertRaises(StatusError):
            self.request("IN_SERVICE")

    def test_crew_cannot_decide_or_set(self):
        request_id = self.request("OUT_OF_SERVICE").body["request_id"]
        with self.assertRaises(CommandDenied):
            self.decide(request_id, "APPROVE", actor=self.crew)
        with self.assertRaises(CommandDenied):
            self.set_status("OUT_OF_SERVICE", actor=self.crew)
