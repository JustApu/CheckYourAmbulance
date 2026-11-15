"""Seal evidence refreshes the same current projection used by normal checks."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from cya.obligations.refresh import refresh_for_receipt


class SealRefreshTests(SimpleTestCase):
    def test_seal_evidence_refreshes_only_linked_obligations(self):
        for command in ("SEAL_RESEAL", "SEAL_SUBSTITUTE"):
            receipt = SimpleNamespace(command_type=command, organization_id="org")
            with (
                self.subTest(command=command),
                patch("cya.seals.models.SealInspectionObligation.objects") as inspections,
                patch("cya.seals.models.SealSubstitutionObligation.objects") as substitutions,
                patch("cya.obligations.refresh.Obligation.objects") as obligations,
                patch("cya.obligations.refresh.refresh_obligations") as refresh,
            ):
                inspections.filter.return_value.values_list.return_value = ["inspection"]
                substitutions.filter.return_value.values_list.return_value = ["substitution"]
                obligations.filter.return_value.values_list.return_value = [
                    "inspection",
                    "substitution",
                ]
                refresh_for_receipt(receipt)
                inspections.filter.assert_called_once_with(organization_id="org", command=receipt)
                substitutions.filter.assert_called_once_with(organization_id="org", command=receipt)
                self.assertEqual(
                    set(obligations.filter.call_args.kwargs["pk__in"]),
                    {"inspection", "substitution"},
                )
                refresh.assert_called_once_with("org", ["inspection", "substitution"])
