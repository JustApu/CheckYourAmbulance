from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.test import SimpleTestCase

from cya.schedules.engine import Rule, due_instants, resolve_local, window, windows


def instant(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class EngineTests(SimpleTestCase):
    def rule(self, **changes):
        return replace(Rule("America/New_York", time(6), instant("2020-01-01")), **changes)

    def test_spring_gap_resolves_exact_boundary(self):
        for wall in (time(2), time(2, 30), time(2, 59, 59, 999999)):
            with self.subTest(wall=wall):
                self.assertEqual(
                    resolve_local(date(2026, 3, 8), wall, "America/New_York"),
                    instant("2026-03-08T07:00"),
                )

    def test_fall_first_occurrence(self):
        self.assertEqual(
            resolve_local(date(2026, 11, 1), time(1, 30), "America/New_York"),
            instant("2026-11-01T05:30"),
        )

    def test_half_hour_gap_and_fold(self):
        self.assertEqual(
            resolve_local(date(2026, 10, 4), time(2, 15), "Australia/Lord_Howe"),
            instant("2026-10-03T15:30"),
        )
        self.assertEqual(
            resolve_local(date(2026, 4, 5), time(1, 45), "Australia/Lord_Howe"),
            instant("2026-04-04T14:45"),
        )

    def test_skipped_civil_date(self):
        self.assertEqual(
            resolve_local(date(2011, 12, 30), time(12), "Pacific/Apia"), instant("2011-12-30T10:00")
        )
        rule = Rule("Pacific/Apia", time(0), instant("2011-01-01"))
        self.assertEqual(
            list(due_instants(rule, instant("2011-12-30T10:00"), instant("2011-12-30T10:00"))),
            [instant("2011-12-30T10:00")],
        )

    def test_window_arithmetic_is_absolute_across_transitions(self):
        zone = ZoneInfo("America/New_York")
        cases = [
            ("2026-03-08T07:30", "2026-03-08T06:30", "2026-03-08T08:30"),
            ("2026-11-01T05:30", "2026-11-01T04:30", "2026-11-01T06:30"),
        ]
        for due, early, grace in cases:
            with self.subTest(due=due):
                result = window(instant(due).astimezone(zone))
                self.assertEqual(result.early_at, instant(early))
                self.assertEqual(result.grace_at, instant(grace))
                self.assertEqual(result.due_at - result.early_at, timedelta(hours=1))
                self.assertEqual(result.grace_at - result.due_at, timedelta(hours=1))
                self.assertIs(result.due_at.tzinfo, UTC)
        # A local clock jumps by an hour; elapsed seconds do not. Never assert
        # that naive clock subtraction measures elapsed time across DST.
        spring = window(instant(cases[0][0]))
        self.assertEqual(spring.due_at.astimezone(zone).hour, 3)
        self.assertEqual(spring.early_at.astimezone(zone).hour, 1)
        fall = window(instant(cases[1][0]))
        self.assertEqual(fall.due_at.astimezone(zone).fold, 0)
        self.assertEqual(fall.grace_at.astimezone(zone).fold, 1)

    def test_zero_asymmetric_and_microsecond_windows(self):
        due = instant("2024-02-29T12:00:00.123456")
        self.assertEqual(window(due, 0, 17).early_at, due)
        self.assertEqual(window(due, 0, 17).grace_at, due + timedelta(seconds=17))

    def test_normal_leap_day_and_inclusive_horizon(self):
        result = list(
            due_instants(self.rule(), instant("2024-02-28T11:00"), instant("2024-03-01T11:00"))
        )
        self.assertEqual(result, [instant(f"2024-{d}T11:00") for d in ("02-28", "02-29", "03-01")])
        self.assertEqual(list(due_instants(self.rule(), result[1], result[1])), [result[1]])
        self.assertEqual(
            list(
                due_instants(
                    self.rule(),
                    result[1] + timedelta(microseconds=1),
                    result[2] - timedelta(microseconds=1),
                )
            ),
            [],
        )

    def test_weekday_weekend_station_calendar(self):
        start, end = instant("2026-10-23"), instant("2026-10-27")
        for recurrence, expected in (("WEEKDAY", [23, 26]), ("WEEKEND", [24, 25])):
            result = list(due_instants(self.rule(recurrence=recurrence), start, end))
            self.assertEqual([d.day for d in result], expected)

    def test_interval_anchor_and_leap_day(self):
        rule = self.rule(recurrence="INTERVAL", interval_days=2, anchor_date=date(2024, 2, 27))
        self.assertEqual(
            [d.day for d in due_instants(rule, instant("2024-02-25"), instant("2024-03-01"))],
            [27, 29],
        )

    def test_recurrence_is_calendar_not_24_hours(self):
        for start, end, hours in (
            ("2026-03-07", "2026-03-10", [23, 24]),
            ("2026-10-31", "2026-11-03", [25, 24]),
        ):
            result = list(due_instants(self.rule(), instant(start), instant(end)))
            self.assertEqual(
                [(b - a).total_seconds() / 3600 for a, b in zip(result, result[1:], strict=False)],
                hours,
            )

    def test_effective_at_filters_due_not_early(self):
        rule = self.rule(effective_at=instant("2026-03-08T09:59"))
        result = list(windows(rule, instant("2026-03-08"), instant("2026-03-09")))
        self.assertEqual(len(result), 1)
        self.assertLess(result[0].early_at, rule.effective_at)
        rule = replace(rule, effective_at=result[0].due_at + timedelta(microseconds=1))
        self.assertEqual(list(due_instants(rule, instant("2026-03-08"), instant("2026-03-09"))), [])

    def test_fold_horizon_does_not_include_second_occurrence(self):
        rule = self.rule(wall_time=time(1, 30))
        self.assertEqual(
            list(due_instants(rule, instant("2026-11-01T06:00"), instant("2026-11-01T07:00"))), []
        )

    def test_horizon_in_non_utc_zone(self):
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 11, 1, 1, 0, tzinfo=zone, fold=0)
        end = datetime(2026, 11, 1, 1, 0, tzinfo=zone, fold=1)
        self.assertEqual(
            list(due_instants(self.rule(wall_time=time(1, 30)), start, end)),
            [instant("2026-11-01T05:30")],
        )

    def test_invalid_inputs_fail_closed(self):
        for changes in (
            {"early_seconds": -1},
            {"grace_seconds": True},
            {"early_seconds": 1.2},
            {"early_seconds": 2147483648},
            {"wall_time": time(1, tzinfo=UTC)},
            {"effective_at": datetime(2026, 1, 1)},
            {"recurrence": "CUSTOM"},
            {"interval_days": 1},
            {"recurrence": "INTERVAL"},
            {"recurrence": "INTERVAL", "interval_days": 0, "anchor_date": date(2026, 1, 1)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.rule(**changes)
        with self.assertRaises(ZoneInfoNotFoundError):
            self.rule(zone="Invalid/Zone")
        for start, end in (
            (datetime(2026, 1, 1), instant("2026-01-02")),
            (instant("2026-01-02"), instant("2026-01-01")),
        ):
            with self.assertRaises(ValueError):
                list(due_instants(self.rule(), start, end))
        with self.assertRaises(ValueError):
            window(datetime(2026, 1, 1))
