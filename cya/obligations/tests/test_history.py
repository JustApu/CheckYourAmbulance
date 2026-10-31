"""Boundary and malformed-history tests independent of the database adapters."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import TestCase

from cya.checks.state import CheckDecision, CheckSnapshot
from cya.obligations.history import (
    RecordedCheck,
    RecordedDeficiency,
    RecordedDeficiencyEvent,
    RecordedLink,
    RecordedSupport,
    deficiency_states,
    replay_checks,
)
from cya.obligations.state import (
    NO_KIT,
    DeterminationEvidence,
    ObligationEvidence,
    ProjectionHistory,
)

T = datetime(2026, 6, 1, 6, tzinfo=UTC)


class HistoryEvidenceTests(TestCase):
    def history(self):
        obligation = ObligationEvidence(
            "o",
            "org",
            "v",
            "item",
            "r",
            "pub",
            "schedule",
            None,
            T - timedelta(days=1),
            T - timedelta(hours=1),
            T,
            T + timedelta(hours=1),
        )
        root = RecordedCheck(
            CheckSnapshot("root", "root", None, T, "FAILED"), "item", NO_KIT, "crew"
        )
        tip = replace(
            root, snapshot=CheckSnapshot("tip", "root", "root", T + timedelta(minutes=1), "PASSED")
        )
        return ProjectionHistory(
            "org",
            obligations=(obligation,),
            check_records=(tip, root),
            check_decisions=(
                CheckDecision("root", "root", "CORRECTION", tip.snapshot.recorded_at, "tip"),
                CheckDecision("root", "tip", "INVALIDATION", T + timedelta(minutes=2)),
            ),
            check_links=(
                RecordedLink("root", "o", T),
                RecordedLink("tip", "o", tip.snapshot.recorded_at),
            ),
        )

    def test_full_export_replays_before_correction_and_invalidation_inclusively(self):
        history = self.history()
        for at, outcome in ((T, "FAILED"), (T + timedelta(minutes=1), "PASSED")):
            checks, facts = replay_checks(history, at)
            self.assertEqual([c.outcome for c in checks], [outcome])
            self.assertEqual(checks[0].recorded_at, T)
            self.assertEqual(facts[0].recorded_at, at)
        self.assertEqual(replay_checks(history, T - timedelta(microseconds=1)), ((), ()))
        self.assertEqual(replay_checks(history, T + timedelta(minutes=2)), ((), ()))

    def test_unlinked_and_not_yet_linked_checks_affect_attention_only(self):
        history = replace(
            self.history(), check_links=(RecordedLink("root", "o", T + timedelta(seconds=1)),)
        )
        checks, facts = replay_checks(history, T)
        self.assertEqual(checks, ())
        self.assertEqual(facts[0].outcome, "FAILED")

    def test_support_link_is_not_visible_before_its_own_recorded_instant(self):
        history = self.history()
        history = replace(
            history,
            determinations=(DeterminationEvidence("d", "o", T, "SUPERVISOR_COMPLETED"),),
            check_supports=(RecordedSupport("root", "d", T + timedelta(seconds=1)),),
        )
        self.assertIsNone(replay_checks(history, T)[0][0].supervisor_determination_id)
        self.assertEqual(
            replay_checks(history, T + timedelta(seconds=1))[0][0].supervisor_determination_id, "d"
        )

    def test_malformed_determination_timestamps_raise_value_error(self):
        for recorded_at in (T.replace(tzinfo=None), None):
            with (
                self.subTest(recorded_at=recorded_at),
                self.assertRaisesRegex(ValueError, "timezone-aware"),
            ):
                replay_checks(
                    replace(
                        self.history(),
                        determinations=(
                            DeterminationEvidence("d", "o", recorded_at, "SUPERVISOR_COMPLETED"),
                        ),
                    ),
                    T,
                )

    def test_a_link_to_an_unexported_obligation_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, "no exported obligation"):
            replay_checks(
                replace(self.history(), check_links=(RecordedLink("root", "missing", T),)), T
            )

    def test_cross_kit_link_and_malformed_chain_fail_explicitly(self):
        history = self.history()
        bad = replace(history.check_records[1], kit_key="another-kit")
        with self.assertRaisesRegex(ValueError, "kit mismatch"):
            replay_checks(replace(history, check_records=(bad,)), T)
        with self.assertRaisesRegex(ValueError, "root is missing"):
            replay_checks(
                replace(history, check_records=(history.check_records[0],)),
                T + timedelta(minutes=1),
            )

    def test_deficiency_status_retains_cancellation_and_reopening_at_their_boundaries(self):
        header = RecordedDeficiency("d", T, frozenset({"item"}))
        opened = RecordedDeficiencyEvent("e1", "d", 1, None, T, None, "OPEN")
        cancelled = RecordedDeficiencyEvent(
            "e2", "d", 2, "e1", T + timedelta(seconds=1), "OPEN", "REPORTED_IN_ERROR"
        )
        reopened = RecordedDeficiencyEvent(
            "e3", "d", 3, "e2", T + timedelta(seconds=2), "REPORTED_IN_ERROR", "OPEN"
        )
        events = (reopened, opened, cancelled)
        for at, status in (
            (T, "OPEN"),
            (cancelled.recorded_at, "REPORTED_IN_ERROR"),
            (reopened.recorded_at, "OPEN"),
        ):
            self.assertEqual(deficiency_states((header,), events, at)[0].status, status)
        for bad in (
            (opened, reopened),
            (opened, replace(cancelled, from_status="CLOSED_ADMINISTRATOR")),
            (opened, replace(cancelled, sequence=1)),
        ):
            with self.assertRaisesRegex(ValueError, "malformed"):
                deficiency_states((header,), bad, reopened.recorded_at)
        with self.assertRaisesRegex(ValueError, "no visible header"):
            deficiency_states((), (opened,), T)
        with self.assertRaisesRegex(ValueError, "no lifecycle"):
            deficiency_states((header,), (), T)
        with self.assertRaises(ValueError):
            deficiency_states((header,), events, T.replace(tzinfo=None))
