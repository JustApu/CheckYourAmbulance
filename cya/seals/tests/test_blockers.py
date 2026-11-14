"""Seal substitution respects the physical scope of deficiencies and suspensions."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from cya.deficiencies.effects import NO_KIT_KEY
from cya.seals.blockers import seal_blockers
from cya.seals.locking import SealLockError


class SealBlockerTests(SimpleTestCase):
    def test_only_the_covered_physical_kit_blocks_substitution(self):
        deficiency = SimpleNamespace(pk="d")
        projections = {"d": SimpleNamespace(status="OPEN")}
        with (
            patch("cya.seals.blockers.Deficiency.objects") as manager,
            patch("cya.seals.blockers.service.correction_scope") as scope,
            patch("cya.seals.blockers.service.suspensions_in_force", return_value=[]),
        ):
            manager.filter.return_value.order_by.return_value = [deficiency]
            scope.return_value = [("item", "definition", "kit-a")]
            self.assertEqual(
                seal_blockers("org", projections, {("item", "kit-b")}, at="instant"),
                (False, False),
            )
            self.assertEqual(
                seal_blockers("org", projections, {("item", "kit-a")}, at="instant"),
                (True, False),
            )

    def test_in_force_suspension_blocks_its_loose_item(self):
        deficiency = SimpleNamespace(pk="d")
        projections = {"d": SimpleNamespace(status="OPEN")}
        suspension = SimpleNamespace(item_id="item", instance_key=NO_KIT_KEY)
        with (
            patch("cya.seals.blockers.Deficiency.objects") as manager,
            patch("cya.seals.blockers.service.correction_scope", return_value=[]),
            patch("cya.seals.blockers.service.suspensions_in_force", return_value=[suspension]),
        ):
            manager.filter.return_value.order_by.return_value = [deficiency]
            self.assertEqual(
                seal_blockers("org", projections, {("item", None)}, at="instant"), (False, True)
            )

    def test_missing_locked_deficiency_fails_closed(self):
        with patch("cya.seals.blockers.Deficiency.objects") as manager:
            manager.filter.return_value.order_by.return_value = []
            with self.assertRaises(SealLockError):
                seal_blockers("org", {"d": SimpleNamespace(status="OPEN")}, set(), at="instant")
