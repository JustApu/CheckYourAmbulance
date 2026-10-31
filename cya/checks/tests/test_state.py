"""Pure effective-outcome replay for correction chains."""

from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase

from cya.checks.state import CheckDecision, CheckSnapshot, effective_checks


class EffectiveCheckTests(SimpleTestCase):
    def setUp(self):
        self.t0 = datetime(2026, 1, 1, tzinfo=UTC)
        self.t1 = self.t0 + timedelta(minutes=1)
        self.t2 = self.t1 + timedelta(minutes=1)
        self.rows = (
            CheckSnapshot("root", "root", None, self.t0, "PASSED"),
            CheckSnapshot("fix1", "root", "root", self.t1, "FAILED"),
            CheckSnapshot("fix2", "root", "fix1", self.t2, "PASSED"),
        )
        self.events = (
            CheckDecision("root", "root", "CORRECTION", self.t1, "fix1"),
            CheckDecision("root", "fix1", "CORRECTION", self.t2, "fix2"),
        )

    def test_as_of_replay_keeps_prior_tip_and_marks_old_rows_superseded(self):
        before = effective_checks(self.rows, self.events, as_of=self.t0)[0]
        after_first = effective_checks(self.rows, self.events, as_of=self.t1)[0]
        after_second = effective_checks(self.rows, self.events, as_of=self.t2)[0]
        self.assertEqual((before.check_id, before.outcome), ("root", "PASSED"))
        self.assertEqual((after_first.check_id, after_first.outcome), ("fix1", "FAILED"))
        self.assertEqual(after_second.check_id, "fix2")
        self.assertEqual(set(after_second.superseded_ids), {"root", "fix1"})

    def test_invalidation_excludes_the_chain_without_reviving_an_earlier_outcome(self):
        invalidation = CheckDecision("root", "fix2", "INVALIDATION", self.t2)
        state = effective_checks(self.rows, (*self.events, invalidation), as_of=self.t2)[0]
        self.assertTrue(state.invalidated)
        self.assertEqual(state.check_id, "fix2")

    def test_forks_and_corrections_after_invalidation_fail_closed(self):
        fork = CheckSnapshot("other", "root", "root", self.t1, "PASSED")
        with self.assertRaisesRegex(ValueError, "forks"):
            effective_checks((*self.rows[:1], self.rows[1], fork), self.events[:1], as_of=self.t2)
        invalidation = CheckDecision("root", "fix1", "INVALIDATION", self.t2)
        later_correction = CheckDecision(
            "root", "fix1", "CORRECTION", self.t2 + timedelta(minutes=1), "fix2"
        )
        with self.assertRaisesRegex(ValueError, "cannot be corrected"):
            effective_checks(
                self.rows,
                (self.events[0], invalidation, later_correction),
                as_of=self.t2 + timedelta(minutes=1),
            )

    def test_correction_without_visible_replacement_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "replacement is missing"):
            effective_checks((self.rows[0],), (self.events[0],), as_of=self.t1)

    def test_naive_as_of_and_evidence_instants_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            effective_checks(self.rows, self.events, as_of=datetime(2026, 1, 1))
        naive = CheckSnapshot("root", "root", None, datetime(2026, 1, 1), "PASSED")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            effective_checks((naive,), (), as_of=self.t0)
