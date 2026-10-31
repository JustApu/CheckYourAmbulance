"""Pure boundary, evidence and historical-state tests (SPEC 5.3, 5.4, 19)."""

from dataclasses import replace
from datetime import timedelta

from django.test import SimpleTestCase

from cya.obligations.state import (
    CheckEvidence,
    DeterminationEvidence,
    ObligationEvidence,
    RuleEvidence,
    State,
    StatusEvidence,
    SuspensionEvidence,
    evaluate_obligation,
)
from cya.obligations.tests.support import START


class StateTests(SimpleTestCase):
    def setUp(self):
        self.obligation = ObligationEvidence(
            "obligation",
            "org",
            "vehicle",
            "item",
            "requirement",
            "publication",
            "schedule",
            None,
            START,
            self.at(5),
            self.at(6),
            self.at(7),
        )

    def at(self, hour):
        return START + timedelta(hours=hour)

    def check(self, outcome="PASSED", at=6, **kw):
        return CheckEvidence(
            "check", "org", "vehicle", "item", "obligation", self.at(at), outcome, **kw
        )

    def evaluate(self, at=8, **kw):
        return evaluate_obligation(self.obligation, self.at(at), **kw)

    def test_exact_window_boundaries(self):
        cases = [
            (4.99, State.UPCOMING),
            (5, State.CURRENT),
            (5.99, State.CURRENT),
            (6, State.DUE),
            (7, State.DUE),
            (7.01, State.OVERDUE),
        ]
        for instant, expected in cases:
            with self.subTest(instant=instant):
                self.assertEqual(self.evaluate(instant).state, expected)

    def test_pass_fail_and_earliest_qualifying_check_keep_distinct_outcomes(self):
        for outcome in ("PASSED", "FAILED"):
            result = self.evaluate(checks=[self.check(outcome, at=5)])
            self.assertEqual((result.state, result.outcome), (State.SATISFIED, outcome))
        early = self.check("FAILED", at=5)
        late = replace(self.check(at=6), id="later")
        self.assertEqual(self.evaluate(checks=[late, early]).evidence_id, early.id)

    def test_na_needs_rule_recorded_and_effective_by_check_time(self):
        valid = RuleEvidence("na", START, START)
        for rule, satisfied in [
            (None, False),
            (valid, True),
            (replace(valid, recorded_at=self.at(8)), False),
            (replace(valid, effective_at=self.at(8)), False),
        ]:
            with self.subTest(rule=rule):
                self.assertEqual(
                    self.evaluate(checks=[self.check("NOT_APPLICABLE", na_rule=rule)]).state,
                    State.SATISFIED if satisfied else State.OVERDUE,
                )

    def test_unknown_unable_future_unlinked_and_pre_window_checks_do_not_satisfy(self):
        for check in [
            self.check("UNKNOWN"),
            self.check("UNABLE_TO_INSPECT"),
            self.check(at=9),
            self.check(at=4),
            replace(self.check(), obligation_id="other"),
            replace(self.check(), organization_id="other"),
            replace(self.check(), item_id="other"),
            replace(self.check(), vehicle_id="other"),
        ]:
            with self.subTest(check=check):
                self.assertEqual(self.evaluate(checks=[check]).state, State.OVERDUE)

    def test_late_check_requires_explicit_link_eligibility_and_records_lateness(self):
        self.assertEqual(self.evaluate(checks=[self.check(at=8)]).state, State.OVERDUE)
        result = self.evaluate(checks=[self.check("FAILED", at=8, late_eligible=True)])
        self.assertEqual(
            (result.state, result.outcome, result.late_seconds), (State.SATISFIED, "FAILED", 3600)
        )

    def test_invalidation_or_correction_excludes_check_only_from_its_recorded_instant(self):
        check = self.check(voided_at=self.at(8))
        self.assertEqual(self.evaluate(7, checks=[check]).state, State.SATISFIED)
        self.assertEqual(self.evaluate(8, checks=[check]).state, State.OVERDUE)

    def test_waiver_reinstatement_and_later_invalidation_are_historical(self):
        waiver = DeterminationEvidence(
            "waiver", "obligation", self.at(4), "WAIVED", rule=RuleEvidence("rule", START, START)
        )
        reinstate = DeterminationEvidence(
            "reinstate", "obligation", self.at(8), "REINSTATED", "waiver"
        )
        invalidate = DeterminationEvidence(
            "invalid", "obligation", self.at(9), "INVALIDATED", "reinstate"
        )
        events = [invalidate, reinstate, waiver]
        self.assertEqual(self.evaluate(7, determinations=events).state, State.WAIVED)
        self.assertEqual(self.evaluate(8, determinations=events).state, State.OVERDUE)
        self.assertEqual(self.evaluate(9, determinations=events).state, State.OVERDUE)

    def test_waiver_with_missing_or_future_rule_fails_closed(self):
        for rule in (
            None,
            RuleEvidence("rule", self.at(9), START),
            RuleEvidence("rule", START, self.at(9)),
        ):
            waiver = DeterminationEvidence("waiver", "obligation", self.at(4), "WAIVED", rule=rule)
            self.assertEqual(self.evaluate(determinations=[waiver]).state, State.OVERDUE)

    def test_supervisor_completion_requires_effective_supporting_unable_check(self):
        determination = DeterminationEvidence(
            "completion", "obligation", self.at(6.5), "SUPERVISOR_COMPLETED"
        )
        check = self.check(
            "UNABLE_TO_INSPECT", supervisor_determination_id="completion", voided_at=self.at(8)
        )
        self.assertEqual(self.evaluate(7, determinations=[determination]).state, State.DUE)
        result = self.evaluate(7, determinations=[determination], checks=[check])
        self.assertEqual((result.state, result.outcome), (State.SATISFIED, "SUPERVISOR_COMPLETED"))
        self.assertEqual(
            self.evaluate(8, determinations=[determination], checks=[check]).state, State.OVERDUE
        )

    def test_supervisor_self_completion_is_flagged_only_for_the_supporting_check_author(self):
        determination = DeterminationEvidence(
            "completion",
            "obligation",
            self.at(6.5),
            "SUPERVISOR_COMPLETED",
            actor_user_id="supervisor",
        )
        check = self.check(
            "UNABLE_TO_INSPECT",
            supervisor_determination_id="completion",
            author_user_id="supervisor",
        )
        result = self.evaluate(7, determinations=[determination], checks=[check])
        self.assertEqual(result.outcome, "SUPERVISOR_COMPLETED")
        self.assertEqual(result.display_text, "Completed by supervisor — unable to inspect")
        self.assertTrue(result.self_completed)
        other_author = replace(check, author_user_id="crew")
        result = self.evaluate(7, determinations=[determination], checks=[other_author])
        self.assertFalse(result.self_completed)

    def test_exemption_is_at_due_and_never_backfills_or_erases_later_overdue(self):
        out = StatusEvidence("out", "vehicle", 1, self.at(5), "OUT_OF_SERVICE")
        back = StatusEvidence("back", "vehicle", 2, self.at(7), "IN_SERVICE")
        self.assertEqual(self.evaluate(status_events=[back, out]).state, State.EXEMPT)
        self.assertEqual(
            self.evaluate(status_events=[replace(out, recorded_at=self.at(7))]).state, State.OVERDUE
        )
        self.assertEqual(
            self.evaluate(status_events=[out, replace(back, recorded_at=self.at(6))]).state,
            State.OVERDUE,
        )
        ad_hoc = replace(self.obligation, routine=False)
        self.assertEqual(
            evaluate_obligation(ad_hoc, self.at(8), status_events=[out]).state, State.OVERDUE
        )

    def test_explicit_ad_hoc_order_cannot_be_waived_by_carry_over_evidence(self):
        ad_hoc = replace(self.obligation, routine=False)
        waiver = DeterminationEvidence(
            "waiver", "obligation", self.at(5), "WAIVED", rule=RuleEvidence("carry", START, START)
        )
        self.assertEqual(self.evaluate(8, determinations=[waiver]).state, State.WAIVED)
        self.assertEqual(
            evaluate_obligation(ad_hoc, self.at(8), determinations=[waiver]).state, State.OVERDUE
        )
        self.assertEqual(
            evaluate_obligation(
                ad_hoc, self.at(8), determinations=[waiver], checks=[self.check()]
            ).state,
            State.SATISFIED,
        )

    def test_suspension_is_distinct_and_ending_it_does_not_fabricate_pass(self):
        suspension = SuspensionEvidence("obligation", self.at(6), self.at(8))
        self.assertEqual(
            self.evaluate(7, suspensions=[suspension], checks=[self.check()]).state, State.SUSPENDED
        )
        self.assertEqual(self.evaluate(8, suspensions=[suspension]).state, State.OVERDUE)

    def test_malformed_time_window_and_ambiguous_determinations_raise(self):
        with self.assertRaises(ValueError):
            evaluate_obligation(self.obligation, START.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            replace(self.obligation, early_at=self.at(9))
        with self.assertRaises(ValueError):
            self.evaluate(-1)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.evaluate(
                determinations=[
                    DeterminationEvidence(str(i), "obligation", self.at(4), "REINSTATED")
                    for i in range(2)
                ]
            )
