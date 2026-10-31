"""Contract drift guard for the R1 physical A/V/M table manifest.

This intentionally checks the explicit catalogue, not arbitrary backtick
mentions elsewhere in the design document.
"""

import re
import unittest
from pathlib import Path

from tools.audit_enforcement import APPEND_ONLY, MUTABLE, VERSIONED

CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "data-model-contracts.md"
CATALOGUE_ROW = re.compile(
    r"^\|\s*`([a-z_]+)`(?:,\s*`([a-z_]+)`)?\s*\|\s*([AVM])\s*\|",
    re.MULTILINE,
)


class ContractManifestTests(unittest.TestCase):
    def test_every_catalogued_table_is_classified_correctly(self):
        text = CONTRACT.read_text(encoding="utf-8")
        found = {"A": set(), "V": set(), "M": set()}
        for match in CATALOGUE_ROW.finditer(text):
            first, second, kind = match.groups()
            found[kind].add(first)
            if second:
                found[kind].add(second)
        self.assertTrue(found["A"], "contract catalogue was not parsed")
        for kind, manifest in (("A", APPEND_ONLY), ("V", VERSIONED), ("M", MUTABLE)):
            with self.subTest(kind=kind):
                self.assertFalse(
                    found[kind] - manifest,
                    f"unclassified contract {kind} tables: {sorted(found[kind] - manifest)}",
                )
                self.assertFalse(found[kind] & ((APPEND_ONLY | VERSIONED | MUTABLE) - manifest))

    def test_implicit_child_and_lock_tables_are_classified(self):
        # Explicitly documented outside single-table catalogue rows.
        self.assertIn("vehicle_override_item", VERSIONED)
        for name in ("item_lock", "kit_lock", "vehicle_lock", "deficiency_lock"):
            self.assertIn(name, MUTABLE)


if __name__ == "__main__":
    unittest.main()
