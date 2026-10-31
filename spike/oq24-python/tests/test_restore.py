"""THROWAWAY spike (Python candidate) criterion 5: backup + point-in-time restore + projection comparison."""
import os
import subprocess
import tempfile
import unittest

from spikepy import db
from spikepy.check import record_check


def projection(conn, dbname):
    cur = conn.cursor()
    cur.execute(f"SELECT c.item_id, c.outcome FROM `{dbname}`.check_record c JOIN "
                f"(SELECT item_id, MAX(check_id) m FROM `{dbname}`.check_record GROUP BY item_id) x ON x.m=c.check_id ORDER BY c.item_id")
    return ",".join(f"{i}={o}" for i, o in cur.fetchall())


@unittest.skipUnless(os.environ.get("SPIKE_BASE"), "SPIKE_BASE not set")
class Restore(unittest.TestCase):
    def test_pitr(self):
        base, dump, binlog, client = (os.environ[k] for k in ("SPIKE_BASE", "SPIKE_DUMP", "SPIKE_BINLOG", "SPIKE_CLIENT"))
        sock = db.sock()
        name = db.new_db_name()
        db.load_schema(name)
        adm = db.admin(name)
        users = db.seed(adm, 3)
        app = db.connect(db=name)
        n = [0]

        def add(user, item, outcome):
            n[0] += 1
            op = f"c{n[0]:07d}-0000-0000-0000-000000000000"
            r = record_check(app, op, 1, item, outcome, users[user])
            if r["status"] == "CONFLICT":
                r = record_check(app, op, 1, item, outcome, users[user], acknowledges_check_id=r["newest_id"])
            self.assertEqual(r["status"], "COMMITTED")
        try:
            add(0, 1, "PASSED"); add(1, 2, "PASSED")
            adm.cursor().execute("FLUSH BINARY LOGS")
            with tempfile.TemporaryDirectory(prefix="spkpyrestore") as tmp:
                dumpfile = f"{tmp}/full.sql"
                subprocess.run([dump, "--no-defaults", "-uroot", f"-S{sock}", "--single-transaction", "--master-data=2",
                                "--routines", "--triggers", f"--result-file={dumpfile}", name], check=True)
                with open(dumpfile) as fh:
                    text = fh.read()
                import re
                m = re.search(r"MASTER_LOG_FILE='([^']+)',\s*MASTER_LOG_POS=(\d+)", text)
                self.assertTrue(m)
                binfile, binpos = m.group(1), m.group(2)
                add(0, 1, "FAILED"); add(2, 1, "PASSED"); add(1, 2, "FAILED")
                c = adm.cursor()
                c.execute("SHOW MASTER STATUS")
                t_file, t_pos = c.fetchone()[:2]
                expected = projection(adm, name)
                c.execute(f"SELECT COUNT(*) FROM `{name}`.check_record")
                rows_at_t = c.fetchone()[0]
                add(2, 2, "PASSED"); add(0, 1, "PASSED")           # after the chosen instant: must not appear

                restored = name + "_rst"
                c.execute(f"CREATE DATABASE `{restored}`")
                with open(dumpfile, "rb") as f:
                    subprocess.run([client, "--no-defaults", "-uroot", f"-S{sock}", restored], stdin=f, check=True)
                replay = subprocess.run([binlog, "--no-defaults", f"--database={name}", f"--rewrite-db={name}->{restored}",
                                         f"--start-position={binpos}", f"--stop-position={t_pos}", f"{base}/binlog/{binfile}"],
                                        check=True, capture_output=True).stdout
                self.assertTrue(replay)
                subprocess.run([client, "--no-defaults", "-uroot", f"-S{sock}"], input=replay, check=True)
                c.execute(f"SELECT COUNT(*) FROM `{restored}`.check_record")
                self.assertEqual(c.fetchone()[0], rows_at_t)
                self.assertEqual(projection(adm, restored), expected)
                self.assertNotEqual(projection(adm, name), expected)
        finally:
            db.drop_db(name)
            db.drop_db(name + "_rst")


if __name__ == "__main__":
    unittest.main()
