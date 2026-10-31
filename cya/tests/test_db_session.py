import copy
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connection, connections
from django.test import SimpleTestCase, TestCase

from cya.core.db import SETTINGS_QUERY, session_problems, verify_session_settings

GOOD_ROW = (
    "+00:00",
    "STRICT_ALL_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ZERO_DATE,NO_ZERO_IN_DATE,"
    "NO_ENGINE_SUBSTITUTION",
    1,
    1,
    "READ-COMMITTED",
)


class SessionProblemsTests(SimpleTestCase):
    def test_good_row_has_no_problems(self):
        self.assertEqual(session_problems(GOOD_ROW), [])

    def test_each_unsafe_value_is_reported(self):
        cases = {
            0: ("SYSTEM", "time_zone"),
            1: ("STRICT_TRANS_TABLES", "lacks ERROR_FOR_DIVISION_BY_ZERO"),
            2: (0, "foreign_key_checks"),
            3: (0, "check_constraint_checks"),
            4: ("REPEATABLE-READ", "tx_isolation"),
        }
        for index, (value, fragment) in cases.items():
            with self.subTest(field=index):
                row = list(GOOD_ROW)
                row[index] = value
                problems = session_problems(tuple(row))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(fragment, problems[0])

    def test_empty_or_malformed_results_are_problems(self):
        row = list(GOOD_ROW)
        row[1] = ""
        self.assertIn("lacks", session_problems(tuple(row))[0])
        for bad in (None, (), GOOD_ROW[:4]):
            with self.subTest(row=bad):
                self.assertIn("unexpected", session_problems(bad)[0])


class LiveSessionTests(TestCase):
    """Run against the real MariaDB 10.11 test database."""

    def test_server_is_mariadb_10_11(self):
        self.assertTrue(connection.mysql_is_mariadb)
        self.assertEqual(connection.mysql_version[:2], (10, 11))

    def test_new_connections_have_the_required_settings(self):
        with connection.cursor() as cursor:
            cursor.execute(SETTINGS_QUERY)
            self.assertEqual(session_problems(cursor.fetchone()), [])

    def probe(self, alias, **options):
        """A separate connection to the test database, so closing it is harmless."""
        settings_dict = copy.deepcopy(connection.settings_dict)
        settings_dict["OPTIONS"].update(options)
        # django.db.connection is a proxy; build a real wrapper of the same backend.
        other = connections["default"].__class__(settings_dict, alias=alias)
        self.addCleanup(other.close)
        return other

    def test_check_refuses_and_closes_a_session_changed_after_connect(self):
        other = self.probe("changed_session_probe")
        other.ensure_connection()
        with other.cursor() as cursor:
            cursor.execute("SET SESSION sql_mode = ''")
        with self.assertRaisesRegex(ImproperlyConfigured, r"lacks .*STRICT_ALL_TABLES"):
            verify_session_settings(sender=None, connection=other)
        self.assertIsNone(other.connection)

    def test_connection_with_unsafe_init_command_is_refused(self):
        other = self.probe("unsafe_session_probe", init_command="SET time_zone = 'SYSTEM'")
        for attempt in (1, 2):  # a retry must not reuse the refused session
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(ImproperlyConfigured, "time_zone"):
                    other.ensure_connection()
                self.assertIsNone(other.connection)

    def test_failed_verification_query_closes_and_retry_verifies_again(self):
        other = self.probe("failing_check_probe", init_command="SET sql_mode = ''")
        with mock.patch("cya.core.db.SETTINGS_QUERY", "SELECT @@session.no_such_setting"):
            with self.assertRaises(DatabaseError):
                other.ensure_connection()
        self.assertIsNone(other.connection)
        # With the real query back, the retry reconnects and refuses the unsafe session.
        with self.assertRaisesRegex(ImproperlyConfigured, r"lacks .*STRICT_ALL_TABLES"):
            other.ensure_connection()
        self.assertIsNone(other.connection)
