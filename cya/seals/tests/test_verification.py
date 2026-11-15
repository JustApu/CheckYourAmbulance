"""Nested kit attestations cannot conceal a later seal break or a scope change."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from cya.seals.certification import StoredCertification
from cya.seals.verification import current_certification


class SealVerificationTests(SimpleTestCase):
    def test_current_nested_kit_must_still_rely_on_the_same_intact_seal(self):
        cabinet = SimpleNamespace(kit_id=None, compartment_slot_id="slot")
        saved = StoredCertification((), (), (("kit", "substitution"),))
        substitution = SimpleNamespace(seal_event_id="old", entered_marking="mark")
        with (
            patch("cya.seals.verification.stored_certification", return_value=saved),
            patch("cya.seals.verification.SealSubstitution.objects") as substitutions,
            patch("cya.seals.verification.kit_sealable", return_value={"kit": True}),
            patch("cya.seals.commands._events", return_value=[]),
            patch("cya.seals.commands._event_state", return_value=(None, False)),
        ):
            substitutions.filter.return_value.select_related.return_value.first.return_value = (
                substitution
            )
            result = current_certification(
                "org",
                "vehicle",
                cabinet,
                (),
                {"kit": ("slot", "assignment")},
                at="instant",
                today=date(2026, 11, 14),
                open_deficiency=False,
                suspended=False,
            )
        self.assertEqual(result, "NESTED_SEAL_INVALID")

    def test_nested_kit_missing_from_current_assignment_is_rejected(self):
        cabinet = SimpleNamespace(kit_id=None, compartment_slot_id="slot")
        saved = StoredCertification((), (), (("kit", "substitution"),))
        with patch("cya.seals.verification.stored_certification", return_value=saved):
            result = current_certification(
                "org",
                "vehicle",
                cabinet,
                (),
                {},
                at="instant",
                today=date(2026, 11, 14),
                open_deficiency=False,
                suspended=False,
            )
        self.assertEqual(result, "KIT_SCOPE_CHANGED")
