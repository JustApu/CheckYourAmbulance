"""Migration round trips preserve guards; existing explicit orders prevent reversal."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from cya.obligations.adhoc import request_inspection
from cya.obligations.tests.test_generation import GenerationCase

OLD = ("cya_obligations", "0005_obligations_command_type_guards")
NEW = ("cya_obligations", "0006_ad_hoc_obligations")


class AdHocMigrationTests(GenerationCase):
    def test_reverse_and_forward_preserve_ascii_binary_keys_and_guards(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([OLD])
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT IS_NULLABLE,COLLATION_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='requirement_version' "
                    "AND COLUMN_NAME IN ('binding_id','schedule_rule_id')"
                )
                self.assertEqual(cursor.fetchall(), (("NO", "ascii_bin"), ("NO", "ascii_bin")))
        finally:
            MigrationExecutor(connection).migrate(latest)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT IS_NULLABLE,COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='requirement_version' "
                "AND COLUMN_NAME IN ('binding_id','schedule_rule_id')"
            )
            self.assertEqual(cursor.fetchall(), (("YES", "ascii_bin"), ("YES", "ascii_bin")))
            from cya.core.audit_schema import inspect_audit_guards, inspect_command_receipt_guards

            inspect_audit_guards(cursor)
            inspect_command_receipt_guards(cursor)

    def test_reversal_refuses_before_changing_any_schema_with_an_existing_order(self):
        request_inspection(
            vehicle_id=str(self.vehicle.pk),
            items=[{"item_id": str(self.suction.pk), "kit_instance_id": None}],
            due_at=self.at(hours=6),
            explanation="Synthetic irreversible history.",
            **self.context(self.supervisor),
        )
        with self.assertRaisesRegex(RuntimeError, "cannot reverse"):
            MigrationExecutor(connection).migrate([OLD])
        self.assertIn(NEW, MigrationExecutor(connection).loader.applied_migrations)
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM obligation WHERE routine=0")
            self.assertEqual(cursor.fetchone()[0], 1)
