"""Pure overlay merge rules (no database)."""

from django.test import SimpleTestCase

from cya.checklists.overlay import OverlayError, OverrideEntry, Requirement, apply_overlay


def req(item, order=10, source="TEMPLATE", quantity=None, kind="PRESENCE_CONDITION"):
    return Requirement(
        item_id=item,
        definition_id=f"def-{item}-{source}",
        source=source,
        display_order=order,
        kind=kind,
        instructions="",
        expected_quantity=quantity,
        unit=None,
        allowed_outcomes=("PASSED",),
        compartment_id="slot",
        system_id=None,
        kit_id=None,
    )


class ApplyOverlayTests(SimpleTestCase):
    def setUp(self):
        self.template = [req("b", 20), req("a", 10), req("c", 30)]

    def items(self, entries):
        return [(r.item_id, r.source) for r in apply_overlay(self.template, entries)]

    def test_no_entries_keeps_template_in_display_order(self):
        self.assertEqual(self.items([]), [("a", "TEMPLATE"), ("b", "TEMPLATE"), ("c", "TEMPLATE")])

    def test_add_remove_replace(self):
        entries = [
            OverrideEntry("d", "ADD", req("d", 5, "OVERRIDE")),
            OverrideEntry("b", "REMOVE", None),
            OverrideEntry("c", "REPLACE", req("c", 30, "OVERRIDE", 4, "QUANTITY")),
        ]
        self.assertEqual(
            self.items(entries), [("d", "OVERRIDE"), ("a", "TEMPLATE"), ("c", "OVERRIDE")]
        )

    def test_ties_break_by_item_id(self):
        result = apply_overlay([req("z", 1), req("y", 1)], [])
        self.assertEqual([r.item_id for r in result], ["y", "z"])

    def test_rejects_inconsistent_entries(self):
        bad = {
            "add existing": OverrideEntry("a", "ADD", req("a", 10, "OVERRIDE")),
            "remove missing": OverrideEntry("x", "REMOVE", None),
            "replace missing": OverrideEntry("x", "REPLACE", req("x", 1, "OVERRIDE")),
            "remove with definition": OverrideEntry("a", "REMOVE", req("a", 1, "OVERRIDE")),
            "add without definition": OverrideEntry("x", "ADD", None),
            "wrong item": OverrideEntry("x", "ADD", req("y", 1, "OVERRIDE")),
            "unknown action": OverrideEntry("a", "CHANGE", req("a", 1, "OVERRIDE")),
        }
        for name, entry in bad.items():
            with self.subTest(name), self.assertRaises(OverlayError):
                apply_overlay(self.template, [entry])

    def test_rejects_two_entries_for_one_item(self):
        with self.assertRaises(OverlayError):
            apply_overlay(
                self.template,
                [
                    OverrideEntry("a", "REMOVE", None),
                    OverrideEntry("a", "ADD", req("a", 1, "OVERRIDE")),
                ],
            )

    def test_does_not_mutate_inputs(self):
        before = list(self.template)
        apply_overlay(self.template, [OverrideEntry("a", "REMOVE", None)])
        self.assertEqual(self.template, before)
