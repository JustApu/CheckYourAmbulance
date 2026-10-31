"""Offline tests for the audit enforcement DDL generators.

Real MariaDB runtime coverage lives in cya/tests/test_audit_runtime_db.py.
"""

import unittest

from tools.audit_enforcement import (
    APPEND_ONLY,
    MUTABLE,
    VERSIONED,
    append_only_triggers,
    immutable_header_guard,
    quote_identifier,
    validate_manifest,
)


class AuditEnforcementTests(unittest.TestCase):
    def test_manifest_classes_are_disjoint(self):
        self.assertFalse(APPEND_ONLY & VERSIONED)
        self.assertFalse(APPEND_ONLY & MUTABLE)
        self.assertFalse(VERSIONED & MUTABLE)

    def test_audit_and_receipt_protected(self):
        for name in ("audit_event", "command_receipt", "role_grant", "notification_outbox"):
            with self.subTest(name=name):
                self.assertIn(name, APPEND_ONLY)
                update, delete = append_only_triggers(name)
                self.assertIn("BEFORE UPDATE", update)
                self.assertIn("BEFORE DELETE", delete)
                self.assertIn("SIGNAL SQLSTATE '45000'", update)

    def test_unknown_table_cannot_get_trigger(self):
        with self.assertRaises(ValueError):
            append_only_triggers("user")

    def test_unsafe_identifiers_rejected(self):
        for name in ("", "9invalid", "x; DROP TABLE audit_event", "bad-name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                quote_identifier(name)

    def test_header_immutability_null_safe(self):
        sql = immutable_header_guard("deficiency")
        self.assertIn("BINARY OLD.`item_id` <=> BINARY NEW.`item_id`", sql)
        self.assertIn("BINARY OLD.`reported_at` <=> BINARY NEW.`reported_at`", sql)
        self.assertIn("BINARY OLD.`organization_id` <=> BINARY NEW.`organization_id`", sql)
        with self.assertRaises(ValueError):
            immutable_header_guard("audit_event")

    def test_new_unclassified_tables_detected(self):
        unknown, missing = validate_manifest({"audit_event", "unclassified_table"})
        self.assertEqual(unknown, {"unclassified_table"})
        self.assertIn("command_receipt", missing)


if __name__ == "__main__":
    unittest.main()
