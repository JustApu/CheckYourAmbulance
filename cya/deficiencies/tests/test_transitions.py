"""The closed SPEC 8.2 transition table: only listed transitions pass."""

from itertools import product
from unittest import TestCase

from cya.deficiencies.transitions import (
    ALLOWED_TRANSITIONS,
    STATUSES,
    TransitionRejected,
    validate_transition,
)

LISTED = {
    ("OPEN", "IN_PROGRESS"),
    ("IN_PROGRESS", "PENDING_VERIFICATION"),
    ("OPEN", "AWAITING_EXTERNAL_DEPENDENCY"),
    ("AWAITING_EXTERNAL_DEPENDENCY", "OPEN"),
    ("IN_PROGRESS", "AWAITING_EXTERNAL_DEPENDENCY"),
    ("AWAITING_EXTERNAL_DEPENDENCY", "IN_PROGRESS"),
    ("PENDING_VERIFICATION", "CLOSED_VERIFIED"),
    ("PENDING_VERIFICATION", "IN_PROGRESS"),
    ("OPEN", "CLOSED_ADMINISTRATOR"),
    ("IN_PROGRESS", "CLOSED_ADMINISTRATOR"),
    ("AWAITING_EXTERNAL_DEPENDENCY", "CLOSED_ADMINISTRATOR"),
    ("PENDING_VERIFICATION", "CLOSED_ADMINISTRATOR"),
    ("OPEN", "CLOSED_UNVERIFIABLE"),
    ("IN_PROGRESS", "CLOSED_UNVERIFIABLE"),
    ("AWAITING_EXTERNAL_DEPENDENCY", "CLOSED_UNVERIFIABLE"),
    ("OPEN", "REPORTED_IN_ERROR"),
    ("IN_PROGRESS", "REPORTED_IN_ERROR"),
    ("PENDING_VERIFICATION", "REPORTED_IN_ERROR"),
    ("CLOSED_VERIFIED", "OPEN"),
    ("CLOSED_ADMINISTRATOR", "OPEN"),
    ("CLOSED_UNVERIFIABLE", "OPEN"),
    ("REPORTED_IN_ERROR", "OPEN"),
}


class TransitionTests(TestCase):
    def test_the_table_is_exactly_the_spec_8_2_arrows(self):
        self.assertEqual(ALLOWED_TRANSITIONS, LISTED)

    def test_every_listed_transition_passes_and_every_other_pair_is_rejected(self):
        for source, target in product(STATUSES, STATUSES):
            with self.subTest(source=source, target=target):
                if (source, target) in LISTED:
                    validate_transition(source, target)
                else:
                    with self.assertRaises(TransitionRejected):
                        validate_transition(source, target)

    def test_creation_is_only_into_open(self):
        validate_transition(None, "OPEN")
        for status in STATUSES:
            if status != "OPEN":
                with self.subTest(status=status), self.assertRaises(TransitionRejected):
                    validate_transition(None, status)

    def test_unknown_and_malformed_statuses_are_rejected(self):
        for source, target in (("OPEN", "DONE"), ("DONE", "OPEN"), (None, None), ("OPEN", None)):
            with self.subTest(source=source, target=target), self.assertRaises(TransitionRejected):
                validate_transition(source, target)

    def test_a_closed_deficiency_cannot_reach_a_second_closed_status(self):
        for closed in (
            "CLOSED_VERIFIED",
            "CLOSED_ADMINISTRATOR",
            "CLOSED_UNVERIFIABLE",
            "REPORTED_IN_ERROR",
        ):
            for target in (
                "CLOSED_VERIFIED",
                "CLOSED_ADMINISTRATOR",
                "CLOSED_UNVERIFIABLE",
                "REPORTED_IN_ERROR",
            ):
                with (
                    self.subTest(closed=closed, target=target),
                    self.assertRaises(TransitionRejected),
                ):
                    validate_transition(closed, target)
