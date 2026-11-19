"""Crew notices (SPEC 13 NOT-3, 15.4 ASG-7; owner defaults of 2026-11-18 on issue #202)."""

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from cya.core.models import CommandReceipt
from cya.deficiencies.models import DeficiencyEvent
from cya.deficiencies.tests.support import DeficiencyCase
from cya.notifications.commands import set_recipient_rule
from cya.notifications.models import NotificationOutbox
from cya.obligations.models import Obligation
from cya.operations import check_due
from cya.operations.assignments import assign_vehicle, end_assignment
from cya.operations.check_due import notify_check_due
from cya.operations.commands import set_vehicle_status
from cya.operations.tests.test_vehicle_assignment import AssignmentCase


def told(event_type):
    """{recipient user id} with an in-app notice of this event type."""
    return set(
        NotificationOutbox.objects.filter(event_type=event_type, channel="IN_APP").values_list(
            "recipient_user_id", flat=True
        )
    )


class AssignmentNoticeTests(AssignmentCase):
    def rule(self, event_type, recipient, enabled):
        set_recipient_rule(
            event_type=event_type, recipient=recipient, enabled=enabled, **self.context(self.admin)
        )

    def test_a_change_by_someone_else_tells_the_person_and_never_the_actor(self):
        self.assign(self.crew, self.a)  # their own: no notice
        self.assertEqual(told("vehicle_assignment.changed"), set())
        self.assign(self.partner, self.a)
        end_assignment(person_id=str(self.crew.pk), **self.context(self.partner))
        self.assertEqual(told("vehicle_assignment.changed"), {self.crew.pk})
        row = NotificationOutbox.objects.get(
            event_type="vehicle_assignment.changed", channel="IN_APP"
        )
        self.assertEqual(row.deep_link, f"/status/vehicles/{self.a.pk}/")
        self.assertEqual(row.payload["action"], "END")

    def test_moved_off_is_off_by_default_and_reaches_the_crew_left_when_on(self):
        self.assign(self.crew, self.b)
        self.assign(self.other, self.b)
        self.assign(self.partner, self.a)
        self.assign(self.crew, self.a, actor=self.partner)  # partner moves crew from B to A
        self.assertEqual(told("vehicle_assignment.moved_off"), set())
        self.assertEqual(told("vehicle_assignment.changed"), {self.crew.pk})
        self.rule("vehicle_assignment.moved_off", "VEHICLE_CREW", True)
        self.assign(self.supply, self.b)
        self.assign(self.supply, self.a, actor=self.partner)
        # The crew left on B is `other` (crew moved away earlier); the mover is never told.
        self.assertEqual(told("vehicle_assignment.moved_off"), {self.other.pk})

    def test_a_supervisor_change_tells_the_person(self):
        assign_vehicle(
            person_id=str(self.crew.pk),
            vehicle_id=str(self.a.pk),
            ends_at=self.hours(8),
            **self.context(self.supervisor),
        )
        self.assertEqual(told("vehicle_assignment.changed"), {self.crew.pk})


class ReportedNoticeTests(DeficiencyCase):
    def test_a_report_tells_the_vehicles_crew_and_supervisors_but_not_the_reporter(self):
        crew_mate = self.make_user("crew-mate")
        assign_vehicle(
            person_id=str(crew_mate.pk),
            vehicle_id=str(self.vehicle.pk),
            ends_at=timezone.now() + timedelta(hours=8),
            **self.context(crew_mate),
        )
        self.failed()  # the administrator records a Failed check and reports a problem
        reported = DeficiencyEvent.objects.get(event_type="REPORTED")
        self.assertEqual(told("deficiency.reported"), {crew_mate.pk, self.supervisor.pk})
        self.assertNotIn(reported.actor_user_id, told("deficiency.reported"))


class CheckDueTests(AssignmentCase):
    def setUp(self):
        super().setUp()
        self.clock.set(self.at(hours=-1))
        self.published_version()
        self.publish_schedule("test-0600")
        self.bind(vehicle=self.a)
        self.generate()
        first = Obligation.objects.filter(vehicle=self.a).order_by("due_at").first()
        self.due, self.opens = first.due_at, first.early_at

    def test_one_notice_per_vehicle_and_due_time_to_its_crew(self):
        self.clock.set(self.opens - timedelta(hours=1))
        self.assign(self.crew, self.a, hours=8)
        self.assign(self.other, self.b, hours=8)
        self.assertEqual(notify_check_due(str(self.org.pk)).notified, 0)  # not open yet
        self.clock.set(self.opens + timedelta(minutes=5))
        report = notify_check_due(str(self.org.pk))
        self.assertEqual(report.notified, 1)
        checks = Obligation.objects.filter(vehicle=self.a, due_at=self.due).count()
        self.assertEqual(report.groups, [(str(self.a.pk), self.due.isoformat(), checks)])
        self.assertEqual(told("vehicle.check_due"), {self.crew.pk})
        row = NotificationOutbox.objects.get(event_type="vehicle.check_due", channel="IN_APP")
        self.assertEqual(row.payload["checks"], checks)
        self.assertEqual(row.deep_link, f"/inspection/vehicles/{self.a.pk}/")
        again = notify_check_due(str(self.org.pk))
        self.assertEqual((again.notified, again.already), (0, 1))
        self.assertEqual(
            CommandReceipt.objects.filter(command_type="VEHICLE_CHECK_DUE_NOTICE").count(), 1
        )

    def test_groups_past_the_first_page_are_reached(self):
        # Codex review of PR #221: notified groups must not keep later ones off the page.
        self.bind(vehicle=self.b)
        self.generate()
        self.assign(self.crew, self.a, hours=8)
        self.assign(self.other, self.b, hours=8)
        self.clock.set(self.opens + timedelta(minutes=5))
        with mock.patch.object(check_due, "PAGE", 1):
            first = notify_check_due(str(self.org.pk), limit=1)
            second = notify_check_due(str(self.org.pk), limit=1)
            third = notify_check_due(str(self.org.pk), limit=1)
        self.assertEqual(first.notified, 1)
        self.assertEqual((second.notified, second.already), (1, 1))
        self.assertEqual((third.notified, third.already), (0, 2))
        self.assertEqual(
            {v for v, _d, _n in first.groups + second.groups}, {str(self.a.pk), str(self.b.pk)}
        )
        self.assertEqual(told("vehicle.check_due"), {self.crew.pk, self.other.pk})

    def test_stale_windows_and_out_of_service_vehicles_are_not_announced(self):
        self.assign(self.crew, self.a, hours=8)
        self.clock.set(self.opens + timedelta(hours=3))
        self.assertEqual(notify_check_due(str(self.org.pk)).notified, 0)  # opened too long ago
        self.clock.set(self.opens - timedelta(minutes=30))
        set_vehicle_status(
            vehicle_id=str(self.a.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic outage.",
            **self.context(self.supervisor),
        )
        self.clock.set(self.opens + timedelta(minutes=5))
        report = notify_check_due(str(self.org.pk))
        self.assertEqual((report.notified, report.out_of_service), (0, 1))
        self.assertEqual(told("vehicle.check_due"), set())
