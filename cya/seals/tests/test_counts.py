"""Cabinet kit counts must refer to fresh, effective ordinary checks."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from cya.seals.counts import validate_kit_counts


class KitCountCheckTests(SimpleTestCase):
    def setUp(self):
        self.line_id, self.check_id = str(uuid4()), str(uuid4())
        self.start = SimpleNamespace(recorded_at=datetime(2026, 11, 14, tzinfo=UTC))
        self.at = self.start.recorded_at + timedelta(minutes=5)
        self.line = SimpleNamespace(line_id=self.line_id)
        root = SimpleNamespace(actor_user_id="actor", recorded_at=self.start.recorded_at)
        self.check = SimpleNamespace(
            pk=self.check_id,
            vehicle_id="vehicle",
            actor_user_id="actor",
            root_check=root,
            root_check_id=self.check_id,
            kit_requirement_id=self.line_id,
            kit_id=None,
            outcome="PASSED",
            quantity=2,
            recorded_at=self.at,
        )
        self.selections = [{"line_id": self.line_id, "check_id": self.check_id}]

    def run_case(self, *, invalid=(), superseded=()):
        initial, later = Mock(), Mock()
        initial.select_related.return_value = [self.check]
        later.values_list.return_value = list(superseded)
        with (
            patch("cya.seals.counts.CheckRecord.objects") as checks,
            patch("cya.seals.counts.CheckEvent.objects") as events,
        ):
            checks.filter.side_effect = [initial, later]
            events.filter.return_value.values_list.return_value = list(invalid)
            return validate_kit_counts(
                "org",
                "vehicle",
                "actor",
                [self.line],
                self.selections,
                start=self.start,
                at=self.at,
            )

    def test_current_fresh_check_matches_its_line(self):
        self.assertEqual(self.run_case(), [(self.line, self.check)])

    def test_other_actor_old_or_invalidated_check_is_rejected(self):
        self.check.actor_user_id = "someone-else"
        with self.assertRaises(ValueError):
            self.run_case()
        self.check.actor_user_id = "actor"
        self.check.root_check.recorded_at -= timedelta(seconds=1)
        with self.assertRaises(ValueError):
            self.run_case()
        self.check.root_check.recorded_at = self.start.recorded_at
        with self.assertRaises(ValueError):
            self.run_case(invalid=[self.check_id])
        with self.assertRaises(ValueError):
            self.run_case(superseded=[self.check_id])

    def test_missing_duplicate_or_unstarted_count_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_kit_counts(
                "org", "vehicle", "actor", [self.line], [], start=self.start, at=self.at
            )
        with self.assertRaises(ValueError):
            validate_kit_counts(
                "org",
                "vehicle",
                "actor",
                [self.line],
                self.selections * 2,
                start=self.start,
                at=self.at,
            )
        with self.assertRaises(ValueError):
            validate_kit_counts(
                "org", "vehicle", "actor", [self.line], self.selections, start=None, at=self.at
            )
