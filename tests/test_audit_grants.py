import unittest

from tools.audit_enforcement import APPEND_ONLY
from tools.audit_grants import (
    assert_runtime_table_privileges,
    feature_table_grants,
    quote_sql_string,
    runtime_grants,
)


class AuditGrantTests(unittest.TestCase):
    def test_all_declared_tables_get_only_select_insert(self):
        grants = runtime_grants(
            database="cya",
            username="cya_runtime",
            host="%",
            existing_tables=set(APPEND_ONLY),
        )
        self.assertEqual(len(grants), len(APPEND_ONLY))
        self.assertTrue(all(g.startswith("GRANT SELECT, INSERT ON `cya`.") for g in grants))
        self.assertTrue(all(" TO 'cya_runtime'@'%'" in g for g in grants))

    def test_missing_table_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "audit_event"):
            runtime_grants(
                database="cya",
                username="runtime",
                host="localhost",
                existing_tables=set(APPEND_ONLY) - {"audit_event"},
            )

    def test_option_a_grants_only_implemented_tables(self):
        grants = runtime_grants(
            database="cya",
            username="runtime",
            host="%",
            existing_tables={"command_receipt", "audit_event", "user", "organization"},
        )
        self.assertEqual(len(grants), 2)
        with self.assertRaises(ValueError):
            runtime_grants(
                database="cya",
                username="runtime",
                host="%",
                existing_tables={"command_receipt", "audit_event", "unknown_table"},
            )

    def test_feature_tables_get_no_delete_and_update_only_on_mutable(self):
        grants = feature_table_grants(
            database="cya",
            username="runtime",
            host="%",
            existing_tables={
                "vehicle",  # M
                "inspection_session",  # immutable M header
                "deficiency",  # immutable M header
                "checklist_item_def",  # V
                "checklist_publication",  # A: granted by runtime_grants, not here
                "user",  # identity anchor: granted separately
                "django_migrations",
                "not_in_manifest",
            },
        )
        self.assertEqual(
            grants,
            [
                "GRANT SELECT, INSERT ON `cya`.`checklist_item_def` TO 'runtime'@'%'",
                "GRANT SELECT, INSERT ON `cya`.`deficiency` TO 'runtime'@'%'",
                "GRANT SELECT, INSERT ON `cya`.`inspection_session` TO 'runtime'@'%'",
                "GRANT SELECT, INSERT, UPDATE ON `cya`.`vehicle` TO 'runtime'@'%'",
            ],
        )
        self.assertFalse(any("DELETE" in g for g in grants))

    def test_quoted_accounts_and_unsafe_identifier(self):
        self.assertEqual(quote_sql_string("O'Brien"), "'O''Brien'")
        with self.assertRaises(ValueError):
            quote_sql_string("unsafe\\account")
        with self.assertRaises(ValueError):
            runtime_grants(
                database="cya;drop",
                username="runtime",
                host="%",
                existing_tables=set(APPEND_ONLY),
            )

    def test_runtime_permissions_all_tables(self):
        actual = {t: {"SELECT", "INSERT"} for t in APPEND_ONLY}
        assert_runtime_table_privileges(actual)
        actual["audit_event"].add("UPDATE")
        with self.assertRaisesRegex(ValueError, "forbidden"):
            assert_runtime_table_privileges(actual)

    def test_missing_insert_fails_closed(self):
        actual = {t: {"SELECT", "INSERT"} for t in APPEND_ONLY}
        actual["command_receipt"].remove("INSERT")
        with self.assertRaisesRegex(ValueError, "SELECT/INSERT"):
            assert_runtime_table_privileges(actual)


if __name__ == "__main__":
    unittest.main()
