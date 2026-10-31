"""THROWAWAY spike (Python candidate) criterion 4: TIME-3 with the standard library (zoneinfo)."""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import MySQLdb

from spikepy import db

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def resolve_local(tz, y, m, d, H, M):
    """Nonexistent local time -> first valid instant after the gap; ambiguous -> first occurrence."""
    naive = datetime(y, m, d, H, M)
    first = naive.replace(tzinfo=tz, fold=0)
    # zoneinfo semantic: fold=0 is the first occurrence for ambiguous times; for a gap it uses the pre-gap offset,
    # which lands after the transition. Detect a gap by round-tripping through UTC.
    as_utc = first.astimezone(UTC)
    if as_utc.astimezone(tz).replace(tzinfo=None) != naive:     # nonexistent local time
        # the transition instant is the first instant with the new offset: step back by minutes.
        t = as_utc
        while (t - timedelta(minutes=1)).astimezone(tz).utcoffset() != (t).astimezone(tz).utcoffset():
            break
        # simpler and exact: the gap starts at the pre-gap offset's wall time == naive - gap; find by search
        t = as_utc
        off_new = t.astimezone(tz).utcoffset()
        while (t - timedelta(minutes=1)).astimezone(tz).utcoffset() == off_new:
            t -= timedelta(minutes=1)
        return t
    return as_utc


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class Dst(unittest.TestCase):
    def test_resolution(self):
        self.assertEqual(iso(resolve_local(NY, 2026, 1, 15, 6, 0)), "2026-01-15 11:00:00")
        self.assertEqual(iso(resolve_local(NY, 2026, 7, 15, 6, 0)), "2026-07-15 10:00:00")
        self.assertEqual(iso(resolve_local(NY, 2026, 3, 8, 2, 30)), "2026-03-08 07:00:00")   # gap -> 03:00 EDT
        self.assertEqual(iso(resolve_local(NY, 2026, 3, 8, 6, 0)), "2026-03-08 10:00:00")
        self.assertEqual(iso(resolve_local(NY, 2026, 11, 1, 1, 30)), "2026-11-01 05:30:00")  # ambiguous -> first (EDT)
        self.assertEqual(iso(resolve_local(NY, 2026, 11, 1, 6, 0)), "2026-11-01 11:00:00")
        self.assertEqual((resolve_local(NY, 2026, 3, 8, 6, 0) - resolve_local(NY, 2026, 3, 7, 6, 0)).total_seconds() / 3600, 23)
        self.assertEqual((resolve_local(NY, 2026, 11, 1, 6, 0) - resolve_local(NY, 2026, 10, 31, 6, 0)).total_seconds() / 3600, 25)

    def test_idempotent_generator(self):
        name = db.new_db_name()
        db.load_schema(name)
        adm = db.admin(name)
        try:
            db.seed(adm)
            app = db.connect(db=name)
            cur = app.cursor()
            cur.execute("SELECT @@session.time_zone")
            self.assertEqual(cur.fetchone()[0], "+00:00")

            def generate(y, m, d, days):
                made = dups = 0
                for n in range(days):
                    day = datetime(y, m, d) + timedelta(days=n)
                    due = resolve_local(NY, day.year, day.month, day.day, 6, 0)
                    try:
                        cur.execute("INSERT INTO obligation (vehicle_id,item_id,due_at_utc,early_at_utc,grace_at_utc,requirement_version) "
                                    "VALUES (1,1,%s,%s,%s,1)", (iso(due), iso(due - timedelta(hours=1)), iso(due + timedelta(hours=1))))
                        made += 1
                    except MySQLdb.IntegrityError as exc:       # plain INSERT + caught duplicate, never INSERT IGNORE
                        if exc.args[0] != 1062:
                            raise
                        dups += 1
                return made, dups
            self.assertEqual(generate(2026, 3, 5, 7), (7, 0))
            self.assertEqual(generate(2026, 3, 5, 7), (0, 7))
            self.assertEqual(generate(2026, 3, 8, 7), (3, 4))
            self.assertEqual(generate(2026, 10, 29, 6), (6, 0))
            a = adm.cursor()
            a.execute("SELECT COUNT(*), MIN(TIMESTAMPDIFF(HOUR, early_at_utc, grace_at_utc)), MAX(TIMESTAMPDIFF(HOUR, early_at_utc, grace_at_utc)) FROM obligation")
            self.assertEqual(a.fetchone(), (16, 2, 2))
        finally:
            db.drop_db(name)


if __name__ == "__main__":
    unittest.main()
