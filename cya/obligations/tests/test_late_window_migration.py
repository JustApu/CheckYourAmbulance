"""The pinned late window (issue #80, SPEC 1.31.0): migrations 0012 and 0013 round trip.

Existing rows take 12 hours without an UPDATE, L is a stored generated column, and after 0013 the
column has no default, so a creator that omits it fails instead of silently getting 12 hours.
"""

from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from cya.obligations.models import Obligation, OdometerObligation
from cya.obligations.tests.test_generation import GenerationCase

BEFORE = ("cya_obligations", "0011_routine_scope_indexes")
AFTER = ("cya_obligations", "0013_late_window_no_default")
TABLES = {
    "obligation": "obligation_late_seconds",
    "odometer_obligation": "odometer_ob_late_seconds",
}
INDEX = "obligation_item_late_limit"


def columns(table):
    """{column: (nullable, default, extra)} for the two late-window columns."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME,IS_NULLABLE,COLUMN_DEFAULT,EXTRA FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
            "AND COLUMN_NAME IN ('late_seconds','late_limit_at')",
            [table],
        )
        return {name: (nullable, default, extra) for name, nullable, default, extra in cursor}


def constraints(table):
    with connection.cursor() as cursor:
        return connection.introspection.get_constraints(cursor, table)


def triggers():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EVENT_OBJECT_TABLE,TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE EVENT_OBJECT_SCHEMA=DATABASE() "
            "AND EVENT_OBJECT_TABLE IN ('obligation','odometer_obligation')"
        )
        return set(cursor.fetchall())


class LateWindowMigrationTests(GenerationCase):
    def assert_pinned_schema(self):
        for table, check in TABLES.items():
            with self.subTest(table=table):
                found = columns(table)
                self.assertEqual(found["late_seconds"][:2], ("NO", None))  # required, no default
                self.assertEqual(found["late_limit_at"][2], "STORED GENERATED")
                self.assertTrue(constraints(table)[check]["check"])
        self.assertEqual(
            constraints("obligation")[INDEX]["columns"],
            ["organization_id", "vehicle_id", "item_id", "kit_key", "late_limit_at"],
        )

    def test_reverse_drops_only_the_new_schema_and_forward_pins_existing_rows_to_12_hours(self):
        with override_settings(CYA_LATE_COMPLETION_HOURS=2):
            self.assertEqual(self.generate().failures, [])
        ids = {
            "obligation": set(Obligation.objects.values_list("pk", flat=True)),
            "odometer_obligation": set(OdometerObligation.objects.values_list("pk", flat=True)),
        }
        self.assertTrue(all(ids.values()))
        self.assertEqual(
            set(Obligation.objects.values_list("late_seconds", flat=True)), {7200}
        )  # captured at creation, before the round trip
        self.assert_pinned_schema()
        guards = triggers()
        self.assertTrue(guards)
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate([BEFORE])
            for table, check in TABLES.items():
                with self.subTest(step="reversed", table=table):
                    self.assertEqual(columns(table), {})
                    self.assertNotIn(check, constraints(table))
            self.assertNotIn(INDEX, constraints("obligation"))
            self.assertEqual(triggers(), guards)  # the append-only guards are untouched
        finally:
            MigrationExecutor(connection).migrate(latest)
        self.assert_pinned_schema()
        self.assertEqual(triggers(), guards)
        # The rows survive the round trip, and the migration gives every existing row the
        # documented 12 hours (SCH-3) with L = G + 12 h computed by the database.
        for model, table in (
            (Obligation, "obligation"),
            (OdometerObligation, "odometer_obligation"),
        ):
            rows = list(
                model.objects.values_list("pk", "grace_at", "late_seconds", "late_limit_at")
            )
            self.assertEqual({r[0] for r in rows}, ids[table])
            for _pk, grace_at, late_seconds, late_limit_at in rows:
                self.assertEqual(late_seconds, 43200)
                self.assertEqual(late_limit_at, grace_at + timedelta(hours=12))
        # Migrating to the same target again changes nothing and does not fail.
        MigrationExecutor(connection).migrate(latest)
        self.assertIn(AFTER, MigrationExecutor(connection).loader.applied_migrations)
        self.assert_pinned_schema()
