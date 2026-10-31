"""As-of status, out-of-service intervals and the routine exemption (SPEC 5.9, OQ-31)."""

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from cya.accounts.models import Organization
from cya.operations.status import (
    Interval,
    current_status,
    intervals_from,
    out_of_service_at,
    out_of_service_during,
    out_of_service_intervals,
    routine_exemption,
    status_at,
)
from cya.operations.tests.support import StatusCase

T = datetime(2026, 11, 1, 12, tzinfo=UTC)
MIN = timedelta(minutes=1)


def at(minutes):
    return T + minutes * MIN


class IntervalArithmeticTests(SimpleTestCase):
    """The pure boundary rules: a status holds from its event up to, not including, the next."""

    def spans(self, events, start, end):
        return [(i.start, i.end, i.event_id) for i in intervals_from(events, start, end)]

    def test_closed_horizon_against_half_open_status_periods(self):
        events = [("a", "OUT_OF_SERVICE", at(10)), ("b", "IN_SERVICE", at(20))]
        self.assertEqual(self.spans(events, at(0), at(30)), [(at(10), at(20), "a")])
        # Out of service begins exactly at the horizon's end: a one-instant overlap.
        self.assertEqual(self.spans(events, at(0), at(10)), [(at(10), at(10), "a")])
        self.assertEqual(self.spans(events, at(0), at(10) - MIN), [])
        # Back in service exactly at the horizon's start: no overlap; one minute earlier: overlap.
        self.assertEqual(self.spans(events, at(20), at(30)), [])
        self.assertEqual(self.spans(events, at(20) - MIN, at(30)), [(at(20) - MIN, at(20), "a")])

    def test_open_ended_period_runs_to_the_horizon_end(self):
        events = [("a", "OUT_OF_SERVICE", at(-60))]
        self.assertEqual(self.spans(events, at(0), at(5)), [(at(0), at(5), "a")])

    def test_limited_and_in_service_are_not_out_of_service(self):
        events = [("a", "LIMITED_SERVICE", at(-5)), ("b", "IN_SERVICE", at(1))]
        self.assertEqual(self.spans(events, at(0), at(10)), [])

    def test_several_periods_in_one_horizon(self):
        events = [
            ("a", "OUT_OF_SERVICE", at(-5)),
            ("b", "IN_SERVICE", at(5)),
            ("c", "OUT_OF_SERVICE", at(8)),
            ("d", "LIMITED_SERVICE", at(12)),
        ]
        self.assertEqual(
            [(s.start, s.end) for s in intervals_from(events, at(0), at(20))],
            [(at(0), at(5)), (at(8), at(12))],
        )
        self.assertIsInstance(intervals_from(events, at(0), at(20))[0], Interval)

    def test_a_period_ended_by_an_event_with_the_same_timestamp_is_empty(self):
        # Sequence breaks the tie: at(10) is in service, so [at(10), at(10)) never was out.
        events = [("a", "OUT_OF_SERVICE", at(10)), ("b", "IN_SERVICE", at(10))]
        self.assertEqual(self.spans(events, at(0), at(20)), [])
        self.assertEqual(self.spans(events, at(10), at(10)), [])
        # A nonempty period that begins exactly at the horizon's end still overlaps.
        events = [("b", "IN_SERVICE", at(10)), ("c", "OUT_OF_SERVICE", at(10))]
        self.assertEqual(self.spans(events, at(0), at(10)), [(at(10), at(10), "c")])


class StatusQueryTests(StatusCase):
    def test_status_is_as_of_the_event_instant_and_past_answers_never_change(self):
        out = self.set_status("OUT_OF_SERVICE")
        out_at = self.events().get().recorded_at
        org, vehicle = self.org.pk, self.vehicle.pk
        self.assertIsNone(status_at(org, vehicle, out_at - timedelta(microseconds=1)))
        self.assertEqual(status_at(org, vehicle, out_at).pk.__str__(), out.body["event_id"])
        self.assertIsNotNone(out_of_service_at(org, vehicle, out_at))
        self.set_status("IN_SERVICE")
        back_at = self.events().last().recorded_at
        self.assertIsNotNone(out_of_service_at(org, vehicle, back_at - timedelta(microseconds=1)))
        self.assertIsNone(out_of_service_at(org, vehicle, back_at))
        # The earlier answer is unchanged by the later event.
        self.assertIsNotNone(out_of_service_at(org, vehicle, out_at + MIN * 0))
        self.assertEqual(current_status(org, vehicle).status, "IN_SERVICE")

    def test_intervals_exemption_and_no_backfill_after_return_to_service(self):
        org, vehicle = self.org.pk, self.vehicle.pk
        self.set_status("OUT_OF_SERVICE")
        began = self.events().get().recorded_at
        self.set_status("IN_SERVICE")
        returned = self.events().last().recorded_at
        due_after = returned + timedelta(hours=6)
        # A due instant that fell inside the interval stays exempt after the return (OQ-31),
        # while a later due instant is an ordinary obligation. Nothing is generated or revived.
        self.assertIsNotNone(routine_exemption(org, vehicle, began))
        self.assertIsNotNone(routine_exemption(org, vehicle, returned - timedelta(microseconds=1)))
        self.assertIsNone(routine_exemption(org, vehicle, returned))
        self.assertIsNone(routine_exemption(org, vehicle, due_after))
        self.assertEqual(
            out_of_service_intervals(org, vehicle, began - timedelta(hours=1), due_after),
            [Interval(began, returned, self.events().first().pk)],
        )
        # [E, G] around a due time before the outage overlaps nothing; one straddling the
        # start or the return overlaps, even though D itself may be in service.
        self.assertFalse(
            out_of_service_during(org, vehicle, began - timedelta(hours=2), began - MIN)
        )
        self.assertTrue(out_of_service_during(org, vehicle, began - MIN, began + MIN))
        self.assertTrue(out_of_service_during(org, vehicle, returned - MIN, returned + MIN))
        self.assertFalse(out_of_service_during(org, vehicle, returned, returned + MIN))

    def test_out_and_back_at_the_same_server_instant_is_never_out_of_service(self):
        org, vehicle = self.org.pk, self.vehicle.pk
        tied = timezone.now().replace(microsecond=0)
        with mock.patch("django.utils.timezone.now", return_value=tied):
            self.set_status("OUT_OF_SERVICE")
            self.set_status("IN_SERVICE")
        self.assertEqual([e.recorded_at for e in self.events()], [tied, tied])
        self.assertIsNone(out_of_service_at(org, vehicle, tied))
        self.assertEqual(out_of_service_intervals(org, vehicle, tied - MIN, tied + MIN), [])
        self.assertFalse(out_of_service_during(org, vehicle, tied - MIN, tied + MIN))
        self.assertFalse(out_of_service_during(org, vehicle, tied, tied))

    def test_queries_are_scoped_to_the_organization_and_validate_instants(self):
        self.set_status("OUT_OF_SERVICE")
        recorded = self.events().get().recorded_at
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        self.assertIsNone(current_status(other.pk, self.vehicle.pk))
        self.assertIsNone(routine_exemption(other.pk, self.vehicle.pk, recorded))
        with self.assertRaises(ValueError):
            status_at(self.org.pk, self.vehicle.pk, datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            out_of_service_intervals(self.org.pk, self.vehicle.pk, recorded, recorded - MIN)
        self.assertIsNone(current_status(self.org.pk, self.vehicles[1].pk))
