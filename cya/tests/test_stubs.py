import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from cya.integrations import (
    IntegrationNotConfigured,
    RosterUnavailable,
    get_oidc_provider,
    get_roster_provider,
)
from cya.testing.fixtures import FIXTURE_DIR, FixtureError, load_fixture
from cya.testing.stubs import activate_stub_google, fixture_roster_provider


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class InjectionTests(SimpleTestCase):
    @override_settings(CYA_OIDC_PROVIDER=None, CYA_ROSTER_PROVIDER=None)
    def test_unconfigured_integrations_fail_closed(self):
        with self.assertRaises(IntegrationNotConfigured):
            get_oidc_provider()
        with self.assertRaises(IntegrationNotConfigured):
            get_roster_provider()

    @override_settings(
        CYA_OIDC_PROVIDER="cya.testing.stubs.active_stub_google",
        CYA_ROSTER_PROVIDER="cya.testing.stubs.fixture_roster_provider",
    )
    def test_settings_inject_the_stubs(self):
        google = activate_stub_google()
        self.assertIs(get_oidc_provider(), google)
        self.assertTrue(
            get_roster_provider().shifts_overlapping(utc(2026, 1, 15), utc(2026, 1, 16))
        )


class RosterStubTests(SimpleTestCase):
    def test_returns_overlapping_shifts_in_utc_order(self):
        provider = fixture_roster_provider()
        # base.json: r00 05:00-11:00Z and r06 11:00-17:00Z on 2026-01-15.
        shifts = provider.shifts_overlapping(utc(2026, 1, 15, 10, 59), utc(2026, 1, 15, 11, 1))
        self.assertEqual([s.shift_id for s in shifts], ["r00", "r06"])
        self.assertEqual(shifts[0].start, utc(2026, 1, 15, 5))
        self.assertEqual(shifts[0].user_ids, ("e1", "e2"))

    def test_half_open_interval_excludes_touching_shift(self):
        shifts = fixture_roster_provider().shifts_overlapping(
            utc(2026, 1, 15, 11), utc(2026, 1, 15, 12)
        )
        self.assertEqual([s.shift_id for s in shifts], ["r06"])

    def test_unavailable_roster_fails_closed(self):
        with self.assertRaises(RosterUnavailable):
            fixture_roster_provider(available=False).shifts_overlapping(
                utc(2026, 1, 15), utc(2026, 1, 16)
            )

    def test_rejects_naive_or_reversed_intervals(self):
        provider = fixture_roster_provider()
        with self.assertRaises(ValueError):
            provider.shifts_overlapping(datetime(2026, 1, 15), utc(2026, 1, 16))
        with self.assertRaises(ValueError):
            provider.shifts_overlapping(utc(2026, 1, 16), utc(2026, 1, 15))


class FixtureLoaderTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")

    def test_every_repository_fixture_is_marked_synthetic(self):
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            with self.subTest(fixture=path.name):
                self.assertIs(load_fixture(path.name)["synthetic"], True)

    def test_rejects_unmarked_malformed_missing_and_path_names(self):
        self.write("unmarked.json", json.dumps({"users": []}))
        self.write("broken.json", "{")
        for name in ("unmarked.json", "broken.json", "missing.json", "../base.json", "base.txt", 3):
            with self.subTest(name=name):
                with self.assertRaises(FixtureError):
                    load_fixture(name, fixture_dir=self.dir)

    def test_malformed_fixture_rosters_are_reported(self):
        naive = {"id": "r", "users": [], "start": "2026-01-15T00:00:00", "end": "2026-01-15T06:00Z"}
        cases = [
            ("naive.json", {"rosters": [naive]}, fixture_roster_provider, "no UTC offset"),
            (
                "bad_time.json",
                {"rosters": [{**naive, "start": "soon"}]},
                fixture_roster_provider,
                "invalid instant",
            ),
            (
                "no_users.json",
                {"rosters": [{"id": "r"}]},
                fixture_roster_provider,
                "malformed roster",
            ),
            (
                "int_user.json",
                {"rosters": [{**naive, "users": [7]}]},
                fixture_roster_provider,
                "malformed roster",
            ),
            ("no_lists.json", {}, fixture_roster_provider, "'rosters' must be a list"),
        ]
        for name, data, factory, message in cases:
            with self.subTest(fixture=name):
                self.write(name, json.dumps({"synthetic": True, **data}))
                with self.assertRaisesRegex(FixtureError, message):
                    factory(name, fixture_dir=self.dir)
