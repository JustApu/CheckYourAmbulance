"""Background re-reads answer 401 when signed out instead of redirecting (issue 37).

A page script re-reads these routes with fetch, which would follow a redirect to /auth/login/:
an open tab past its session would then start sign-in by itself and count against the sign-in
rate limit (issue 26). Full-page navigations keep redirecting; role refusals stay 403.
"""

import time
from unittest import mock

from django.test import Client, override_settings

from cya.accounts.ratelimit import LIMITER
from cya.inspections.tests.test_crew_ui import CrewUiCase


class BackgroundRefreshTests(CrewUiCase):
    def setUp(self):
        super().setUp()
        self.deficiency = self.failed(actor=self.user).body["deficiency_ids"][0]
        self.re_reads = (
            "/fleet/readiness/panel/",
            f"/status/vehicles/{self.vehicle.pk}/panel/",
            f"/deficiencies/{self.deficiency}/verification-items/",
        )
        self.pages = (
            "/fleet/readiness/",
            f"/status/vehicles/{self.vehicle.pk}/",
            f"/deficiencies/{self.deficiency}/verification/",
        )

    def assert_signed_out(self, response, url):
        self.assertEqual(response.status_code, 401, url)
        self.assertNotIn("Location", response, url)
        self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED", url)
        self.assertIn("no-store", response["Cache-Control"], url)

    def test_signed_out_re_reads_get_401_and_page_navigations_still_redirect(self):
        anonymous = Client()
        for url in self.re_reads:
            with self.subTest(url=url):
                self.assert_signed_out(anonymous.get(url), url)
        for url in self.pages:
            with self.subTest(url=url):
                response = anonymous.get(url)
                self.assertEqual(
                    (response.status_code, response["Location"]), (302, "/auth/login/")
                )

    def test_an_expired_session_gets_401_too(self):
        later = time.time() + 30 * 3600  # past CYA_SESSION_MAX_AGE (8 hours by default)
        with mock.patch("cya.accounts.middleware.time.time", return_value=later):
            for url in self.re_reads:
                with self.subTest(url=url):
                    self.assert_signed_out(self.client.get(url), url)

    def test_signed_in_re_reads_work_and_role_refusals_stay_403(self):
        self.assertEqual(self.client.get(self.re_reads[1]).status_code, 200)
        self.assertEqual(self.client.get(self.re_reads[2]).status_code, 200)
        self.assertEqual(self.client.get(self.re_reads[0]).status_code, 403)  # Crew: no dashboard
        supervisor, user = self.harness.sign_in("refresh-supervisor")
        self.grant(user, "SUPERVISOR")
        self.assertEqual(supervisor.get(self.re_reads[0]).status_code, 200)

    def test_a_stale_tab_does_not_count_against_the_sign_in_limit(self):
        anonymous = Client()
        LIMITER.reset()  # setUp's own sign-in already used this address's single request
        with override_settings(CYA_SIGNIN_RATE_LIMIT=1):
            for _ in range(5):  # a dashboard timer and hints firing past the session
                for url in self.re_reads:
                    anonymous.get(url)
            sign_in = anonymous.get("/auth/login/")
        self.assertEqual(sign_in.status_code, 302)  # the first real sign-in is not refused
