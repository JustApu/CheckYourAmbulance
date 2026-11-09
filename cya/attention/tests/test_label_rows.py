"""The label-only checklist read returns exactly the full read's label fields (issue 75).

Every test compares vehicle_label_rows with the label fields of vehicle_checklist(...).rows,
field for field and in order, and also checks which path answered: the structure path must not
evaluate obligations, and the fallback must.
"""

from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import connection
from django.test.utils import CaptureQueriesContext

from cya.attention import checklist
from cya.attention.checklist import (
    ChecklistQueryError,
    LabelRow,
    vehicle_checklist,
    vehicle_label_rows,
)
from cya.attention.tests.support import AttentionCase
from cya.obligations.adhoc import request_inspection

LABEL_FIELDS = tuple(LabelRow.__dataclass_fields__)


class LabelRowTests(AttentionCase):
    def labels(self):
        return tuple(vehicle_label_rows(str(self.org.pk), str(self.vehicle.pk)))

    def expected(self):
        return tuple(LabelRow(**{f: getattr(r, f) for f in LABEL_FIELDS}) for r in self.rows())

    def assertEquivalent(self, *, structure_only):
        with mock.patch.object(
            checklist, "_obligation_by_key", wraps=checklist._obligation_by_key
        ) as evaluated:
            found = self.labels()
        self.assertEqual(found, self.expected())
        self.assertEqual(evaluated.called, not structure_only)
        return found

    def order(self, item, kit_id=None, due_at=None):
        return request_inspection(
            vehicle_id=str(self.vehicle.pk),
            items=[{"item_id": str(item.pk), "kit_instance_id": kit_id}],
            due_at=due_at or self.at(hours=6),
            explanation="Synthetic return inspection.",
            **self.context(self.supervisor),
        )

    def without(self, item):
        self.published_version([r for r in self.items if r["item_id"] != str(item.pk)])

    def test_a_fresh_vehicle_answers_from_the_structure_in_the_full_order(self):
        found = self.assertEquivalent(structure_only=True)
        self.assertEqual(len(found), len(self.items) - 1 + 2)  # the kit item has two rows
        self.assertEqual(
            sorted(r.kit_id for r in found if r.item_id == str(self.kit_item.pk)),
            sorted([self.reference, self.kit_b]),
        )

    def test_check_and_deficiency_state_does_not_change_the_labels(self):
        self.failed(self.suction)
        self.assertEquivalent(structure_only=True)

    def test_an_order_for_a_current_row_stays_on_the_structure_path(self):
        self.order(self.suction)
        self.assertEquivalent(structure_only=True)

    def test_an_order_for_a_kit_row_stays_on_the_structure_path(self):
        self.order(self.kit_item, kit_id=self.kit_b)
        self.assertEquivalent(structure_only=True)

    def test_an_order_whose_item_left_the_checklist_falls_back_and_keeps_its_row(self):
        self.order(self.suction)
        self.clock.set(self.at(hours=1))
        self.without(self.suction)
        found = self.assertEquivalent(structure_only=False)
        self.assertIn(str(self.suction.pk), [r.item_id for r in found])

    def test_a_missed_order_for_a_removed_item_falls_back_and_is_left_out(self):
        due = self.at(hours=6)
        self.order(self.suction, due_at=due)
        self.clock.set(self.at(hours=1))
        self.without(self.suction)
        self.clock.set(due + timedelta(days=30))
        found = self.assertEquivalent(structure_only=False)
        self.assertNotIn(str(self.suction.pk), [r.item_id for r in found])

    def test_a_removed_item_with_only_routine_history_stays_on_the_structure_path(self):
        self.clock.set(self.at(hours=1))
        self.without(self.suction)
        found = self.assertEquivalent(structure_only=True)
        self.assertNotIn(str(self.suction.pk), [r.item_id for r in found])

    def test_more_distinct_order_keys_than_the_limit_fall_back(self):
        self.order(self.suction)
        with mock.patch.object(checklist, "ADHOC_KEY_LIMIT", 0):
            self.assertEquivalent(structure_only=False)

    def test_an_unknown_vehicle_is_not_found_as_in_the_full_read(self):
        for call in (
            lambda: vehicle_label_rows(str(self.org.pk), str(uuid4())),
            lambda: vehicle_checklist(str(self.org.pk), str(uuid4())),
        ):
            with self.assertRaises(ChecklistQueryError) as caught:
                call()
            self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))

    def test_the_structure_path_issues_fewer_queries_than_the_full_read(self):
        with CaptureQueriesContext(connection) as light:
            self.labels()
        with CaptureQueriesContext(connection) as full:
            self.rows()
        self.assertLess(len(light), len(full))
