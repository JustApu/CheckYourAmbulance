"""Physical seal scope construction with synthetic configuration."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from cya.seals.scope import SealScopeError, resolve_kit_lines, resolve_scope


class SealScopeTests(SimpleTestCase):
    def test_conflicting_bound_checklists_do_not_silently_drop_a_definition(self):
        def resolved(quantity):
            requirement = SimpleNamespace(
                kit_id=None,
                item_id="supply",
                definition_id=f"definition-{quantity}",
                source="TEMPLATE",
                kind="QUANTITY",
                unit=None,
                expiration_tracked=False,
                allowed_outcomes=("PASSED", "FAILED"),
            )
            item = SimpleNamespace(
                requirement=requirement, compartment_id="cab-row", effective_quantity=quantity
            )
            return SimpleNamespace(items=[item], template_version_id=f"version-{quantity}")

        with (
            patch("cya.seals.scope.bound_template_ids", return_value=["a", "b"]),
            patch("cya.seals.scope.ChecklistTemplate.objects") as templates,
            patch("cya.seals.scope.resolve_checklist", side_effect=[resolved(1), resolved(2)]),
            patch("cya.seals.scope._active_assignments", return_value={}),
            patch("cya.seals.scope.KitInstanceDefinition.objects") as definitions,
            patch("cya.seals.scope.KitDefinitionItem.objects") as contents,
            patch("cya.seals.scope.Compartment.objects") as compartments,
        ):
            templates.filter.return_value.order_by.return_value = [
                SimpleNamespace(pk="a"),
                SimpleNamespace(pk="b"),
            ]
            definitions.filter.return_value.order_by.return_value = []
            contents.filter.return_value.order_by.return_value = []
            compartments.filter.return_value.values_list.return_value = [("cab-row", "slot")]
            with self.assertRaisesRegex(SealScopeError, "incompatible definitions"):
                resolve_scope("org", "vehicle", timezone.now(), slot_id="slot")

    def test_kit_lines_pin_only_their_definition_and_current_assignments(self):
        line = SimpleNamespace(pk="line", definition_id="type-a", required_count=2)
        instance = SimpleNamespace(kit_id="kit-a", definition_id="type-a")
        other = SimpleNamespace(kit_id="kit-b", definition_id="type-b")
        with (
            patch("cya.seals.scope.kit_list_effective_at", return_value="version"),
            patch("cya.seals.scope.VehicleKitRequirement.objects") as requirements,
            patch("cya.seals.scope.KitInstanceDefinition.objects") as definitions,
        ):
            requirements.filter.return_value.order_by.return_value = [line]
            definitions.filter.return_value.order_by.return_value = [instance, other]
            result = resolve_kit_lines(
                "org",
                "vehicle",
                "slot",
                timezone.now(),
                {
                    "kit-a": ("slot", "event-a"),
                    "kit-b": ("slot", "event-b"),
                },
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(
            (result[0].line_id, result[0].required_count, result[0].assignments),
            ("line", 2, (("kit-a", "event-a"),)),
        )

    def test_cabinet_covers_loose_item_and_only_kits_in_its_slot(self):
        requirement = SimpleNamespace(
            kit_id=None,
            item_id="loose",
            definition_id="loose-def",
            source="TEMPLATE",
            kind="PRESENCE_CONDITION",
            unit=None,
            expiration_tracked=False,
            allowed_outcomes=("PASSED", "FAILED"),
        )
        loose = SimpleNamespace(
            requirement=requirement, compartment_id="cab-row", effective_quantity=None
        )
        mapping = SimpleNamespace(kit_id="kit-a", definition_id="kit-type")
        content = SimpleNamespace(
            pk="content",
            version_id="version",
            item_id="supply",
            kind="QUANTITY",
            expected_quantity=3,
            unit=None,
            expiration_tracked=True,
            allowed_outcomes=["PASSED", "FAILED"],
        )
        with (
            patch(
                "cya.seals.scope._active_assignments",
                return_value={
                    "kit-a": ("slot-a", "assignment-a"),
                    "kit-b": ("slot-b", "assignment-b"),
                },
            ),
            patch("cya.seals.scope._resolved_items", return_value={"loose": (0, loose, "v")}),
            patch("cya.seals.scope.definition_version_at", return_value="version"),
            patch("cya.seals.scope.KitInstanceDefinition.objects") as definitions,
            patch("cya.seals.scope.KitDefinitionItem.objects") as contents,
            patch("cya.seals.scope.Compartment.objects") as compartments,
        ):
            definitions.filter.return_value.order_by.return_value = [mapping]
            contents.filter.return_value.order_by.return_value = [content]
            compartments.filter.return_value.values_list.return_value = [("cab-row", "slot-a")]
            items, assignments = resolve_scope("org", "vehicle", timezone.now(), slot_id="slot-a")
        self.assertEqual(assignments, {"kit-a": ("slot-a", "assignment-a")})
        self.assertEqual({item.key for item in items}, {("loose", None), ("supply", "kit-a")})
        self.assertTrue(next(item for item in items if item.item_id == "supply").expiration_tracked)

    def test_unknown_legacy_kit_does_not_become_an_empty_scope(self):
        with (
            patch("cya.seals.scope._active_assignments", return_value={"kit": ("slot", "event")}),
            patch("cya.seals.scope._resolved_items", return_value={}),
            patch("cya.seals.scope.KitInstanceDefinition.objects") as definitions,
            patch("cya.seals.scope.KitDefinitionItem.objects") as contents,
            patch("cya.seals.scope.Compartment.objects") as compartments,
        ):
            definitions.filter.return_value.order_by.return_value = []
            contents.filter.return_value.order_by.return_value = []
            compartments.filter.return_value.values_list.return_value = []
            with self.assertRaises(SealScopeError):
                resolve_scope("org", "vehicle", timezone.now(), kit_id="kit")
