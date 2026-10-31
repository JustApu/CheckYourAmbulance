"""record_odometer and set_odometer_threshold: anomaly rule, linking, replay, malformed input."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError

from cya.checks.commands import CheckError
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.core.models import CommandReceipt
from cya.odometer import commands as odometer_commands
from cya.odometer.models import EngineHoursEntry, OdometerEntry, OdometerObligationLink
from cya.odometer.tests.support import OdometerCase


class ReadingTests(OdometerCase):
    def rejected(self, code, *args, **kwargs):
        before = self.counts()
        with self.assertRaises(CheckError) as caught:
            self.record(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.counts(), before)  # a rejected write leaves no receipt either
        return caught.exception

    def test_a_first_reading_is_stored_attributed_and_is_its_own_root(self):
        result = self.record("120345.6")
        row = OdometerEntry.objects.get()
        self.assertEqual(result.status, 201)
        self.assertEqual(
            (row.value, row.unit, row.actor_user_id, row.prior_entry_id, row.root_entry_id),
            (Decimal("120345.6"), "mi", self.crew.pk, None, row.pk),
        )
        self.assertEqual(row.actor_roles_at_time, ["CREW"])
        self.assertFalse(row.anomaly_confirmed)
        self.assertEqual(result.body["entry_id"], row.pk)
        self.assertEqual(CommandReceipt.objects.filter(command_type="ODOMETER_RECORD").count(), 1)

    def test_a_reading_may_equal_the_previous_one_and_a_rise_chains_to_it(self):
        self.record(1000)
        self.record(1000)
        self.record(1250)
        first, second, third = OdometerEntry.objects.order_by("recorded_at")
        self.assertEqual((second.prior_entry_id, third.prior_entry_id), (first.pk, second.pk))

    def test_a_lower_reading_is_refused_until_the_author_confirms_it(self):
        self.record(5000)
        error = self.rejected("ANOMALY_CONFIRMATION_REQUIRED", 4999)
        self.assertEqual((error.status, error.path), (409, "value"))
        self.record(4999, confirm_anomaly=True, explanation="Cluster replaced.")
        row = OdometerEntry.objects.order_by("recorded_at").last()
        self.assertEqual(
            (row.value, row.anomaly_confirmed, row.confirmation_explanation, row.actor_user_id),
            (Decimal("4999"), True, "Cluster replaced.", self.crew.pk),
        )
        self.record(5100)  # the confirmed reading is now the previous valid one
        self.assertEqual(
            OdometerEntry.objects.order_by("recorded_at").last().prior_entry_id, row.pk
        )

    def test_a_rise_over_the_default_threshold_needs_confirmation_and_the_limit_itself_does_not(
        self,
    ):
        self.record(10000)
        self.record(11000)  # exactly the 1000 mi default
        self.rejected("ANOMALY_CONFIRMATION_REQUIRED", "12000.1")
        self.record("12000.1", confirm_anomaly=True)
        self.assertTrue(OdometerEntry.objects.order_by("recorded_at").last().anomaly_confirmed)

    def test_confirming_a_normal_reading_stores_no_confirmation(self):
        self.record(10)
        self.record(20, confirm_anomaly=True)
        row = OdometerEntry.objects.order_by("recorded_at").last()
        self.assertFalse(row.anomaly_confirmed)
        self.assertIsNone(row.confirmation_explanation)

    def test_an_explanation_without_a_confirmed_anomaly_is_refused(self):
        self.record(10)
        error = self.rejected("INVALID_CONFIRMATION", 20, explanation="Why not.")
        self.assertEqual(error.path, "explanation")

    def test_an_administrator_changes_the_threshold_in_revisions_per_unit(self):
        self.assertEqual(self.set_threshold("mi", 200).body["revision"], 1)
        self.assertEqual(self.set_threshold("mi", "300.5").body["revision"], 2)
        self.assertEqual(self.set_threshold("km", 50).body["revision"], 1)
        self.record(0)
        self.record(300)
        self.rejected("ANOMALY_CONFIRMATION_REQUIRED", "600.6")
        self.record("600.5")  # exactly the new limit of 300.5

    def test_the_default_for_kilometres_is_larger_and_units_do_not_mix(self):
        self.record(1, unit="km")
        self.record(1601, unit="km")
        self.rejected("ANOMALY_CONFIRMATION_REQUIRED", 3202, unit="km")
        error = self.rejected("UNIT_MISMATCH", 1700, unit="mi")
        self.assertEqual(error.path, "unit")

    def test_only_an_administrator_may_change_the_threshold(self):
        for actor in (self.crew, self.supervisor):
            before = self.counts()
            with self.assertRaises(CommandDenied):
                self.set_threshold("mi", 500, actor=actor)
            self.assertEqual(self.counts()[3], before[3])

    def test_a_threshold_must_be_a_positive_known_unit_value(self):
        for unit, limit in (("mi", 0), ("mi", "-1"), ("mi", "1e3"), ("m", 5), ("mi", True)):
            with self.assertRaises(CheckError):
                self.set_threshold(unit, limit)
        self.assertEqual(self.counts()[3], 0)

    def test_engine_hours_are_optional_and_a_lower_value_needs_confirmation(self):
        self.record(100)
        self.assertEqual(EngineHoursEntry.objects.count(), 0)
        result = self.record(110, engine_hours="50.5")
        hours = EngineHoursEntry.objects.get()
        self.assertEqual(
            (hours.value, hours.unit, hours.prior_entry_id), (Decimal("50.5"), "h", None)
        )
        self.assertEqual(result.body["engine_hours_entry_id"], hours.pk)
        error = self.rejected("ANOMALY_CONFIRMATION_REQUIRED", 120, engine_hours=40)
        self.assertEqual(error.path, "engine_hours")
        self.record(120, engine_hours=40, confirm_anomaly=True, explanation="Unit swapped.")
        newest = EngineHoursEntry.objects.order_by("recorded_at").last()
        self.assertEqual(
            (newest.anomaly_confirmed, newest.confirmation_explanation, newest.prior_entry_id),
            (True, "Unit swapped.", hours.pk),
        )
        self.assertFalse(OdometerEntry.objects.order_by("recorded_at").last().anomaly_confirmed)

    def test_a_replay_returns_the_original_result_and_stores_nothing(self):
        operation_id = str(uuid4())
        first = self.record(77, operation_id=operation_id)
        before = self.counts()
        again = self.record(77, operation_id=operation_id)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(self.counts(), before)
        different = self.record(78, operation_id=operation_id)
        self.assertTrue(different.operation_reused)
        self.assertEqual(self.counts(), before)
        with self.assertRaises(OperationConflict):
            self.record(77, operation_id=operation_id, actor=self.supervisor)

    def test_a_disabled_user_is_denied_and_nothing_is_stored(self):
        self.crew.active = False
        self.crew.save(update_fields=["active"])
        before = self.counts()
        with self.assertRaises(CommandDenied):
            self.record(5)
        self.assertEqual(self.counts(), before)

    def test_malformed_input_is_refused_before_anything_is_stored(self):
        bad_values = (None, 1.5, True, -1, "-1", "1e3", "12.34", "abc", "", 10**7, "12345678")
        for value in bad_values:
            with self.subTest(value=value):
                self.rejected("INVALID_OBSERVATION", value)
        self.rejected("INVALID_OBSERVATION", 5, unit="m")
        self.rejected("INVALID_OBSERVATION", 5, engine_hours="1.25")
        self.rejected("MALFORMED_REQUEST", 5, confirm_anomaly="yes")
        self.rejected("MALFORMED_REQUEST", 5, explanation="   ")
        self.rejected("NOT_YET_SUPPORTED", 5, session_id=str(uuid4()))
        with self.assertRaises(CheckError):
            self.record(5, vehicle=type("V", (), {"pk": "not-a-uuid"})())
        with self.assertRaises(CheckError):
            self.record(5, operation_id="not-a-uuid")

    def test_an_unknown_other_organizations_or_inactive_vehicle_is_refused(self):
        from cya.accounts.models import Organization
        from cya.checklists.models import Vehicle

        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign = Vehicle.objects.create(organization=other, display_name="Foreign")
        error = self.rejected("NOT_FOUND", 5, vehicle=foreign)
        self.assertEqual(error.status, 404)
        self.vehicle.active = False
        self.vehicle.save(update_fields=["active"])
        self.rejected("VEHICLE_INACTIVE", 5)


class LinkingTests(OdometerCase):
    def links(self):
        return {
            str(link.obligation_id): str(link.entry_id)
            for link in OdometerObligationLink.objects.all()
        }

    def test_a_reading_in_the_window_satisfies_that_inspections_obligation_once(self):
        self.at_window(-30)
        result = self.record(100)
        self.assertEqual(result.body["obligation_links"], [str(self.first.pk)])
        self.assertEqual(self.links(), {str(self.first.pk): result.body["entry_id"]})
        self.at_window(10)
        second = self.record(110, actor=self.supervisor)  # another crew member, same window
        self.assertEqual(second.body["obligation_links"], [])
        self.assertEqual(len(self.links()), 1)
        link = OdometerObligationLink.objects.get()
        self.assertEqual(
            (link.actor_user_id, link.recorded_at),
            (self.crew.pk, OdometerEntry.objects.order_by("recorded_at").first().recorded_at),
        )

    def test_a_reading_before_the_window_opens_links_to_nothing(self):
        self.clock.set(self.at(hours=4, minutes=59))
        result = self.record(100)
        self.assertEqual(result.body["obligation_links"], [])
        self.assertEqual(OdometerEntry.objects.count(), 1)

    def test_the_window_edges_are_inclusive(self):
        self.clock.set(self.at(hours=5))
        self.assertEqual(self.record(1).body["obligation_links"], [str(self.first.pk)])
        self.clock.set(self.at(days=1, hours=5))
        self.assertEqual(self.record(2).body["obligation_links"], [str(self.second.pk)])

    def test_a_late_reading_satisfies_the_earliest_overdue_obligation(self):
        self.clock.set(
            self.at(hours=7, minutes=1)
        )  # past the first grace, before the second window
        result = self.record(100)
        self.assertEqual(result.body["obligation_links"], [str(self.first.pk)])

    def test_an_on_time_window_wins_over_an_older_overdue_obligation(self):
        self.clock.set(self.at(days=1, hours=6))  # the first is overdue; the second is in window
        self.assertEqual(self.record(100).body["obligation_links"], [str(self.second.pk)])
        # Nothing is left in window, so the next reading completes the overdue one late (SCH-2).
        self.assertEqual(self.record(110).body["obligation_links"], [str(self.first.pk)])
        self.assertEqual(self.record(120).body["obligation_links"], [])

    def test_another_vehicles_obligation_is_never_linked(self):
        other = self.vehicles[1]
        self.bind(vehicle=other, reconcile=False)
        self.generate()
        theirs = {str(o.pk) for o in self.odometers() if str(o.vehicle_id) == str(other.pk)}
        self.assertEqual(len(theirs), 2)
        self.at_window(0)
        result = self.record(100, vehicle=other)
        self.assertTrue(set(result.body["obligation_links"]) <= theirs)
        self.assertEqual(len(result.body["obligation_links"]), 1)
        self.assertEqual(self.record(100).body["obligation_links"], [str(self.first.pk)])


class InstantTests(OdometerCase):
    def watch_instant(self, after=None):
        """Wrap the obligation lookup: remember the instant it decided at, then let time pass."""
        seen = {}
        real = odometer_commands._obligation_to_satisfy

        def wrapper(organization_id, vehicle_id, now):
            seen["now"] = now
            found = real(organization_id, vehicle_id, now)
            if after is not None:
                self.clock.set(after)  # the window opens after the decision, before the write
            return found

        return seen, mock.patch.object(odometer_commands, "_obligation_to_satisfy", wrapper)

    def test_the_entry_the_receipt_and_the_decision_share_one_instant(self):
        self.at_window(-30)
        seen, patch = self.watch_instant()
        with patch:
            result = self.record(100)
        entry = OdometerEntry.objects.get(pk=result.body["entry_id"])
        self.assertEqual(entry.recorded_at, seen["now"])
        self.assertEqual(entry.command.recorded_at, seen["now"])
        self.assertEqual(OdometerObligationLink.objects.get().recorded_at, seen["now"])

    def test_a_window_opening_between_the_decision_and_the_write_cannot_split_them(self):
        # Decided one microsecond before the window opens; the clock then passes the start.
        self.clock.set(self.first.early_at - timedelta(microseconds=2))
        seen, patch = self.watch_instant(after=self.first.early_at + timedelta(microseconds=50))
        with patch:
            result = self.record(100)
        entry = OdometerEntry.objects.get(pk=result.body["entry_id"])
        self.assertLess(seen["now"], self.first.early_at)
        # Stored at the instant it was judged at: before the window, so correctly unlinked.
        self.assertEqual(entry.recorded_at, seen["now"])
        self.assertEqual(result.body["obligation_links"], [])
        self.assertEqual(OdometerObligationLink.objects.count(), 0)


class ThresholdRaceTests(OdometerCase):
    def test_a_threshold_changed_after_the_command_read_it_is_refused_by_the_database(self):
        self.record(1000)
        self.set_threshold("mi", 100)
        before = self.counts()
        # The command read the old limit (1,000 mi); the new revision of 100 mi is already in
        # force, so the insert trigger refuses a rise of 500 and the call writes nothing.
        with (
            mock.patch.object(odometer_commands, "threshold_for", return_value=Decimal(1000)),
            self.assertRaises(DatabaseError),
        ):
            self.record(1500)
        self.assertEqual(self.counts()[0], before[0])
        self.assertEqual(self.counts()[4], before[4])  # no receipt either
        # The retry reads the new limit: 500 over 100 is an anomaly and needs its confirmation.
        with self.assertRaises(CheckError) as caught:
            self.record(1500)
        self.assertEqual(caught.exception.code, "ANOMALY_CONFIRMATION_REQUIRED")
        self.assertEqual(self.record(1500, confirm_anomaly=True).status, 201)


class ConcurrencyTests(OdometerCase):
    def test_simultaneous_readings_chain_in_one_order_and_satisfy_the_obligation_once(self):
        self.at_window(-30)

        def enter(number):
            return self.record(100, actor=self.crew if number % 2 else self.supervisor)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [
                future.result(timeout=120) for future in [pool.submit(enter, n) for n in range(4)]
            ]
        self.assertEqual({r.status for r in results}, {201})
        self.assertEqual(sum(len(r.body["obligation_links"]) for r in results), 1)
        self.assertEqual(OdometerObligationLink.objects.count(), 1)
        rows = list(OdometerEntry.objects.order_by("recorded_at", "id"))
        self.assertEqual(len(rows), 4)
        self.assertEqual([r.prior_entry_id for r in rows[1:]], [r.pk for r in rows[:-1]])
        self.assertIsNone(rows[0].prior_entry_id)
