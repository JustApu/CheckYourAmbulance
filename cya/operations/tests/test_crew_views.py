"""Vehicle crew screens (SPEC 15.4 ASG-2 to ASG-8, AC-39, issue #202 part 2)."""

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from cya.inspections.tests.test_crew_ui import CrewUiCase
from cya.operations.assignments import current_assignment, current_crew
from cya.operations.crew_views import until_instant, when
from cya.operations.models import VehicleAssignmentEvent

CREW = "/status/crew/"
TOKEN = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


class UntilTests(SimpleTestCase):
    def test_the_next_time_the_station_clock_reads_it(self):
        zone = ZoneInfo("America/Chicago")
        now = datetime(2026, 11, 18, 23, 30, tzinfo=UTC)  # 17:30 in Chicago
        self.assertEqual(
            until_instant("18:00", zone, now), datetime(2026, 11, 18, 18, 0, tzinfo=zone)
        )
        # An earlier clock time is tomorrow morning: an overnight shift.
        self.assertEqual(
            until_instant("06:00", zone, now), datetime(2026, 11, 19, 6, 0, tzinfo=zone)
        )
        self.assertEqual(
            until_instant("17:30", zone, now), datetime(2026, 11, 19, 17, 30, tzinfo=zone)
        )
        for bad in ("", "25:00", "6pm", None, "18:00:30"):
            self.assertIsNone(until_instant(bad, zone, now))
        self.assertEqual(when(until_instant("06:00", zone, now), zone, now), "Thu 06:00")
        self.assertEqual(when(until_instant("18:00", zone, now), zone, now), "18:00")


class CrewScreenCase(CrewUiCase):
    def setUp(self):
        super().setUp()
        self.clock.set(self.at(hours=7))
        self.a, self.b = self.vehicles
        self.partner_client, self.partner = self.harness.sign_in("crew-partner")
        self.crew_user = self.user

    def form(self, client, page, **fields):
        token = TOKEN.search(client.get(page).content.decode()).group(1)
        import uuid

        fields.setdefault("operation_id", str(uuid.uuid4()))
        return client.post(CREW, {"csrfmiddlewaretoken": token, **fields})

    def page(self, vehicle):
        return f"/status/vehicles/{vehicle.pk}/"

    def assign(self, client, vehicle, until="19:00", **fields):
        return self.form(
            client,
            self.page(vehicle),
            stage="assign",
            vehicle_id=str(vehicle.pk),
            until=until,
            **fields,
        )


class VehicleCrewPageTests(CrewScreenCase):
    def test_putting_yourself_on_a_truck_shows_on_the_page_and_the_list(self):
        page = self.client.get(self.page(self.a))
        self.assertContains(page, "Nobody is assigned to this vehicle right now.")
        self.assertContains(page, "Put me on this truck")
        done = self.assign(self.client, self.a)
        self.assertEqual(done["Location"], f"{self.page(self.a)}?crew=assigned#crew")
        page = self.client.get(f"{self.page(self.a)}?crew=assigned")
        # One status line, announced once (PR #218 review).
        self.assertContains(
            page, '<span class="save-state-text">You&#x27;re on this vehicle.</span>', html=True
        )
        self.assertContains(page, 'data-save-state="saved"', count=1)
        self.assertContains(page, '<span class="crew-name">You</span>', html=True)
        self.assertContains(page, "until 19:00")
        self.assertContains(self.client.get("/status/"), "Crew: You")
        self.assertContains(
            self.partner_client.get("/status/"), f"Crew: {self.crew_user.display_name}"
        )

    def test_a_partner_moves_someone_over_and_is_shown_where_they_are(self):
        self.assign(self.client, self.b)  # the wrong truck
        self.assign(self.partner_client, self.a)
        page = self.partner_client.get(self.page(self.a))
        self.assertContains(
            page,
            f"{self.crew_user.display_name} (on {self.b.display_name} until 19:00, moves them)",
        )
        moved = self.assign(self.partner_client, self.a, person_id=str(self.crew_user.pk))
        self.assertIn("crew=moved", moved["Location"])
        here = current_assignment(str(self.org.pk), str(self.crew_user.pk))
        self.assertEqual(str(here.vehicle_id), str(self.a.pk))

    def test_controls_follow_who_may_act_and_the_server_still_decides(self):
        self.assign(self.client, self.a)
        page = self.partner_client.get(self.page(self.a))
        self.assertNotContains(page, "Add someone")
        self.assertNotContains(page, f"End {self.crew_user.display_name}")
        refused = self.form(
            self.partner_client,
            self.page(self.a),
            stage="end",
            vehicle_id=str(self.a.pk),
            person_id=str(self.crew_user.pk),
        )
        self.assertEqual(refused.status_code, 403)
        self.assertIsNotNone(current_assignment(str(self.org.pk), str(self.crew_user.pk)))

    def test_bad_times_are_explained_and_change_nothing(self):
        for until, text in (("6pm", "Enter a time such as 18:00."),):
            done = self.assign(self.client, self.a, until=until)
            self.assertIn("crew_error=MALFORMED_REQUEST", done["Location"])
            self.assertContains(self.client.get(done["Location"].split("#")[0]), text)
        self.assertFalse(VehicleAssignmentEvent.objects.exists())

    def test_extend_and_end_from_the_vehicle_page(self):
        self.assign(self.client, self.a)
        self.form(
            self.client, self.page(self.a), stage="extend", vehicle_id=str(self.a.pk), until="21:00"
        )
        self.assertContains(self.client.get(self.page(self.a)), "until 21:00")
        self.form(self.client, self.page(self.a), stage="end", vehicle_id=str(self.a.pk))
        self.assertEqual(current_crew(str(self.org.pk)), [])

    def test_supervisors_see_history_and_crew_do_not(self):
        self.assign(self.client, self.a)
        self.assertNotContains(self.client.get(self.page(self.a)), "Crew history")
        supervisor, user = self.harness.sign_in("crew-supervisor")
        self.grant(user, "SUPERVISOR")
        page = supervisor.get(self.page(self.a))
        self.assertContains(page, "Crew history")
        self.assertContains(
            page, f"{self.crew_user.display_name} on {self.a.display_name} until 19:00"
        )


class HomeTruckTests(CrewScreenCase):
    def test_home_asks_which_truck_then_shows_it_first_and_offers_extend_near_the_end(self):
        home = self.client.get("/")
        self.assertContains(home, "Which truck are you on?")
        done = self.form(
            self.client, "/", stage="assign", vehicle_id=str(self.b.pk), until="08:00", next="home"
        )
        self.assertEqual(done["Location"], "/?crew=assigned")
        home = self.client.get("/?crew=assigned")
        self.assertContains(
            home, f'You\'re on <a href="{self.page(self.b)}">{self.b.display_name}</a> until 08:00.'
        )
        self.assertNotContains(home, "Extend until")  # more than 30 minutes away
        self.clock.set(self.at(hours=7, minutes=40))
        home = self.client.get("/")
        self.assertContains(home, f"Still on {self.b.display_name}? Extend until")
        body = home.content.decode()
        due = body[body.index('id="home-due"') :]
        if self.a.display_name in due and self.b.display_name in due:
            self.assertLess(due.index(self.b.display_name), due.index(self.a.display_name))
        ended = self.form(self.client, "/", stage="end", vehicle_id=str(self.b.pk), next="home")
        self.assertEqual(ended["Location"], "/?crew=ended")
        self.assertContains(self.client.get("/"), "Which truck are you on?")


class LimitPageTests(CrewScreenCase):
    def test_only_administrators_set_the_limit(self):
        self.assertEqual(self.client.get("/administration/crew-assignments/").status_code, 403)
        admin, user = self.harness.sign_in("crew-admin")
        self.grant(user, "ADMINISTRATOR")
        page = admin.get("/administration/crew-assignments/")
        self.assertContains(page, 'value="24"')
        self.assertContains(admin.get("/administration/"), "Crew assignments")
        token = TOKEN.search(page.content.decode()).group(1)
        import uuid

        saved = admin.post(
            "/administration/crew-assignments/",
            {"csrfmiddlewaretoken": token, "operation_id": str(uuid.uuid4()), "max_hours": "48"},
        )
        self.assertEqual(saved["Location"], "/administration/crew-assignments/?saved=1")
        self.assertContains(admin.get(saved["Location"]), 'value="48"')
        bad = admin.post(
            "/administration/crew-assignments/",
            {"csrfmiddlewaretoken": token, "operation_id": str(uuid.uuid4()), "max_hours": "500"},
        )
        self.assertContains(bad, "Enter a whole number of hours from 1 to 168.", status_code=400)
