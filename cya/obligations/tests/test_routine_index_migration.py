"""The routine-scope indexes (issue #74) are created and dropped by one reversible migration."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = ("cya_obligations", "0010_projection_vehicle_lookup")
AFTER = ("cya_obligations", "0011_routine_scope_indexes")
INDEXES = {
    "obligation": ("obligation_vehicle_routine", ["organization_id", "vehicle_id", "routine"]),
    "obligation_projection": (
        "projection_vehicle_routine",
        ["organization_id", "vehicle_id", "routine"],
    ),
}


def index_columns(table, name):
    with connection.cursor() as cursor:
        found = connection.introspection.get_constraints(cursor, table).get(name)
    return None if found is None else found["columns"]


class RoutineIndexMigrationTests(TransactionTestCase):
    def test_indexes_exist_with_the_scope_columns_in_order(self):
        for table, (name, columns) in INDEXES.items():
            with self.subTest(table=table):
                self.assertEqual(index_columns(table, name), columns)

    def test_reverse_drops_only_these_indexes_and_forward_recreates_them_idempotently(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate([BEFORE])
            for table, (name, _columns) in INDEXES.items():
                with self.subTest(step="reversed", table=table):
                    self.assertIsNone(index_columns(table, name))
            # The earlier window indexes are untouched by the reversal.
            self.assertIsNotNone(index_columns("obligation", "obligation_vehicle_due"))
            self.assertIsNotNone(index_columns("obligation_projection", "projection_vehicle_due"))
        finally:
            MigrationExecutor(connection).migrate(latest)
        for table, (name, columns) in INDEXES.items():
            with self.subTest(step="reapplied", table=table):
                self.assertEqual(index_columns(table, name), columns)
        # Migrating to the same target again changes nothing and does not fail.
        MigrationExecutor(connection).migrate(latest)
        self.assertIn(AFTER, MigrationExecutor(connection).loader.applied_migrations)
        for table, (name, columns) in INDEXES.items():
            self.assertEqual(index_columns(table, name), columns)
