"""A saved seal cannot certify changed physical contents or kit assignments."""

from unittest.mock import patch

from django.test import SimpleTestCase

from cya.kits.seal_rules import CertifiedKitCount
from cya.seals.certification import (
    CertifiedItem,
    CertifiedKitPresence,
    certification_refusal,
    stored_certification,
)
from cya.seals.scope import ScopeItem, ScopeKitLine, ScopeKitPresence


class SealCertificationTests(SimpleTestCase):
    def test_reseal_without_inspection_is_never_treated_as_certified(self):
        with patch("cya.seals.models.SealInspection.objects") as inspections:
            inspections.filter.return_value.first.return_value = None
            with self.assertRaisesRegex(ValueError, "no full inspection"):
                stored_certification("org", "reseal")

    def setUp(self):
        self.scope = [
            ScopeItem("item", "kit", "def", "KIT_DEFINITION", "QUANTITY", 2, None, True, ()),
        ]
        self.items = [CertifiedItem("item", "kit", "KIT_DEFINITION", "def")]
        self.line = ScopeKitLine("line", "kit-type", 1, (("kit", "assignment"),))
        self.count = CertifiedKitCount("line", 1, (("kit", "assignment"),))

    def test_unchanged_scope_matches(self):
        self.assertIsNone(certification_refusal(self.scope, self.items, [self.line], [self.count]))

    def test_changed_item_or_definition_invalidates_certification(self):
        old = [CertifiedItem("item", "kit", "KIT_DEFINITION", "old-def")]
        self.assertEqual(
            certification_refusal(self.scope, old, [self.line], [self.count]), "ITEM_SCOPE_CHANGED"
        )
        self.assertEqual(
            certification_refusal(self.scope, [], [self.line], [self.count]), "ITEM_SCOPE_CHANGED"
        )

    def test_assignment_and_line_changes_invalidate_certification(self):
        moved = ScopeKitLine("line", "kit-type", 1, (("kit", "new-assignment"),))
        self.assertEqual(
            certification_refusal(self.scope, self.items, [moved], [self.count]),
            "KIT_SCOPE_CHANGED",
        )
        self.assertEqual(
            certification_refusal(self.scope, self.items, [], [self.count]), "KIT_SCOPE_CHANGED"
        )

    def test_cabinet_certification_requires_each_assigned_kit_to_be_present(self):
        current = (ScopeKitPresence("kit", "assignment", "presence-item"),)
        certified = (CertifiedKitPresence("kit", "assignment", "presence-item", 1),)
        self.assertIsNone(
            certification_refusal(
                self.scope,
                self.items,
                [self.line],
                [self.count],
                current_presence=current,
                certified_presence=certified,
            )
        )
        self.assertEqual(
            certification_refusal(
                self.scope,
                self.items,
                [self.line],
                [self.count],
                current_presence=current,
                certified_presence=(CertifiedKitPresence("kit", "assignment", "presence-item", 0),),
            ),
            "KIT_PRESENCE_CHANGED",
        )

    def test_mismatch_cannot_substitute_but_nested_certification_is_supported(self):
        missing = CertifiedKitCount("line", 0, ())
        self.assertEqual(
            certification_refusal(self.scope, self.items, [self.line], [missing]),
            "KIT_SCOPE_CHANGED",
        )
        self.assertIsNone(
            certification_refusal(
                self.scope, [], [self.line], [self.count], nested_kit_ids=("kit",)
            )
        )
