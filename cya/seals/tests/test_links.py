"""Seal evidence selects obligations per physical item and per kit-presence line."""

from datetime import UTC, datetime
from unittest.mock import patch

from django.test import SimpleTestCase

from cya.seals.links import LinkTarget, plan_links


class SealLinkTests(SimpleTestCase):
    def test_each_physical_item_and_presence_line_has_its_own_selection(self):
        targets = [
            LinkTarget("item", "kit-a"),
            LinkTarget("item", "kit-b"),
            LinkTarget("presence", kit_requirement_id="line-a"),
            LinkTarget("presence", kit_requirement_id="line-b"),
        ]
        instant = datetime(2026, 11, 14, tzinfo=UTC)
        with patch("cya.seals.links.choose_links", return_value=[{"ordinal": 0}]) as choose:
            plans = plan_links("org", "vehicle", targets, recorded_at=instant)
        self.assertEqual([target for target, _ in plans], targets)
        self.assertEqual(
            [call.args[0].kit_id for call in choose.call_args_list], ["kit-a", "kit-b", None, None]
        )
        self.assertEqual(
            [call.args[0].kit_requirement_id for call in choose.call_args_list],
            [None, None, "line-a", "line-b"],
        )
        self.assertTrue(
            all(call.kwargs["recorded_at"] == instant for call in choose.call_args_list)
        )

    def test_duplicate_or_ambiguous_physical_targets_are_refused(self):
        target = LinkTarget("item", "kit")
        with patch("cya.seals.links.choose_links", return_value=[]):
            with self.assertRaises(ValueError):
                plan_links("org", "vehicle", [target, target], recorded_at="instant")
            with self.assertRaises(ValueError):
                plan_links(
                    "org", "vehicle", [LinkTarget("item", "kit", "line")], recorded_at="instant"
                )
