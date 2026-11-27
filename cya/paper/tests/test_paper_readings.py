"""Paper-entered odometer and engine-hours readings (SPEC 8.0.0 ODO-6, LATE-5; issue #9 Part B3).

As in test_paper_checks, the 2026-06-02 odometer obligation has E = 09:00Z, G = 11:00Z and
L = 23:00Z, and paper is entered on 2026-06-03 after L.
"""

from datetime import time, timedelta
from uuid import uuid4

from cya.checks.commands import CheckError
from cya.odometer.commands import record_odometer, set_odometer_threshold
from cya.odometer.corrections import correct_reading
from cya.odometer.models import EngineHoursEntry, OdometerEntry, OdometerObligationLink
from cya.odometer.state import get_vehicle_odometer_states_as_of
from cya.paper.tests.support import ENTRY, JUNE_2, PaperCheckCase


class ReadingCase(PaperCheckCase):
    def reading(self, value, batch_id=None, actor=None, **fields):
        return record_odometer(
            vehicle_id=str(self.vehicle.pk),
            value=value,
            unit="mi",
            paper_entry_batch_id=batch_id,
            **fields,
            **self.context(actor or self.crew),
        )

    def live_reading(self, value, at, **fields):
        self.clock.set(at)
        result = self.reading(value, **fields)
        self.clock.set(ENTRY)
        return result

    def june_2_odometer(self, instant):
        states = get_vehicle_odometer_states_as_of(
            str(self.vehicle.pk),
            instant,
            organization_id=str(self.org.pk),
            start=JUNE_2,
            end=JUNE_2 + timedelta(days=1),
        )
        return next(s for s in states if s.due_at == JUNE_2.replace(hour=10))


class ObligationTests(ReadingCase):
    def test_a_paper_reading_entered_after_l_completes_its_performed_obligation(self):
        result = self.reading("40210", self.batch(at=time(6, 15)))
        entry = OdometerEntry.objects.get(pk=result.body["entry_id"])
        self.assertEqual(entry.performed_at, JUNE_2.replace(hour=10, minute=15))
        self.assertEqual(len(result.body["obligation_links"]), 1)
        now = self.june_2_odometer(entry.recorded_at)
        self.assertEqual(now.state.value, "SATISFIED")
        self.assertEqual(now.completed_at, entry.performed_at)
        before = self.june_2_odometer(entry.recorded_at - timedelta(microseconds=1))
        self.assertNotEqual(before.state.value, "SATISFIED")
        self.assertEqual(result.body["paper_entry"]["performed_at"], "2026-06-02T10:15:00+00:00")

    def test_a_paper_reading_performed_after_l_satisfies_nothing(self):
        result = self.reading("40210", self.batch(at=time(19, 30)))
        self.assertEqual(result.body["obligation_links"], [])
        self.assertFalse(OdometerObligationLink.objects.exists())

    def test_an_obligation_already_satisfied_is_not_linked_again(self):
        self.live_reading("40200", JUNE_2.replace(hour=10, minute=30))
        result = self.reading("40190", self.batch(at=time(6, 10)))
        self.assertEqual(result.body["obligation_links"], [])

    def test_another_users_batch_answers_as_absent(self):
        batch_id = self.batch(actor=self.supervisor, performers=[self.crew])
        with self.assertRaises(CheckError) as caught:
            self.reading("40210", batch_id, actor=self.crew)
        self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))
        self.assertFalse(OdometerEntry.objects.exists())


class SequenceTests(ReadingCase):
    """ODO-6: a paper reading is placed, and compared, by its performance instant."""

    def setUp(self):
        super().setUp()
        self.later = self.live_reading("1000", JUNE_2.replace(hour=12))

    def test_a_reading_below_the_next_is_normal_and_takes_its_place_in_the_sequence(self):
        result = self.reading("990", self.batch(at=time(6, 15)))
        entry = OdometerEntry.objects.get(pk=result.body["entry_id"])
        self.assertIsNone(entry.prior_entry_id)  # nothing was read before 10:15Z
        self.assertFalse(entry.anomaly_confirmed)
        # A later live reading still follows the 12:00Z one, not the paper one entered after it.
        self.clock.set(ENTRY + timedelta(hours=1))
        live = self.reading("1010")
        self.assertEqual(
            str(OdometerEntry.objects.get(pk=live.body["entry_id"]).prior_entry_id),
            self.later.body["entry_id"],
        )

    def test_a_reading_above_the_next_needs_confirmation(self):
        batch_id = self.batch(at=time(6, 15))
        with self.assertRaises(CheckError) as caught:
            self.reading("1010", batch_id)
        self.assertEqual(caught.exception.code, "ANOMALY_CONFIRMATION_REQUIRED")
        confirmed = self.reading("1010", batch_id, confirm_anomaly=True)
        self.assertTrue(OdometerEntry.objects.get(pk=confirmed.body["entry_id"]).anomaly_confirmed)

    def test_a_reading_far_below_the_next_needs_confirmation(self):
        set_odometer_threshold(unit="mi", max_increase=100, **self.context(self.admin))
        batch_id = self.batch(at=time(6, 15))
        with self.assertRaises(CheckError) as caught:
            self.reading("800", batch_id)
        self.assertEqual(caught.exception.code, "ANOMALY_CONFIRMATION_REQUIRED")
        self.assertIn("lower than the next", str(caught.exception))

    def test_engine_hours_follow_the_same_place(self):
        self.live_reading("1001", JUNE_2.replace(hour=13), engine_hours="500")
        batch_id = self.batch(at=time(6, 15))
        with self.assertRaises(CheckError) as caught:
            self.reading("990", batch_id, engine_hours="501")
        self.assertEqual(
            (caught.exception.code, caught.exception.path),
            ("ANOMALY_CONFIRMATION_REQUIRED", "engine_hours"),
        )
        ok = self.reading("990", batch_id, engine_hours="499")
        hours = EngineHoursEntry.objects.get(pk=ok.body["engine_hours_entry_id"])
        self.assertEqual(hours.performed_at, JUNE_2.replace(hour=10, minute=15))


class CorrectionTests(ReadingCase):
    def test_a_correction_keeps_the_batch_and_the_place(self):
        result = self.reading("40210", self.batch(at=time(6, 15)))
        root = OdometerEntry.objects.get(pk=result.body["entry_id"])
        corrected = correct_reading(
            kind="odometer",
            reading_id=str(root.pk),
            expected_tip_entry_id=str(root.pk),
            value="40211",
            operation_id=str(uuid4()),
            **{k: v for k, v in self.context(self.crew).items() if k != "operation_id"},
        )
        row = OdometerEntry.objects.get(pk=corrected.body["entry_id"])
        self.assertEqual(
            (row.paper_entry_batch_id, row.performed_at),
            (root.paper_entry_batch_id, root.performed_at),
        )
