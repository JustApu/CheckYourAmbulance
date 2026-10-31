"""Small InnoDB reproduction of the dumped-schema reference-index reversal (issue 145 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/145))."""

from importlib import import_module

from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase

migration = import_module("cya.checks.migrations.0005_check_conflict_basis")
corrections = import_module("cya.checks.migrations.0006_check_corrections")
PARENT = "cya_145_org"
CHILD = "cya_145_check"
KEY_COLUMNS = {
    "cya_145_basis_check_id_org_fk": "basis_check_id",
    "cya_145_acknowledges_check_id_org_fk": "acknowledges_check_id",
    "cya_145_predecessor_org_fk": "predecessor_check_id",
}
COLUMNS = tuple(KEY_COLUMNS.values())


class ReferenceIndexMigrationTests(TransactionTestCase):
    def setUp(self):
        self.addCleanup(self.drop_tables)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE TABLE {PARENT} (id INT PRIMARY KEY) ENGINE=InnoDB")
            cursor.execute(
                f"CREATE TABLE {CHILD} (id INT PRIMARY KEY, organization_id INT NOT NULL, "
                "basis_check_id INT NULL, acknowledges_check_id INT NULL, "
                "predecessor_check_id INT NULL UNIQUE, "
                "UNIQUE KEY cya_145_org_id (organization_id,id), "
                f"CONSTRAINT cya_145_org_fk FOREIGN KEY (organization_id) REFERENCES {PARENT}(id)) "
                "ENGINE=InnoDB"
            )
            self.forward(cursor)
            cursor.execute(f"INSERT INTO {PARENT} VALUES (1),(2)")
            cursor.execute(f"INSERT INTO {CHILD} (id,organization_id) VALUES (101,1),(202,2)")

    def drop_tables(self):
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {CHILD}")
            cursor.execute(f"DROP TABLE IF EXISTS {PARENT}")

    @staticmethod
    def isolated(statement):
        # Exercise the migration's actual key SQL, scoped to minimal synthetic tables.
        return statement.replace("`check_record`", f"`{CHILD}`").replace(
            "cya_check_record_", "cya_145_"
        )

    def forward(self, cursor):
        statements = [
            s
            for s in [*migration.FORWARD, *corrections._sql()]
            if s.startswith("ALTER TABLE `check_record`")
        ]
        self.assertEqual(len(statements), len(COLUMNS))
        for statement in statements:
            cursor.execute(self.isolated(statement))

    def keys(self, cursor):
        cursor.execute(
            "SELECT CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY CONSTRAINT_NAME,ORDINAL_POSITION",
            [CHILD],
        )
        return cursor.fetchall()

    def round_trip(self, cursor):
        before = self.keys(cursor)
        for statement in [*corrections._reverse_sql(), *migration.REVERSE]:
            if statement.startswith("ALTER TABLE `check_record`") or (
                statement.startswith("DROP INDEX") and statement.endswith("ON `check_record`")
            ):
                cursor.execute(self.isolated(statement))
        for column in COLUMNS:
            cursor.execute(f"ALTER TABLE {CHILD} DROP COLUMN {column}")
        cursor.execute(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            [CHILD],
        )
        names = {row[0] for row in cursor.fetchall()}
        for name in KEY_COLUMNS:
            self.assertNotIn(name, names)
        for column in COLUMNS:
            unique = " UNIQUE" if column == "predecessor_check_id" else ""
            cursor.execute(f"ALTER TABLE {CHILD} ADD COLUMN {column} INT NULL{unique}")
        self.forward(cursor)
        self.assertEqual(self.keys(cursor), before)
        cursor.execute(f"SHOW INDEX FROM {CHILD}")
        indexes = {}
        for row in cursor.fetchall():
            indexes.setdefault(row[2], []).append(row[4])
        cursor.execute(
            f"INSERT INTO {CHILD} (id,organization_id,basis_check_id,acknowledges_check_id,"
            "predecessor_check_id) VALUES (303,1,101,101,101)"
        )
        cursor.execute(f"DELETE FROM {CHILD} WHERE id=303")
        for name, column in KEY_COLUMNS.items():
            self.assertEqual(indexes[name], ["organization_id", column])
            with self.assertRaises(DatabaseError), transaction.atomic():
                cursor.execute(
                    f"INSERT INTO {CHILD} (id,organization_id,{column}) VALUES (303,1,202)"
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            cursor.execute(f"INSERT INTO {CHILD} (id,organization_id) VALUES (303,3)")
        cursor.execute("SELECT @@foreign_key_checks,@@check_constraint_checks")
        self.assertEqual(cursor.fetchone(), (1, 1))

    def test_reference_keys_round_trip_on_a_migrated_table(self):
        with connection.cursor() as cursor:
            for _ in range(2):
                self.round_trip(cursor)

    def test_reference_keys_round_trip_after_dump_style_recreation(self):
        with connection.cursor() as cursor:
            cursor.execute(f"SHOW CREATE TABLE {CHILD}")
            definition = cursor.fetchone()[1]
            cursor.execute(f"DROP TABLE {CHILD}")
            cursor.execute(definition)
            cursor.execute(f"INSERT INTO {CHILD} (id,organization_id) VALUES (101,1),(202,2)")
            for _ in range(2):
                self.round_trip(cursor)
