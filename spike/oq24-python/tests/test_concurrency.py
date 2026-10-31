"""THROWAWAY spike (Python candidate) criterion 2: concurrent idempotent check recording (raw driver)."""
import multiprocessing as mp
import unittest

from spikepy import db
from spikepy.check import record_check

N = 8


def op(a, b):
    return f"b{a:07d}-0000-0000-0000-{b:012d}"


def _replay(args):
    name, users = args
    conn = db.connect(db=name)
    r = record_check(conn, op(1, 1), 1, 1, "PASSED", users[0])
    return r["status"], r["check_id"]


def _distinct(args):
    name, users, i = args
    conn = db.connect(db=name)
    user, outcome = users[i], ("FAILED" if i % 2 else "PASSED")
    r = record_check(conn, op(3, i), 1, 3, outcome, user)
    trace = [r["status"]]
    tries = 0
    while r["status"] == "CONFLICT" and tries < 50:
        tries += 1
        r = record_check(conn, op(3, i), 1, 3, outcome, user, acknowledges_check_id=r["newest_id"])
        trace.append(r["status"])
    return trace


class Concurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = db.new_db_name()
        db.load_schema(cls.name)
        cls.adm = db.admin(cls.name)
        cls.users = db.seed(cls.adm, N)
        cls.adm.cursor().execute("INSERT INTO items (name) VALUES ('Synthetic AED')")
        db.lock_rows(cls.adm)
        cls.pool = mp.get_context("fork").Pool(N)

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()
        cls.pool.join()
        db.drop_db(cls.name)

    def count(self, sql, *args):
        c = self.adm.cursor()
        c.execute(sql, args)
        return c.fetchone()[0]

    def test_replay_and_distinct_operations(self):
        res = self.pool.map(_replay, [(self.name, self.users)] * N)
        self.assertEqual(self.count("SELECT COUNT(*) FROM check_record WHERE item_id=1"), 1)
        self.assertEqual(len({r[1] for r in res}), 1)
        self.assertEqual(sum(1 for r in res if r[0] == "COMMITTED"), 1)

        conn = db.connect(db=self.name)
        first = record_check(conn, op(2, 1), 1, 2, "PASSED", self.users[0])
        again = record_check(conn, op(2, 1), 1, 2, "PASSED", self.users[0])
        self.assertEqual((again["status"], again["check_id"]), ("REPLAY", first["check_id"]))

        traces = self.pool.map(_distinct, [(self.name, self.users, i) for i in range(N)])
        self.assertEqual(self.count("SELECT COUNT(*) FROM check_record WHERE item_id=3"), N)
        self.assertEqual(self.count("SELECT COUNT(DISTINCT actor_user_id) FROM check_record WHERE item_id=3"), N)
        self.assertEqual(self.count("SELECT COUNT(*) FROM check_record WHERE item_id=3 AND outcome='FAILED'"), N // 2)
        self.assertGreaterEqual(sum(1 for t in traces if "CONFLICT" in t), N - 1)
        self.assertEqual(self.count("SELECT COUNT(*) - COUNT(DISTINCT operation_id) FROM check_record"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM audit_event WHERE action='CHECK_RECORDED'"),
                         self.count("SELECT COUNT(*) FROM check_record"))

    def test_warning_leaves_operation_id_unused(self):
        a, b = db.connect(db=self.name), db.connect(db=self.name)
        adm = self.adm.cursor()
        adm.execute("INSERT INTO items (name) VALUES ('Synthetic glucometer')")
        db.lock_rows(self.adm)
        item = self.count("SELECT MAX(item_id) FROM items")
        record_check(a, op(4, 1), 1, item, "PASSED", self.users[0])
        warn = record_check(b, op(4, 2), 1, item, "FAILED", self.users[1])
        self.assertEqual(warn["status"], "CONFLICT")
        self.assertEqual(self.count("SELECT COUNT(*) FROM check_record WHERE operation_id=%s", op(4, 2)), 0)
        record_check(a, op(4, 3), 1, item, "PASSED", self.users[2], acknowledges_check_id=warn["newest_id"])
        stale = record_check(b, op(4, 2), 1, item, "FAILED", self.users[1], acknowledges_check_id=warn["newest_id"])
        self.assertEqual(stale["status"], "CONFLICT")
        self.assertNotEqual(stale["newest_id"], warn["newest_id"])
        done = record_check(b, op(4, 2), 1, item, "FAILED", self.users[1], acknowledges_check_id=stale["newest_id"])
        self.assertEqual(done["status"], "COMMITTED")


if __name__ == "__main__":
    unittest.main()
