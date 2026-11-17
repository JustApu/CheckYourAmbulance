"""The signed-in Home screen (issue #194 items 3 and 11)."""

import re
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from django.db import DatabaseError
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import timezone

from cya.checklists.models import Vehicle
from cya.inspections.tests.test_crew_ui import CrewUiCase
from cya.shell import home


class GreetingTests(SimpleTestCase):
    def test_first_name_is_the_first_word_of_the_display_name(self):
        self.assertEqual(home.first_name(SimpleNamespace(display_name="  Sam  Example ")), "Sam")
        self.assertEqual(home.first_name(SimpleNamespace(display_name="")), "")

    def test_part_of_day_follows_station_time(self):
        user = SimpleNamespace(display_name="Sam Example")
        cases = {6: "Good morning, Sam", 13: "Good afternoon, Sam", 21: "Good evening, Sam"}
        with timezone.override(ZoneInfo("America/Chicago")):
            for hour, text in cases.items():
                with self.subTest(hour=hour):
                    now = datetime(2026, 11, 17, hour, tzinfo=ZoneInfo("America/Chicago"))
                    self.assertEqual(home.greeting(user, now)["text"], text)

    def test_no_name_still_greets(self):
        now = datetime(2026, 11, 17, 8, tzinfo=ZoneInfo("UTC"))
        with timezone.override(ZoneInfo("UTC")):
            self.assertEqual(
                home.greeting(SimpleNamespace(display_name=""), now)["text"], "Good morning"
            )


class HomeScreenTests(CrewUiCase):
    def page(self, client=None):
        response = (client or self.client).get("/")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_crew_home_greets_and_offers_one_primary_action(self):
        html = self.page()
        self.assertRegex(html, r"<h1>Good (morning|afternoon|evening)")
        self.assertIn("station time", html)
        self.assertEqual(html.count('class="button primary"'), 1)
        self.assertIn('href="/inspection/">Start an inspection</a>', html)
        for heading in ("Due today", "Open problems"):
            self.assertIn(f">{heading}</h2>", html)
        self.assertNotIn("Needs you", html, "the supervisor sections are for supervisors")

    def test_an_open_session_shows_in_progress_with_continue(self):
        opened = self.open()
        self.assertEqual(opened.status_code, 200, opened.content)
        session_id = opened.json()["result"]["session_id"]
        html = self.page()
        self.assertIn(">In progress</h2>", html)
        self.assertIn(self.vehicle.display_name, html)
        self.assertRegex(
            html,
            rf'class="button primary" href="/inspection/sessions/{session_id}/">Continue inspection',
        )
        self.assertIn("0 items checked", html)
        self.assertNotIn("Start an inspection", html)
        # Home always covers every vehicle (owner, 2026-11-17).
        self.assertIn(">Due today</h2>", html)
        self.assertIn(">Open problems</h2>", html)
        self.assertNotIn("your vehicles", html)

    def test_supervisor_home_adds_tiles_needs_you_and_vehicles(self):
        client, user = self.harness.sign_in("home-supervisor")
        self.grant(user, "SUPERVISOR")
        html = self.page(client)
        self.assertIn("vehicles ready", html)
        self.assertIn(">Needs you</h2>", html)
        self.assertIn("Nothing is waiting for you.", html)
        self.assertIn(">Vehicles</h2>", html)
        self.assertIn(self.vehicle.display_name, html)

    def test_a_failing_section_says_so_and_the_page_still_renders(self):
        with mock.patch.object(home, "problems", side_effect=DatabaseError("synthetic")):
            with self.assertLogs("cya.shell.home", "ERROR") as logs:
                html = self.page()
        self.assertIn("Problems are unavailable right now.", html)
        self.assertIn(">Due today</h2>", html, "other sections still render")
        self.assertNotIn("synthetic", "\n".join(logs.output), "only the error class is logged")

    def test_home_targets_are_at_least_48_px(self):
        # Codex review of PR #201: Home's links and buttons keep the 48 px target rule.
        css = render_to_string("shell/shell.css")
        rule = re.search(r"\.home-list a \{[^}]*\}", css).group(0)
        self.assertIn("min-height: 48px", rule)

    def test_a_bounded_fleet_is_disclosed_not_presented_as_the_total(self):
        client, user = self.harness.sign_in("home-truncated")
        self.grant(user, "SUPERVISOR")
        with mock.patch("cya.attention.readiness.VEHICLE_LIMIT", 1):
            html = self.page(client)
        fleet = Vehicle.objects.filter(organization=self.org, active=True).count()
        self.assertGreater(fleet, 1, "the fixture has more vehicles than the patched limit")
        self.assertIn("Only the first 1 vehicles are shown here.", html)
        self.assertIn('href="/inspection/">See every vehicle in Inspect</a>', html)
        self.assertIn(f"counting the first 1 of {fleet}", html)

    def test_home_shows_no_scores_or_rankings(self):
        page = self.page()
        html = page[page.index("<main") : page.index("</main>")]  # the page content only
        for word in ("rank", "score", "leaderboard"):
            self.assertNotIn(word, html.lower())
