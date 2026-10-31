"""Offline negative tests for runtime grants (no production credentials)."""

import unittest

from tools.audit_runtime_verify import validate_runtime_grants


class RuntimeGrantTests(unittest.TestCase):
    def setUp(self):
        self.good = [
            "GRANT USAGE ON *.* TO 'runtime'@'%'",
            "GRANT SELECT, INSERT ON `cya`.`command_receipt` TO 'runtime'@'%'",
            "GRANT SELECT, INSERT ON `cya`.`audit_event` TO 'runtime'@'%'",
        ]

    def test_minimum_grants_accepted(self):
        validate_runtime_grants(self.good, database="cya")

    def test_schema_wide_write_denied(self):
        with self.assertRaises(ValueError):
            validate_runtime_grants(
                self.good + ["GRANT INSERT ON `cya`.* TO 'runtime'@'%'"],
                database="cya",
            )

    def test_update_and_delete_denied(self):
        for privilege in ("UPDATE", "DELETE", "TRIGGER", "ALL PRIVILEGES"):
            with self.subTest(privilege=privilege):
                with self.assertRaises(ValueError):
                    validate_runtime_grants(
                        self.good + [f"GRANT {privilege} ON `cya`.`audit_event` TO 'runtime'@'%'"],
                        database="cya",
                    )

    def test_immutable_session_header_update_denied(self):
        validate_runtime_grants(
            self.good + ["GRANT SELECT, INSERT ON `cya`.`inspection_session` TO 'runtime'@'%'"],
            database="cya",
        )
        with self.assertRaises(ValueError):
            validate_runtime_grants(
                self.good
                + ["GRANT SELECT, INSERT, UPDATE ON `cya`.`inspection_session` TO 'runtime'@'%'"],
                database="cya",
            )

    def test_role_grant_denied(self):
        with self.assertRaises(ValueError):
            validate_runtime_grants(
                self.good + ["GRANT 'editor'@'%' TO 'runtime'@'%'"], database="cya"
            )

    def test_missing_insert_denied(self):
        with self.assertRaises(ValueError):
            validate_runtime_grants(self.good[:2], database="cya")

    def test_grant_option_and_unclassified_or_cross_database_scopes_are_denied(self):
        bad = [
            self.good[-1] + " WITH GRANT OPTION",
            "GRANT SELECT ON `other`.`audit_event` TO 'runtime'@'%'",
            "GRANT SELECT ON `cya`.`unclassified` TO 'runtime'@'%'",
            "GRANT CREATE USER ON *.* TO 'runtime'@'%'",
            "GRANT PROXY ON 'other'@'%' TO 'runtime'@'%'",
            "GRANT EXECUTE ON PROCEDURE `cya`.`unsafe` TO 'runtime'@'%'",
            "SET DEFAULT ROLE 'editor' FOR 'runtime'@'%'",
        ]
        for line in bad:
            with self.subTest(kind=line.split()[0]):
                with self.assertRaises(ValueError):
                    validate_runtime_grants(self.good + [line], database="cya")


if __name__ == "__main__":
    unittest.main()
