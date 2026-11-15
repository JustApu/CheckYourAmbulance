"""A reseal accepts only bounded, fresh, unambiguous inspection input."""

from datetime import date

from django.test import SimpleTestCase

from cya.seals.commands import SealError
from cya.seals.reseal import _observations


class ResealInputTests(SimpleTestCase):
    def test_iso_date_is_fresh_input_and_does_not_mutate_the_request(self):
        row = {
            "item_id": "item",
            "kit_id": None,
            "outcome": "PASSED",
            "count": None,
            "expiration": "2026-11-14",
            "measurement": None,
        }
        entries, deficiencies = _observations([row])
        self.assertEqual(entries[0]["expiration"], date(2026, 11, 14))
        self.assertEqual(row["expiration"], "2026-11-14")
        self.assertEqual(deficiencies, {("item", None): None})

    def test_malformed_and_duplicate_entries_are_rejected(self):
        row = {
            "item_id": "item",
            "kit_id": None,
            "outcome": "PASSED",
            "count": None,
            "expiration": "not-a-date",
            "measurement": None,
        }
        with self.assertRaises(SealError):
            _observations([row])
        valid = row | {"expiration": None}
        with self.assertRaises(SealError):
            _observations([valid, valid])
        with self.assertRaises(SealError):
            _observations([valid | {"unexpected": True}])
