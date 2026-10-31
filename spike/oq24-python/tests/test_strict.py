"""THROWAWAY spike (Python candidate) criterion 3: strict data handling."""
import unittest

import MySQLdb

from spikepy import db

INS = ("INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, explanation, actor_user_id) "
       "VALUES (%s,1,1,%s,%s,%s)")


class Strict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = db.new_db_name()
        db.load_schema(cls.name)
        cls.adm = db.admin(cls.name)
        cls.user = db.seed(cls.adm)[0]
        cls.app = db.connect(db=cls.name)

    @classmethod
    def tearDownClass(cls):
        db.drop_db(cls.name)

    def test_rejections(self):
        c = self.app.cursor()
        for label, args in {
            "oversized": ("a0000000-0000-0000-0000-000000000001", "FAILED", "x" * 500, self.user),
            "bad enum": ("a0000000-0000-0000-0000-000000000002", "MAYBE", None, self.user),
            "unable without reason (CHECK)": ("a0000000-0000-0000-0000-000000000004", "UNABLE", None, self.user),
        }.items():
            with self.subTest(label), self.assertRaises(MySQLdb.Error):
                c.execute(INS, args)
        with self.assertRaises(MySQLdb.Error):   # foreign key
            c.execute("INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, actor_user_id) "
                      "VALUES ('a0000000-0000-0000-0000-000000000003',999,1,'PASSED',%s)", (self.user,))
        c.execute(INS, ("a0000000-0000-0000-0000-000000000006", "PASSED", None, self.user))
        with self.assertRaises(MySQLdb.IntegrityError) as ctx:
            c.execute(INS, ("a0000000-0000-0000-0000-000000000006", "PASSED", None, self.user))
        self.assertEqual(ctx.exception.args[0], 1062)

    def test_default_session_mode_and_nonstrict_risk(self):
        # What does the driver give us with NO explicit sql_mode? (informational, recorded in the report)
        raw = MySQLdb.connect(unix_socket=db.sock(), user="app", passwd="spike-local-only", db=self.name)
        cur = raw.cursor()
        cur.execute("SELECT @@SESSION.sql_mode")
        default_mode = cur.fetchone()[0]
        self.assertIn("STRICT", default_mode, f"driver default session mode was: {default_mode}")
        # A deliberately non-strict session silently truncates (risk demonstrated).
        loose = db.connect(db=self.name)
        loose.cursor().execute("SET SESSION sql_mode=''")
        loose.cursor().execute(INS, ("a0000000-0000-0000-0000-000000000007", "PASSED", "y" * 500, self.user))
        a = self.adm.cursor()
        a.execute("SELECT CHAR_LENGTH(explanation) FROM check_record WHERE operation_id='a0000000-0000-0000-0000-000000000007'")
        self.assertEqual(a.fetchone()[0], 120)
        with self.assertRaises(MySQLdb.Error):   # CHECK still enforced
            loose.cursor().execute(INS, ("a0000000-0000-0000-0000-000000000008", "UNABLE", None, self.user))


if __name__ == "__main__":
    unittest.main()
