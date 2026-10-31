"""THROWAWAY spike (Python candidate) criterion 1: append-only enforcement by privilege and trigger."""
import unittest

import MySQLdb

from spikepy import db


class Privileges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = db.new_db_name()
        db.load_schema(cls.name)
        cls.adm = db.admin(cls.name)
        cls.users = db.seed(cls.adm)
        cls.app = db.connect(db=cls.name)
        cls.app.cursor().execute(
            "INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, actor_user_id) "
            "VALUES ('11111111-1111-1111-1111-111111111111',1,1,'PASSED',%s)", (cls.users[0],))

    @classmethod
    def tearDownClass(cls):
        db.drop_db(cls.name)

    def test_appends_succeed(self):
        cur = self.app.cursor()
        cur.execute("INSERT INTO audit_event (actor_id, action) VALUES (%s, 'TEST')", (self.users[0],))
        cur.execute("INSERT INTO check_event (operation_id, check_id, event_type, new_outcome, actor_user_id) "
                    "VALUES ('22222222-2222-2222-2222-222222222222',1,'CORRECTION','FAILED',%s)", (self.users[0],))

    def test_mutations_denied(self):
        statements = {
            "UPDATE": "UPDATE check_record SET outcome='FAILED' WHERE check_id=1",
            "DELETE": "DELETE FROM check_record WHERE check_id=1",
            "TRUNCATE": "TRUNCATE TABLE check_record",
            "DROP TABLE": "DROP TABLE check_record",
            "ALTER": "ALTER TABLE check_record ADD COLUMN x INT",
            "DROP TRIGGER": "DROP TRIGGER check_record_no_update",
            "REPLACE": "REPLACE INTO check_record (check_id, operation_id, vehicle_id, item_id, outcome, actor_user_id) "
                       "VALUES (1,'33333333-3333-3333-3333-333333333333',1,1,'PASSED',1)",
            "ON DUPLICATE KEY UPDATE": "INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, actor_user_id) "
                       "VALUES ('11111111-1111-1111-1111-111111111111',1,1,'PASSED',1) ON DUPLICATE KEY UPDATE outcome='FAILED'",
        }
        for name, sql in statements.items():
            with self.subTest(name), self.assertRaises(MySQLdb.OperationalError) as ctx:
                self.app.cursor().execute(sql)
            self.assertIn(ctx.exception.args[0], (1142, 1419), name)   # 1142 table privilege; 1419 needs TRIGGER/SUPER
        cur = self.adm.cursor()
        cur.execute("SELECT outcome FROM check_record WHERE check_id=1")
        self.assertEqual(cur.fetchone()[0], "PASSED")

    def test_triggers_block_misgranted_account(self):
        cur = self.adm.cursor()
        cur.execute("CREATE USER IF NOT EXISTS 'oopspy'@'localhost' IDENTIFIED BY 'x'")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{self.name}`.check_record TO 'oopspy'@'localhost'")
        try:
            oops = db.connect("oopspy", "x", self.name)
            with self.assertRaises(MySQLdb.OperationalError) as ctx:
                oops.cursor().execute("UPDATE check_record SET outcome='FAILED' WHERE check_id=1")
            self.assertIn("append-only", str(ctx.exception))
        finally:
            cur.execute("DROP USER 'oopspy'@'localhost'")

    def test_author_only_correction_and_invalidation_reason(self):
        cur = self.app.cursor()
        with self.assertRaises(MySQLdb.OperationalError) as ctx:
            cur.execute("INSERT INTO check_event (operation_id, check_id, event_type, new_outcome, actor_user_id) "
                        "VALUES ('44444444-4444-4444-4444-444444444444',1,'CORRECTION','PASSED',%s)", (self.users[1],))
        self.assertIn("original author", str(ctx.exception))
        with self.assertRaises(MySQLdb.Error):
            cur.execute("INSERT INTO check_event (operation_id, check_id, event_type, actor_user_id) "
                        "VALUES ('66666666-6666-6666-6666-666666666666',1,'INVALIDATION',%s)", (self.users[1],))


if __name__ == "__main__":
    unittest.main()
