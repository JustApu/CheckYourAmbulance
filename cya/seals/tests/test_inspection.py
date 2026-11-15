"""Fresh observations are checked against each physical item's resolved requirements."""

from datetime import date

from django.test import SimpleTestCase

from cya.seals.inspection import validate_inspection
from cya.seals.scope import ScopeItem

TODAY = date(2026, 11, 14)


def item(item_id, kit_id, kind="PRESENCE_CONDITION", quantity=None, tracked=False, unit=None):
    return ScopeItem(
        item_id,
        kit_id,
        "definition",
        "TEMPLATE",
        kind,
        quantity,
        unit,
        tracked,
        ("PASSED", "FAILED", "UNABLE_TO_INSPECT"),
    )


def observation(item_id, kit_id=None, **values):
    return {
        "item_id": item_id,
        "kit_id": kit_id,
        "outcome": values.get("outcome", "PASSED"),
        "count": values.get("count"),
        "expiration": values.get("expiration"),
        "measurement": values.get("measurement"),
    }


class InspectionValidationTests(SimpleTestCase):
    def test_physical_instances_and_expiration_are_independent(self):
        scope = [item("supply", "a", "QUANTITY", 2, True), item("supply", "b")]
        rows = [
            observation("supply", "a", outcome=None, count=1, expiration=TODAY),
            observation("supply", "b"),
        ]
        result, expiration, mismatches = validate_inspection(scope, rows, today=TODAY)
        self.assertEqual(result[0].outcome, "FAILED")
        self.assertEqual(expiration, TODAY)
        self.assertEqual(mismatches, (("supply", "a"),))

    def test_untracked_items_need_no_expiration(self):
        result, expiration, mismatches = validate_inspection(
            [item("supply", None)], [observation("supply")], today=TODAY
        )
        self.assertEqual((len(result), expiration, mismatches), (1, None, ()))

    def test_missing_duplicate_and_unlisted_items_fail_closed(self):
        scope = [item("supply", None), item("other", None)]
        for rows in (
            [observation("supply")],
            [observation("supply"), observation("supply")],
            [observation("supply"), observation("unexpected")],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_inspection(scope, rows, today=TODAY)

    def test_bad_observations_and_expired_contents_are_rejected(self):
        quantity = [item("supply", None, "QUANTITY", 2, True)]
        for row in (
            observation("supply", count=1, expiration=TODAY),
            observation("supply", count=True, expiration=TODAY),
            observation("supply", count=2, expiration=date(2026, 11, 13)),
            observation("supply", count=2),
            observation("supply", count=2, expiration=TODAY, outcome="UNABLE_TO_INSPECT"),
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                validate_inspection(quantity, [row], today=TODAY)

    def test_measurement_requires_the_configured_unit(self):
        scope = [item("pressure", None, "MEASUREMENT", unit="psi")]
        good = observation("pressure", measurement={"value": "1.5", "unit": "psi"})
        self.assertEqual(len(validate_inspection(scope, [good], today=TODAY)[0]), 1)
        bad = observation("pressure", measurement={"value": "1.5", "unit": "bar"})
        with self.assertRaises(ValueError):
            validate_inspection(scope, [bad], today=TODAY)
