"""Odometer satisfaction in the as-of-T obligation derivation (SCH-4, AC-21)."""

from cya.obligations.state import State
from cya.odometer.state import get_vehicle_odometer_states_as_of
from cya.odometer.tests.support import OdometerCase
from cya.operations import commands as operation_commands


class StateTests(OdometerCase):
    def states(self, instant, **kw):
        result = get_vehicle_odometer_states_as_of(
            self.vehicle.pk, instant, organization_id=self.org.pk, **kw
        )
        return {s.obligation_id: s for s in result}

    def test_an_unsatisfied_obligation_follows_the_window(self):
        first = str(self.first.pk)
        expected = {
            self.at(hours=4): State.UPCOMING,
            self.at(hours=5): State.CURRENT,
            self.at(hours=6): State.DUE,
            self.at(hours=7): State.DUE,
            self.at(hours=7, minutes=1): State.OVERDUE,
        }
        for instant, state in expected.items():
            with self.subTest(instant=instant):
                self.assertEqual(self.states(instant)[first].state, state)

    def test_any_crew_members_reading_satisfies_it_for_everyone_from_its_instant(self):
        self.clock.set(self.at(hours=5, minutes=30))
        result = self.record(500, actor=self.supervisor)
        first = str(self.first.pk)
        satisfied = self.states(self.at(hours=6))[first]
        self.assertEqual(
            (satisfied.state, satisfied.entry_id, satisfied.late_seconds),
            (State.SATISFIED, result.body["entry_id"], 0),
        )
        # As of an instant before the reading it was not satisfied (evidence known by T only).
        self.assertEqual(self.states(self.at(hours=5, minutes=29))[first].state, State.CURRENT)
        self.assertEqual(self.states(self.at(hours=6))[str(self.second.pk)].state, State.UPCOMING)

    def test_a_late_reading_records_how_late_it_was(self):
        self.clock.set(self.at(hours=7, minutes=30))
        self.record(500)
        done = self.states(self.at(hours=9))[str(self.first.pk)]
        self.assertEqual(done.state, State.SATISFIED)
        self.assertAlmostEqual(done.late_seconds, 1800, delta=1)

    def test_out_of_service_at_the_due_instant_exempts_the_obligation(self):
        self.clock.set(self.at(hours=5, minutes=30))
        operation_commands.set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic outage.",
            **self.context(self.supervisor),
        )
        self.assertEqual(self.states(self.at(hours=6))[str(self.first.pk)].state, State.EXEMPT)

    def test_an_interval_wider_than_the_bound_is_refused(self):
        from datetime import timedelta

        with self.assertRaises(ValueError):
            self.states(self.at(hours=6), start=self.at(days=-40), end=self.at(days=1))
        with self.assertRaises(ValueError):
            self.states(self.at(hours=6).replace(tzinfo=None))
        self.assertEqual(timedelta(days=31).days, 31)
