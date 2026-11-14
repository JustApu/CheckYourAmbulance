"""A saved seal cannot certify changed physical contents or kit assignments."""

from django.test import SimpleTestCase

from cya.kits.seal_rules import CertifiedKitCount
from cya.seals.certification import CertifiedItem, certification_refusal
from cya.seals.scope import ScopeItem, ScopeKitLine


class SealCertificationTests(SimpleTestCase):
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
