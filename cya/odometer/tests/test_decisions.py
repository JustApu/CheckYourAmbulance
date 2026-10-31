"""ODO-5 reading correction and chain invalidation, including as-of satisfaction."""

from uuid import uuid4

from cya.checks.commands import CheckError, SelfInvalidationForbidden
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.obligations.state import State
from cya.odometer.commands import previous_reading
from cya.odometer.corrections import correct_reading, invalidate_reading
from cya.odometer.models import EngineHoursEntry, OdometerEntry, ReadingEvent
from cya.odometer.state import get_vehicle_odometer_states_as_of
from cya.odometer.tests.support import OdometerCase


class DecisionTests(OdometerCase):
    def correct(self, reading_id, tip_id, value, *, kind="odometer", actor=None, **fields):
        context = self.context(actor or self.crew)
        context.update(fields)
        return correct_reading(
            kind=kind,
            reading_id=reading_id,
            expected_tip_entry_id=tip_id,
            value=value,
            **context,
        )

    def invalidate(self, reading_id, *, kind="odometer", actor=None, **fields):
        context = self.context(actor or self.admin)
        context.update(fields)
        context.setdefault("explanation", "Synthetic invalidation reason.")
        return invalidate_reading(kind=kind, reading_id=reading_id, **context)

    def state(self, at):
        states = get_vehicle_odometer_states_as_of(self.vehicle.pk, at, organization_id=self.org.pk)
        return next(row for row in states if row.obligation_id == str(self.first.pk))

    def test_author_correction_appends_a_chain_and_changes_effective_value(self):
        self.at_window(-20)
        root = self.record(100).body["entry_id"]
        first = self.correct(root, root, 110).body["entry_id"]
        second = self.correct(root, first, 120).body["entry_id"]
        rows = {row.pk: row for row in OdometerEntry.objects.filter(root_entry_id=root)}
        self.assertEqual(set(rows), {root, first, second})
        self.assertEqual(rows[first].predecessor_entry_id, root)
        self.assertEqual(rows[second].predecessor_entry_id, first)
        self.assertEqual(ReadingEvent.objects.filter(event_type="CORRECTION").count(), 2)
        self.assertEqual(self.state(self.at(hours=6)).entry_id, second)

    def test_correcting_an_older_reading_keeps_the_newer_reading_as_baseline(self):
        self.at_window(-20)
        older = self.record(100).body["entry_id"]
        self.clock.set(self.at(days=1, hours=6))
        newer = self.record(150, actor=self.supervisor).body["entry_id"]

        # This correction is judged against the reading before its root (none), not the later
        # reading. Its late recorded_at must not make it the vehicle's newest baseline.
        corrected = self.correct(older, older, 120)
        replacement = OdometerEntry.objects.get(pk=corrected.body["entry_id"])
        self.assertIsNone(replacement.prior_entry_id)
        self.assertFalse(replacement.anomaly_confirmed)
        self.assertEqual(
            str(previous_reading(OdometerEntry, self.org.pk, self.vehicle.pk).pk), newer
        )

        self.clock.set(self.at(days=2, hours=6))
        with self.assertRaises(CheckError) as caught:
            self.record(130)
        self.assertEqual(caught.exception.code, "ANOMALY_CONFIRMATION_REQUIRED")

    def test_correction_is_author_only_and_a_stale_tip_is_refused(self):
        root = self.record(100).body["entry_id"]
        before_receipts = CommandReceipt.objects.count()
        before_entries = OdometerEntry.objects.count()
        before_events = ReadingEvent.objects.count()

        with self.assertRaises(CommandDenied):
            self.correct(root, root, 110, actor=self.supervisor)

        self.assertEqual(CommandReceipt.objects.count(), before_receipts)
        self.assertEqual(OdometerEntry.objects.count(), before_entries)
        self.assertEqual(ReadingEvent.objects.count(), before_events)

        denial_audit = AuditEvent.objects.filter(
            organization=self.org,
            action="READING_CORRECTION",
            outcome="DENIED",
        ).latest("recorded_at")
        self.assertEqual(denial_audit.actor_user_id, self.supervisor.pk)
        self.assertEqual(denial_audit.reason_code, "FORBIDDEN")
        self.assertIn("SUPERVISOR", denial_audit.roles_at_time)

        # State is unchanged after denial: root reading remains effective
        self.assertEqual(OdometerEntry.objects.get(pk=root).value, 100)
        self.assertEqual(
            str(previous_reading(OdometerEntry, self.org.pk, self.vehicle.pk).pk),
            root,
        )

        # Author can correct, replay succeeds, and compare-and-swap tip is enforced
        op_id = str(uuid4())
        first = self.correct(root, root, 110, operation_id=op_id)
        replayed = self.correct(root, root, 110, operation_id=op_id)
        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.body["entry_id"], first.body["entry_id"])

        with self.assertRaises(CheckError) as caught:
            self.correct(root, root, 120)
        self.assertEqual(caught.exception.code, "CHAIN_CHANGED")

    def test_invalidation_voids_satisfaction_and_a_fresh_reading_can_satisfy_it(self):
        self.at_window(-20)
        root = self.record(100).body["entry_id"]
        before = self.at(hours=5, minutes=45)
        self.clock.set(self.at(hours=5, minutes=50))
        self.invalidate(root)
        self.assertEqual(self.state(before).state, State.SATISFIED)
        self.assertNotEqual(self.state(self.at(hours=6)).state, State.SATISFIED)
        fresh = self.record(110).body["entry_id"]
        self.assertEqual(self.state(self.at(hours=6)).entry_id, fresh)
        self.assertEqual(OdometerEntry.objects.count(), 2)

    def test_invalidation_never_revives_an_earlier_correction(self):
        root = self.record(100).body["entry_id"]
        tip = self.correct(root, root, 110).body["entry_id"]
        self.invalidate(tip)
        with self.assertRaises(CheckError) as caught:
            self.correct(root, tip, 120)
        self.assertEqual(caught.exception.code, "CHAIN_INVALIDATED")
        self.assertEqual(ReadingEvent.objects.filter(event_type="INVALIDATION").count(), 1)
        with self.assertRaises(CheckError) as caught:
            self.invalidate(root)
        self.assertEqual(caught.exception.code, "CHAIN_INVALIDATED")

    def test_only_another_users_chain_may_be_invalidated_by_an_administrator(self):
        root = self.record(100).body["entry_id"]
        with self.assertRaises(CommandDenied):
            self.invalidate(root, actor=self.supervisor)
        own = self.record(110, actor=self.admin).body["entry_id"]
        with self.assertRaises(SelfInvalidationForbidden):
            self.invalidate(own, actor=self.admin)
        self.assertEqual(ReadingEvent.objects.count(), 0)

    def test_engine_hours_use_the_same_chain_rules(self):
        row = self.record(100, engine_hours=40).body["engine_hours_entry_id"]
        before_entries = EngineHoursEntry.objects.count()
        with self.assertRaises(CommandDenied):
            self.correct(row, row, 41, kind="engine-hours", actor=self.supervisor)
        self.assertEqual(EngineHoursEntry.objects.count(), before_entries)
        denial = AuditEvent.objects.filter(
            organization=self.org,
            action="READING_CORRECTION",
            outcome="DENIED",
        ).latest("recorded_at")
        self.assertEqual(denial.actor_user_id, self.supervisor.pk)

        corrected = self.correct(row, row, 41, kind="engine-hours").body["entry_id"]
        self.assertEqual(EngineHoursEntry.objects.get(pk=corrected).root_entry_id, row)
        self.invalidate(row, kind="engine-hours")
        self.assertEqual(ReadingEvent.objects.get(event_type="INVALIDATION").root_hours_id, row)
        self.record(110, engine_hours=42)
        latest = EngineHoursEntry.objects.exclude(pk__in=(row, corrected)).get()
        self.assertIsNone(latest.prior_entry_id)

    def test_replay_malformed_input_and_anomaly_confirmation(self):
        self.record(100)
        root = self.record(200).body["entry_id"]
        operation_id = str(uuid4())
        with self.assertRaises(CheckError) as caught:
            self.correct(root, root, 90)
        self.assertEqual(caught.exception.code, "ANOMALY_CONFIRMATION_REQUIRED")
        first = self.correct(root, root, 90, confirm_anomaly=True, operation_id=operation_id)
        again = self.correct(root, root, 90, confirm_anomaly=True, operation_id=operation_id)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        reused = self.correct(root, root, 91, confirm_anomaly=True, operation_id=operation_id)
        self.assertTrue(reused.operation_reused)
        with self.assertRaises(CheckError):
            self.correct(root, first.body["entry_id"], "1.234")
        with self.assertRaises(CheckError):
            self.invalidate(root, explanation=" ")

    def test_correction_exceeding_a_later_reading_requires_confirmation_and_keeps_sequence(self):
        self.at_window(-20)
        older = self.record(100).body["entry_id"]
        self.clock.set(self.at(days=1, hours=6))
        newer = self.record(150, actor=self.supervisor).body["entry_id"]

        # 100 -> 150 sequence: correcting 100 to 300 exceeds the later reading (150)
        self.clock.set(self.at(days=2, hours=6))
        with self.assertRaises(CheckError) as caught:
            self.correct(older, older, 300)
        self.assertEqual(caught.exception.code, "ANOMALY_CONFIRMATION_REQUIRED")
        self.assertEqual(caught.exception.path, "value")

        # Supplying confirm_anomaly=True records the correction with anomaly_confirmed=True
        op_id = str(uuid4())
        confirmed = self.correct(
            older,
            older,
            300,
            confirm_anomaly=True,
            explanation="Recalibrated meter.",
            operation_id=op_id,
        )
        replacement_id = confirmed.body["entry_id"]
        replacement = OdometerEntry.objects.get(pk=replacement_id)
        self.assertIsNone(replacement.prior_entry_id)
        self.assertTrue(replacement.anomaly_confirmed)
        self.assertEqual(replacement.confirmation_explanation, "Recalibrated meter.")
        self.assertEqual(replacement.value, 300)

        # Later reading (150) remains unchanged and is not silently rewritten or invalidated
        newer_entry = OdometerEntry.objects.get(pk=newer)
        self.assertEqual(newer_entry.value, 150)
        self.assertFalse(ReadingEvent.objects.filter(event_type="INVALIDATION").exists())

        # Sequence position is unchanged: newest effective reading is still 'newer' (150)
        latest = previous_reading(OdometerEntry, self.org.pk, self.vehicle.pk)
        self.assertEqual(str(latest.pk), newer)

        # Replay returns original receipt
        replayed = self.correct(
            older,
            older,
            300,
            confirm_anomaly=True,
            explanation="Recalibrated meter.",
            operation_id=op_id,
        )
        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.body["entry_id"], replacement_id)

    def test_engine_hours_correction_exceeding_a_later_reading_requires_confirmation(self):
        older = self.record(100, engine_hours=10).body["engine_hours_entry_id"]
        self.clock.set(self.at(days=1, hours=6))
        newer = self.record(110, engine_hours=15, actor=self.supervisor).body[
            "engine_hours_entry_id"
        ]

        # Correcting older 10h to 30h exceeds later reading 15h
        self.clock.set(self.at(days=2, hours=6))
        with self.assertRaises(CheckError) as caught:
            self.correct(older, older, 30, kind="engine-hours")
        self.assertEqual(caught.exception.code, "ANOMALY_CONFIRMATION_REQUIRED")
        self.assertEqual(caught.exception.path, "value")

        # Supplying confirm_anomaly=True succeeds
        confirmed = self.correct(
            older,
            older,
            30,
            kind="engine-hours",
            confirm_anomaly=True,
            explanation="Synthetic correction.",
        )
        replacement = EngineHoursEntry.objects.get(pk=confirmed.body["entry_id"])
        self.assertTrue(replacement.anomaly_confirmed)
        self.assertEqual(replacement.value, 30)
        self.assertEqual(
            str(previous_reading(EngineHoursEntry, self.org.pk, self.vehicle.pk).pk),
            newer,
        )
