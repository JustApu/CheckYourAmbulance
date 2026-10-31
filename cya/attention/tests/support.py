"""Fixtures for attention tests: checks with deficiencies, two identical kits and a schedule."""

from cya.checklists.models import Item
from cya.deficiencies.tests.support import DeficiencyCase
from cya.kits.tests.support import KitCase


class AttentionCase(DeficiencyCase, KitCase):
    """Vehicle A follows a daily 06:00 schedule and has two identical kits in one slot.

    The server clock is the controllable one from the obligation fixtures; it starts at
    2026-06-01 00:00 UTC, so the first inspection is due at 06:00 that day.
    """

    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600")
        self.reference = self.add_kit("Reference").body["kit_id"]  # names the slot for the item
        self.kit_b = self.add_kit("B").body["kit_id"]
        self.items = [*self.items, self.spec(self.kit_item, 80, kit_id=self.reference)]
        self.version_id = self.published_version(self.items)
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])

    def rows(self, **kw):
        from cya.attention.checklist import vehicle_checklist

        return vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), **kw).rows

    def row(self, item, kit_id=None, **kw):
        found = [r for r in self.rows(**kw) if r.item_id == str(item.pk) and r.kit_id == kit_id]
        self.assertEqual(len(found), 1, f"expected one row for {item.label}")
        return found[0]

    def item_ids(self, **kw):
        return [(r.item_id, r.kit_id) for r in self.rows(**kw)]

    def other_item(self):
        return Item.objects.create(organization=self.org, label="Synthetic other")
