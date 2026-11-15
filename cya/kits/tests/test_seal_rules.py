"""Seal verification rules shared by kit and cabinet workflows."""

from dataclasses import replace
from datetime import date, timedelta
from unittest import TestCase

from cya.kits.seal_rules import (
    CertifiedKitCount,
    InspectionEntry,
    InspectionRequirement,
    SealVerificationFacts,
    inspection_summary,
    kit_count_refusal,
    verification_refusal,
)


class SealVerificationTests(TestCase):
    def setUp(self):
        self.today = date(2026, 11, 14)
        self.facts = SealVerificationFacts(
            sealable=True,
            intact=True,
            marking="crew-17",
            expires_on=self.today,
            audit_pending=False,
            open_deficiency=False,
            suspended=False,
        )

    def verify(self, facts=None, **overrides):
        args = {"marking": "crew-17", "attested": True, "today": self.today}
        return verification_refusal(facts or self.facts, **(args | overrides))

    def test_expiration_date_is_inclusive(self):
        self.assertIsNone(self.verify())
        self.assertEqual(self.verify(today=self.today + timedelta(days=1)), "SEAL_EXPIRED")
        self.assertIsNone(self.verify(replace(self.facts, expires_on=None, no_expiration=True)))

    def test_each_domain_gate_blocks_substitution(self):
        for change, code in (
            ({"sealable": False}, "NOT_SEALABLE"),
            ({"intact": False}, "SEAL_OPEN"),
            ({"audit_pending": True}, "AUDIT_PENDING"),
            ({"open_deficiency": True}, "OPEN_DEFICIENCY"),
            ({"suspended": True}, "CHECK_SUSPENDED"),
        ):
            with self.subTest(code=code):
                self.assertEqual(self.verify(replace(self.facts, **change)), code)

    def test_marking_and_explicit_attestation_are_required(self):
        self.assertEqual(self.verify(marking="crew-18"), "MARKING_MISMATCH")
        self.assertEqual(self.verify(attested=False), "ATTESTATION_REQUIRED")
        self.assertEqual(self.verify(attested=1), "ATTESTATION_REQUIRED")

    def test_malformed_facts_fail_closed(self):
        for change in (
            {"intact": "yes"},
            {"expires_on": "tomorrow"},
            {"expires_on": None},
            {"no_expiration": True},
            {"marking": ""},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(replace(self.facts, **change))


class SealInspectionTests(TestCase):
    def test_complete_inspection_returns_earliest_date_and_quantity_mismatches(self):
        today = date(2026, 11, 14)
        requirements = (
            InspectionRequirement("kit-a/item", 3, True),
            InspectionRequirement("kit-b/item", None, False),
        )
        entries = (
            InspectionEntry("kit-a/item", 2, today),
            InspectionEntry("kit-b/item", None, None),
        )
        self.assertEqual(
            inspection_summary(requirements, entries, today=today),
            (today, ("kit-a/item",)),
        )
        self.assertEqual(inspection_summary(requirements[1:], entries[1:], today=today), (None, ()))

    def test_missing_duplicate_and_expired_entries_are_refused(self):
        today = date(2026, 11, 14)
        item = InspectionEntry("item", 1, today)
        requirement = InspectionRequirement("item", 1, True)
        other = InspectionRequirement("other", 1, True)
        for requirements, entries in (
            ((requirement, other), (item,)),
            ((requirement, other), (item, item)),
            ((requirement,), (replace(item, expiration=today - timedelta(days=1)),)),
            ((requirement,), (replace(item, count=None),)),
            ((requirement,), (replace(item, count=True),)),
            ((requirement,), (replace(item, expiration=None),)),
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                inspection_summary(requirements, entries, today=today)

    def test_certified_kit_line_is_bound_to_current_assignments(self):
        certified = CertifiedKitCount("line", 2, (("kit-a", "event-a"), ("kit-b", "event-b")))
        args = {
            "line_id": "line",
            "expected_count": 2,
            "current_assignments": (("kit-b", "event-b"), ("kit-a", "event-a")),
        }
        self.assertIsNone(kit_count_refusal(certified, **args))
        self.assertEqual(
            kit_count_refusal(certified, **(args | {"line_id": "new-line"})), "KIT_SCOPE_CHANGED"
        )
        self.assertEqual(
            kit_count_refusal(
                certified,
                **(args | {"current_assignments": (("kit-a", "event-new"), ("kit-b", "event-b"))}),
            ),
            "KIT_SCOPE_CHANGED",
        )
        self.assertEqual(
            kit_count_refusal(certified, **(args | {"expected_count": 3})), "KIT_COUNT_MISMATCH"
        )
        with self.assertRaises(ValueError):
            kit_count_refusal(
                certified,
                **(args | {"current_assignments": (("kit-a", "event-a"), ("kit-a", "event-b"))}),
            )
