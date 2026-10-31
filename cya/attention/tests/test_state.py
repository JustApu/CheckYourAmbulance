"""Pure item attention rules (SPEC 7.5, 19.4): no database."""

from datetime import UTC, datetime, timedelta
from unittest import TestCase

from cya.attention.state import CheckFact, DeficiencyFact, attention_states, latest_checks
from cya.obligations.state import NO_KIT

T0 = datetime(2026, 6, 1, tzinfo=UTC)


def check(check_id, outcome, minutes, item="item", kit=NO_KIT):
    return CheckFact(check_id, item, kit, T0 + timedelta(minutes=minutes), outcome)


def deficiency(deficiency_id, status="OPEN", items=("item",), kit=NO_KIT):
    return DeficiencyFact(deficiency_id, status, frozenset(items), kit)


def red(checks, deficiencies, key=("item", NO_KIT)):
    return attention_states([key], checks, deficiencies)[key]


class LatestCheckTests(TestCase):
    def test_the_latest_check_is_reported_whatever_its_outcome_and_per_kit(self):
        found = latest_checks(
            [
                check("a", "FAILED", 1),
                check("b", "PASSED", 2),
                check("c", "FAILED", 3, kit="kit-1"),
                check("d", "PASSED", 4, kit="kit-2"),
            ]
        )
        self.assertEqual(found[("item", NO_KIT)].check_id, "b")
        self.assertEqual(found[("item", "kit-1")].check_id, "c")
        self.assertEqual(found[("item", "kit-2")].check_id, "d")
        self.assertEqual(latest_checks([]), {})


class AttentionRuleTests(TestCase):
    def test_nothing_recorded_is_clear(self):
        self.assertFalse(red([], []).red)
        self.assertEqual(red([], []).state, "CLEAR")

    def test_failed_and_unable_checks_are_red_until_a_later_effective_check(self):
        for outcome in ("FAILED", "UNABLE_TO_INSPECT"):
            with self.subTest(outcome=outcome):
                result = red([check("c1", outcome, 1)], [])
                self.assertEqual((result.red, result.check_id), (True, "c1"))
                self.assertEqual(result.check_outcome, outcome)
                self.assertFalse(red([check("c1", outcome, 1), check("c2", "PASSED", 2)], []).red)

    def test_passed_and_not_applicable_checks_are_clear(self):
        for outcome in ("PASSED", "NOT_APPLICABLE"):
            self.assertFalse(red([check("c1", outcome, 1)], []).red)

    def test_a_later_pass_never_clears_an_unresolved_deficiency(self):
        checks = [check("c1", "FAILED", 1), check("c2", "PASSED", 2)]
        for status in (
            "OPEN",
            "IN_PROGRESS",
            "AWAITING_EXTERNAL_DEPENDENCY",
            "PENDING_VERIFICATION",
        ):
            with self.subTest(status=status):
                result = red(checks, [deficiency("d1", status)])
                self.assertEqual((result.red, result.deficiency_ids), (True, ("d1",)))
                self.assertIsNone(result.check_id)  # the check cause is gone; the deficiency stays

    def test_resolved_deficiencies_do_not_count(self):
        for status in (
            "CLOSED_VERIFIED",
            "CLOSED_ADMINISTRATOR",
            "CLOSED_UNVERIFIABLE",
            "REPORTED_IN_ERROR",
        ):
            self.assertFalse(red([check("c1", "PASSED", 1)], [deficiency("d1", status)]).red)

    def test_a_deficiency_only_applies_to_items_in_its_scope(self):
        self.assertFalse(red([], [deficiency("d1", items=("other",))]).red)

    def test_the_latest_check_decides_whatever_the_input_order(self):
        older, newer = check("c1", "FAILED", 1), check("c2", "PASSED", 2)
        self.assertFalse(red([older, newer], []).red)
        self.assertFalse(red([newer, older], []).red)

    def test_equal_instants_break_on_the_id_deterministically(self):
        one, two = check("a", "FAILED", 1), check("b", "PASSED", 1)
        self.assertEqual(red([one, two], []), red([two, one], []))

    def test_a_failure_in_one_kit_is_red_for_that_kit_only(self):
        keys = [("item", "kit-a"), ("item", "kit-b")]
        result = attention_states(
            keys,
            [check("c1", "FAILED", 1, kit="kit-a"), check("c2", "PASSED", 2, kit="kit-b")],
            [deficiency("d1", kit="kit-a")],
        )
        self.assertTrue(result[keys[0]].red)
        self.assertFalse(result[keys[1]].red)

    def test_a_deficiency_naming_no_kit_applies_to_every_instance(self):
        keys = [("item", "kit-a"), ("item", "kit-b")]
        result = attention_states(keys, [], [deficiency("d1")])
        self.assertTrue(all(result[key].red for key in keys))

    def test_a_deficiency_names_every_applicable_id_sorted(self):
        result = red([], [deficiency("d2"), deficiency("d1")])
        self.assertEqual(result.deficiency_ids, ("d1", "d2"))
