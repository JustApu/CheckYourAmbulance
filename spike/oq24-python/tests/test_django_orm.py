"""THROWAWAY spike (Python candidate): the same concurrency semantics through the Django ORM, using the
least-privilege account, plus Django's own database defaults."""
import multiprocessing as mp
import unittest

from spikepy import db


def _child(args):
    from django import db as djdb
    name, users, i = args
    djdb.connections.close_all()                     # never share the parent's connection after fork
    from spikepy import dj
    dj.configure(name)
    from spikepy.orm_check import record_check
    user, outcome = users[i], ("FAILED" if i % 2 else "PASSED")
    op = f"d{3:07d}-0000-0000-0000-{i:012d}"
    r = record_check(op, 1, 3, outcome, user)
    trace = [r["status"]]
    for _ in range(50):
        if r["status"] != "CONFLICT":
            break
        r = record_check(op, 1, 3, outcome, user, acknowledges_check_id=r["newest_id"])
        trace.append(r["status"])
    return trace


def _replay(args):
    from django import db as djdb
    name, users = args
    djdb.connections.close_all()
    from spikepy import dj
    dj.configure(name)
    from spikepy.orm_check import record_check
    r = record_check("d0000fff-0000-0000-0000-000000000001", 1, 1, "PASSED", users[0])
    return r["status"], r["check_id"]


class DjangoOrm(unittest.TestCase):
    N = 8

    @classmethod
    def setUpClass(cls):
        cls.name = db.new_db_name()
        db.load_schema(cls.name)
        cls.adm = db.admin(cls.name)
        cls.users = db.seed(cls.adm, cls.N)
        cls.adm.cursor().execute("INSERT INTO items (name) VALUES ('Synthetic AED')")
        db.lock_rows(cls.adm)
        from spikepy import dj
        dj.configure(cls.name)

    @classmethod
    def tearDownClass(cls):
        from django import db as djdb
        djdb.connections.close_all()
        db.drop_db(cls.name)

    def count(self, sql, *a):
        c = self.adm.cursor()
        c.execute(sql, a)
        return c.fetchone()[0]

    def test_django_connection_defaults(self):
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT @@SESSION.sql_mode, @@SESSION.time_zone, @@version")
            mode, tz, version = cur.fetchone()
        self.assertIn("STRICT", mode, f"Django's default session sql_mode: {mode}")
        self.assertIn("MariaDB", version)
        print(f"\n  Django default session sql_mode={mode!r} time_zone={tz!r} server={version}")

    def test_orm_replay_distinct_and_audit(self):
        ctx = mp.get_context("fork")
        with ctx.Pool(self.N) as pool:
            res = pool.map(_replay, [(self.name, self.users)] * self.N)
            self.assertEqual(self.count("SELECT COUNT(*) FROM check_record WHERE operation_id LIKE %s", 'd0000fff%'), 1)
            self.assertEqual({r[1] for r in res}.__len__(), 1)
            self.assertEqual(sum(1 for r in res if r[0] == "COMMITTED"), 1)
            traces = pool.map(_child, [(self.name, self.users, i) for i in range(self.N)])
        self.assertEqual(self.count("SELECT COUNT(*) FROM check_record WHERE item_id=3"), self.N)
        self.assertEqual(self.count("SELECT COUNT(DISTINCT actor_user_id) FROM check_record WHERE item_id=3"), self.N)
        self.assertEqual(self.count("SELECT COUNT(*) FROM check_record WHERE item_id=3 AND outcome='FAILED'"), self.N // 2)
        self.assertGreaterEqual(sum(1 for t in traces if "CONFLICT" in t), self.N - 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM audit_event WHERE action='CHECK_RECORDED'"),
                         self.count("SELECT COUNT(*) FROM check_record WHERE operation_id LIKE %s", "d%"))

    def test_orm_errors_map_to_exceptions(self):
        from django.db import DataError, IntegrityError, ProgrammingError, OperationalError, transaction
        from spikepy.models import CheckRecord
        with self.assertRaises((DataError, OperationalError)):
            with transaction.atomic():
                CheckRecord.objects.create(operation_id="e0000000-0000-0000-0000-000000000001", vehicle_id=1, item_id=1,
                                           outcome="PASSED", explanation="x" * 500, actor_user_id=self.users[0])
        CheckRecord.objects.create(operation_id="e0000000-0000-0000-0000-000000000002", vehicle_id=1, item_id=1,
                                   outcome="PASSED", actor_user_id=self.users[0])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CheckRecord.objects.create(operation_id="e0000000-0000-0000-0000-000000000002", vehicle_id=1, item_id=1,
                                           outcome="PASSED", actor_user_id=self.users[0])
        # The ORM's update/delete paths hit the privilege wall (no UPDATE/DELETE on append-only tables).
        with self.assertRaises((OperationalError, ProgrammingError)):
            with transaction.atomic():
                CheckRecord.objects.filter(operation_id="e0000000-0000-0000-0000-000000000002").update(outcome="FAILED")
        with self.assertRaises((OperationalError, ProgrammingError)):
            with transaction.atomic():
                CheckRecord.objects.filter(operation_id="e0000000-0000-0000-0000-000000000002").delete()

    def test_django_system_check(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command("check", "--database", "default", stdout=out, stderr=out)
        print(f"\n  django check output: {out.getvalue().strip()[:300]}")


if __name__ == "__main__":
    unittest.main()
