"""Crew seal views show current requirements without prefilling observations."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from cya.seals.readers import inspection_scope, subjects
from cya.seals.scope import ScopeItem


class SealReaderTests(SimpleTestCase):
    def test_subjects_show_expiry_and_latest_tip(self):
        now = datetime(2026, 11, 14, tzinfo=UTC)
        vehicle = SimpleNamespace(organization=SimpleNamespace(station_zone="UTC"))
        kit = SimpleNamespace(pk="kit", label="Sample kit", compartment_id=None)
        event = SimpleNamespace(
            pk="event",
            kit_id="kit",
            event_type="RESEAL",
            expiration=now.date(),
            no_expiration=False,
        )
        with (
            patch("cya.seals.readers._vehicle", return_value=vehicle),
            patch("cya.seals.readers._bounded", side_effect=[[], [kit], [event]]),
            patch("cya.seals.readers.Compartment.objects"),
            patch("cya.seals.readers.KitInstance.objects"),
            patch("cya.seals.readers.SealEvent.objects"),
            patch("cya.seals.readers.kit_sealable", return_value={"kit": True}),
        ):
            row = subjects("org", "vehicle", at=now)[0]
        self.assertEqual(
            (row["state"], row["tip_id"], row["expiration"]), ("SEALED", "event", "2026-11-14")
        )
        self.assertNotIn("marking", row)

    def test_inspection_scope_never_includes_previous_counts_or_dates(self):
        item = ScopeItem(
            "item", None, "definition", "TEMPLATE", "QUANTITY", 2, None, True, ("PASSED", "FAILED")
        )
        subject = {"kind": "slot", "id": "slot", "label": "Sample"}
        with (
            patch("cya.seals.readers._vehicle"),
            patch("cya.seals.readers.subjects", return_value=[subject]),
            patch("cya.seals.readers.resolve_scope", return_value=((item,), {})),
            patch("cya.seals.readers.resolve_kit_lines", return_value=()),
            patch("cya.seals.readers.Item.objects") as items,
            patch("cya.seals.readers.KitInstance.objects") as kits,
            patch("cya.seals.readers.KitDefinition.objects") as definitions,
        ):
            items.filter.return_value.values_list.return_value = [("item", "Sample item")]
            kits.filter.return_value.values_list.return_value = []
            definitions.filter.return_value = []
            data = inspection_scope(\n                "org", "vehicle", "slot", "slot", at=datetime(2026, 11, 14, tzinfo=UTC)\n            )
        self.assertTrue(data["items"][0]["expiration_tracked"])
        self.assertNotIn("count", data["items"][0])
        self.assertNotIn("expiration", data["items"][0])
