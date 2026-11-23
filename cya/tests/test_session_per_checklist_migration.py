"""Reverse and reapply the SES-7 session event trigger (issue #256 Part D, migration 0006)."""

from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag

BEFORE = ("cya_inspections", "0005_auto_close_guards")
AFTER = ("cya_inspections", "0006_one_session_per_checklist")
MARKER = "another session of this vehicle checklist is in progress"


def trigger_text():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='cya_session_event_bi'"
        )
        row = cursor.fetchone()
    return None if row is None else row[0]


@tag("migration_rollback")
class SessionPerChecklistMigrationTests(TransactionTestCase):
    def test_the_rewrite_only_adds_the_guard(self):
        previous = import_module("cya.inspections.migrations.0005_auto_close_guards")
        current = import_module("cya.inspections.migrations.0006_one_session_per_checklist")
        before, after = previous.event_insert(6), current.event_insert(6)
        self.assertEqual(after.replace(current.ONE_IN_PROGRESS.format(hours=6), ""), before)

    def test_reverse_restores_the_previous_trigger_and_forward_reapplies(self):
        self.assertIn(MARKER, trigger_text())
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate([BEFORE])
            reversed_text = trigger_text()
            self.assertIsNotNone(reversed_text)
            self.assertNotIn(MARKER, reversed_text)
        finally:
            MigrationExecutor(connection).migrate(latest)
        self.assertIn(MARKER, trigger_text())
        self.assertIn(AFTER, MigrationExecutor(connection).loader.applied_migrations)
