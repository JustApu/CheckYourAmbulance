"""Status pages (issue 148 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/148)): authorization, role-specific forms, escaping and honest failure."""

import json
import re
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError
from django.test import Client, override_settings

from cya.accounts.models import Organization
from cya.checklists.models import Vehicle
from cya.operations.tests.test_api import StatusHttpCase


class StatusPageTests(StatusHttpCase):
    @property
    def url(self):
        return f"/status/vehicles/{self.vehicle.pk}/"

    def test_pages_require_sign_in_and_a_current_role(self):
        urls = ("/status/", self.url, self.url + "panel/")
        for url in urls:
            response = Client().get(url)
            self.assertEqual((response.status_code, response["Location"]), (302, "/auth/login/"))
            with override_settings(CYA_ROLE_LOOKUP=None):
                self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_missing_foreign_and_malformed_vehicles_are_not_disclosed(self):
        other = Organization.objects.create(name="Synthetic other", station_zone="UTC")
        foreign = Vehicle.objects.create(organization=other, display_name="Synthetic elsewhere")
        for value in (str(foreign.pk), str(uuid4()), "not-a-uuid"):
            for suffix in ("", "panel/"):
                response = self.client.get(f"/status/vehicles/{value}/{suffix}")
                self.assertEqual(response.status_code, 404, (value, suffix))

    def test_navigation_links_the_status_section_for_every_role(self):
        html = self.client.get("/status/").content.decode()
        self.assertRegex(html, r'href="/status/"[^>]*>\s*Vehicle status')
        self.assertIn('aria-current="page"', html)

    def test_crew_sees_the_request_form_and_no_set_or_decision_forms(self):
        self.request("OUT_OF_SERVICE", actor=self.crew)
        html = self.client.get(self.url).content.decode()
        self.assertIn('data-kind="request"', html)
        self.assertNotIn('data-kind="set"', html)
        self.assertNotIn('data-kind="decide"', html)
        self.assertIn("Synthetic request.", html)

    def test_supervisor_sees_set_and_decision_forms_and_no_request_form(self):
        self.request("LIMITED_SERVICE", actor=self.crew)
        client, _token = self.as_role("SUPERVISOR", "status-sup")
        html = client.get(self.url).content.decode()
        self.assertIn('data-kind="set"', html)
        self.assertEqual(html.count('data-kind="decide"'), 2)
        self.assertIn('data-decision="APPROVE"', html)
        self.assertIn('data-decision="DENY"', html)
        self.assertNotIn('data-kind="request"', html)
        self.assertEqual(
            html.count('name="restrictions"'), 2
        )  # set form + limited-service approval

    def test_page_config_names_only_same_origin_paths_for_this_vehicle(self):
        found = re.search(
            r'<script id="status-config" type="application/json">(.*?)</script>',
            self.client.get(self.url).content.decode(),
            re.S,
        )
        config = json.loads(found.group(1))
        self.assertEqual(config["vehicle_id"], str(self.vehicle.pk))
        self.assertEqual(config["socket"], f"/ws/v1/vehicles/{self.vehicle.pk}/live/")
        for key in ("panel_url", "set_url", "request_url", "decision_url_template"):
            self.assertTrue(config[key].startswith("/"), key)

    def test_free_text_is_escaped_and_out_of_service_says_exempt(self):
        self.set_status("OUT_OF_SERVICE", explanation="<script>alert(1)</script>")
        self.request("LIMITED_SERVICE", reason="<img src=x onerror=alert(2)>")
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("<strong>Exempt</strong>", html)
        self.assertNotRegex(html, r"Exempt[^<]*Passed\b(?!\.)")

    def test_panel_is_the_live_fragment_only(self):
        self.set_status("LIMITED_SERVICE", restrictions="No siren.")
        response = self.client.get(self.url + "panel/")
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<html", html)
        self.assertIn("Limited Service", html)
        self.assertIn("Restrictions: No siren.", html)

    def test_fleet_list_shows_status_and_waiting_requests(self):
        self.set_status("OUT_OF_SERVICE")
        self.request("IN_SERVICE")
        html = self.client.get("/status/").content.decode()
        self.assertIn("1 status-change request waiting", html)
        self.assertIn("1 request", html)
        self.assertIn('data-status="OUT_OF_SERVICE"', html)

    def test_a_database_failure_is_an_honest_503_page(self):
        with mock.patch(
            "cya.operations.service.pending_requests", side_effect=DatabaseError("synthetic")
        ):
            for url in ("/status/", self.url, self.url + "panel/"):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 503, url)
                self.assertIn("could not be loaded completely", response.content.decode())
