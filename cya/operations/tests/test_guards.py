"""Database guards: append-only, chain, receipt attribution, decisions, tenant keys."""

from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.accounts.models import Organization
from cya.checklists.models import Vehicle
from cya.core.audit_repository import append_receipt
from cya.core.models import CommandReceipt
from cya.operations.models import StatusRequest, VehicleStatusEvent
from cya.operations.tests.support import StatusCase


class GuardTests(StatusCase):
    def refused(self, operation):
        with self.assertRaises(DatabaseError), transaction.atomic():
            operation()

    def setUp(self):
        super().setUp()
        self.first = self.set_status("OUT_OF_SERVICE")
        self.event = VehicleStatusEvent.objects.get(pk=self.first.body["event_id"])
        self.request_row = StatusRequest.objects.get(
            pk=self.request("IN_SERVICE").body["request_id"]
        )

    def evidence(self, receipt=None, **overrides):
        receipt = receipt or self.event.command
        return {
            "organization_id": self.org.pk,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.supervisor,
            "actor_roles_at_time": ["SUPERVISOR"],
            **overrides,
        }

    def next_event(self, **overrides):
        fields = {
            "vehicle": self.vehicle,
            "sequence": 2,
            "prior_event": self.event,
            "status": "IN_SERVICE",
            "explanation": "Synthetic.",
            **self.evidence(),
        }
        fields.update(overrides)
        return VehicleStatusEvent.objects.create(**fields)

    def test_both_tables_are_append_only(self):
        for rows in (
            VehicleStatusEvent.objects.filter(pk=self.event.pk),
            StatusRequest.objects.filter(pk=self.request_row.pk),
        ):
            self.refused(lambda rows=rows: rows.update(recorded_at=self.event.recorded_at))
            self.refused(rows.delete)
        self.assertEqual(VehicleStatusEvent.objects.count(), 1)

    def test_a_valid_follow_up_row_is_accepted_so_the_refusals_below_are_meaningful(self):
        self.next_event()
        self.assertEqual(self.events().count(), 2)

    def test_chain_rules(self):
        self.refused(lambda: self.next_event(sequence=3))  # gap in the sequence
        self.refused(lambda: self.next_event(sequence=1))  # duplicate sequence
        self.refused(lambda: self.next_event(prior_event=None))  # a second "first" event
        other = VehicleStatusEvent.objects.get(pk=self.first.body["event_id"])
        self.refused(lambda: self.next_event(vehicle=self.vehicles[1], prior_event=other))
        self.next_event()
        self.refused(
            lambda: self.next_event(sequence=3, status="LIMITED_SERVICE", restrictions="x")
        )

    def test_value_checks(self):
        for overrides in (
            {"status": "DEFECT_PENDING"},
            {"explanation": "   "},
            {"status": "LIMITED_SERVICE"},  # restrictions required
            {"restrictions": "No."},  # restrictions only for limited service
            {"actor_roles_at_time": ["ROOT"]},
            {"actor_roles_at_time": "SUPERVISOR"},
        ):
            with self.subTest(overrides):
                self.refused(lambda overrides=overrides: self.next_event(**overrides))

    def test_row_must_match_its_receipt(self):
        crew_receipt = CommandReceipt.objects.get(pk=self.request_row.command_id)
        self.refused(lambda: self.next_event(actor_user=self.admin))
        self.refused(
            lambda: self.next_event(command=crew_receipt, recorded_at=crew_receipt.recorded_at)
        )
        self.refused(lambda: self.next_event(recorded_at=self.event.recorded_at.replace(year=2000)))

    def test_cross_organization_references_are_refused(self):
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign = Vehicle.objects.create(organization=other, display_name="Foreign")
        self.refused(lambda: self.next_event(vehicle=foreign))
        self.refused(lambda: self.next_event(organization_id=other.pk))

    def decision(self, action, **overrides):
        fields = {
            "vehicle": self.vehicle,
            "requested_status": "IN_SERVICE",
            "reason": "Synthetic.",
            "request": self.request_row,
            "action": action,
            "decision_explanation": "Synthetic.",
            **self.evidence(self.request_row.command, actor_user=self.crew),
        }
        fields.update(overrides)
        return StatusRequest.objects.create(**fields)

    def test_decision_rules(self):
        self.refused(lambda: self.decision("DENY", decision_explanation=" "))
        self.refused(lambda: self.decision("DENY", decision_explanation=None))
        self.refused(lambda: self.decision("DENY", requested_status="OUT_OF_SERVICE"))
        self.refused(lambda: self.decision("DENY", vehicle=self.vehicles[1]))
        self.refused(lambda: self.decision("DENY", request=None))
        self.refused(lambda: self.decision("REQUEST"))  # a REQUEST that names a request
        self.refused(lambda: self.decision("APPROVE"))  # no matching status event in its command
        self.decision("DENY")
        self.refused(lambda: self.decision("DENY"))  # a request is decided once

    def _receipt(self, command_type, actor=None):
        actor = actor or self.supervisor
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                actor_user=actor,
                operation_id=str(uuid4()),
                command_type=command_type,
                request_digest="0" * 64,
                response_status=200,
                response_body={},
                correlation_id=str(uuid4()),
            )

    def test_status_event_command_type_guard(self):
        # 1. Unrelated command type is refused
        unrelated = self._receipt("CHECKLIST_PUBLISH")
        self.refused(
            lambda: self.next_event(
                command=unrelated,
                recorded_at=unrelated.recorded_at,
            )
        )

        # 2. VEHICLE_STATUS_SET is accepted
        set_rcpt = self._receipt("VEHICLE_STATUS_SET")
        ev1 = self.next_event(
            command=set_rcpt,
            recorded_at=set_rcpt.recorded_at,
        )
        self.assertIsNotNone(ev1.pk)

        # 3. STATUS_REQUEST_DECIDE is accepted
        decide_rcpt = self._receipt("STATUS_REQUEST_DECIDE")
        ev2 = VehicleStatusEvent.objects.create(
            vehicle=self.vehicle,
            sequence=3,
            prior_event=ev1,
            status="OUT_OF_SERVICE",
            explanation="Approved change",
            command=decide_rcpt,
            recorded_at=decide_rcpt.recorded_at,
            actor_user=self.supervisor,
            actor_roles_at_time=["SUPERVISOR"],
            organization=self.org,
        )
        self.assertIsNotNone(ev2.pk)

    def test_status_request_command_type_guard(self):
        # 1. Unrelated command type is refused for request
        unrelated = self._receipt("VEHICLE_STATUS_SET", actor=self.crew)
        self.refused(
            lambda: StatusRequest.objects.create(
                organization=self.org,
                vehicle=self.vehicle,
                requested_status="IN_SERVICE",
                reason="Synthetic",
                action="REQUEST",
                command=unrelated,
                recorded_at=unrelated.recorded_at,
                actor_user=self.crew,
                actor_roles_at_time=["CREW"],
            )
        )

        # 2. STATUS_REQUEST is accepted
        req_rcpt = self._receipt("STATUS_REQUEST", actor=self.crew)
        req = StatusRequest.objects.create(
            organization=self.org,
            vehicle=self.vehicle,
            requested_status="IN_SERVICE",
            reason="Synthetic",
            action="REQUEST",
            command=req_rcpt,
            recorded_at=req_rcpt.recorded_at,
            actor_user=self.crew,
            actor_roles_at_time=["CREW"],
        )
        self.assertIsNotNone(req.pk)

        # 3. STATUS_REQUEST_DECIDE is accepted for decision
        decide_rcpt = self._receipt("STATUS_REQUEST_DECIDE", actor=self.supervisor)
        dec = StatusRequest.objects.create(
            organization=self.org,
            vehicle=self.vehicle,
            requested_status="IN_SERVICE",
            reason="Synthetic",
            request=req,
            action="DENY",
            decision_explanation="Denied",
            command=decide_rcpt,
            recorded_at=decide_rcpt.recorded_at,
            actor_user=self.supervisor,
            actor_roles_at_time=["SUPERVISOR"],
        )
        self.assertIsNotNone(dec.pk)
