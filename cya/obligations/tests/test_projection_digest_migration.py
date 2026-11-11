"""The stored facts digest (issue #99): migration 0014 round trip and its values on existing rows."""

import hashlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from cya.obligations.models import ObligationProjection
from cya.obligations.tests.test_generation import GenerationCase

BEFORE = ("cya_obligations", "0013_late_window_no_default")
AFTER = ("cya_obligations", "0014_projection_facts_digest")


def digest_column():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='obligation_projection' AND COLUMN_NAME='facts_digest'"
        )
        found = cursor.fetchone()
    return None if found is None else found[0]


def stored_rows():
    with connection.cursor() as cursor:
        cursor.execute("SELECT obligation_id, facts, facts_digest FROM obligation_projection")
        return cursor.fetchall()


class ProjectionDigestMigrationTests(GenerationCase):
    def assert_digests_match_text(self):
        rows = stored_rows()
        self.assertTrue(rows)
        for oid, text, digest in rows:
            with self.subTest(obligation=oid):
                self.assertEqual(digest, hashlib.sha256(text.encode()).hexdigest())

    def test_reverse_drops_only_the_column_and_forward_fills_it_for_existing_rows(self):
        self.assertEqual(self.generate().failures, [])
        before = set(ObligationProjection.objects.values_list("obligation_id", flat=True))
        self.assertTrue(before)
        self.assertEqual(digest_column(), "STORED GENERATED")
        self.assert_digests_match_text()
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate([BEFORE])
            self.assertIsNone(digest_column())
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM obligation_projection")
                self.assertEqual(cursor.fetchone()[0], len(before))
        finally:
            MigrationExecutor(connection).migrate(latest)
        self.assertEqual(digest_column(), "STORED GENERATED")
        self.assertEqual(
            set(ObligationProjection.objects.values_list("obligation_id", flat=True)), before
        )
        self.assert_digests_match_text()  # existing rows got their digest from the migration
        MigrationExecutor(connection).migrate(latest)  # reapplying changes nothing
        self.assertIn(AFTER, MigrationExecutor(connection).loader.applied_migrations)

    def test_a_refresh_write_keeps_the_digest_in_step_with_the_facts(self):
        self.assertEqual(self.generate().failures, [])
        row = ObligationProjection.objects.order_by("pk").first()
        changed = {**row.facts, "obligation": {**row.facts["obligation"], "explanation": "Marker"}}
        ObligationProjection.objects.filter(pk=row.pk).update(facts=changed)
        self.assert_digests_match_text()
