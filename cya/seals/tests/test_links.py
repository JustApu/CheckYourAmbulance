"""Seal evidence selects obligations per physical item and per kit-presence line."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from cya.seals.links import (
    LinkTarget,
    persist_inspection_links,
    persist_substitution_links,
    plan_links,
)


class SealLinkTests(SimpleTestCase):
    def test_persistence_preserves_receipt_attribution_and_late_timing(self):
        receipt = SimpleNamespace(recorded_at="instant")
        actor = SimpleNamespace(user="actor", roles=("CREW",))
        evidence = SimpleNamespace(organization_id="org")
        target = LinkTarget("item", kit_requirement_id="line")
        obligation = SimpleNamespace(kit_key="line-unit")
        link = {
            "id": "link",
            "obligation": obligation,
            "ordinal": 0,
            "shared_rule": None,
            "timing": "LATE",
        }
        with (
            patch("cya.seals.models.SealInspectionObligation.objects") as inspection,
            patch("cya.seals.models.SealSubstitutionObligation.objects") as substitution,
        ):
            persist_inspection_links(evidence, [link], receipt, actor)
            persist_substitution_links(evidence, target, [link], receipt, actor)
        for manager in (inspection, substitution):
            fields = manager.create.call_args.kwargs
            self.assertIs(fields["command"], receipt)
            self.assertEqual(fields["recorded_at"], "instant")
            self.assertEqual(fields["actor_user"], "actor")
            self.assertEqual(fields["actor_roles_at_time"], ["CREW"])
            self.assertTrue(fields["late_eligible"])
        self.assertEqual(substitution.create.call_args.kwargs["kit_key"], "line-unit")

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
