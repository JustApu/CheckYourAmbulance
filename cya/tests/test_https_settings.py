"""HTTPS behind the TLS-terminating proxy (SPEC DEP-10; issue 25).

Parsing is tested by executing the real settings file under a patched environment; request
behavior through SecurityMiddleware and CSRF with the signed-in HTTP harness. No network.
"""

import json
import os
import runpy
from pathlib import Path
from unittest import mock
from uuid import uuid4

from django.conf import settings
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, override_settings

from cya.checklists.tests.support import ChecklistCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

SETTINGS_FILE = Path(settings.BASE_DIR) / "cya" / "settings.py"
PROXY = {"SECURE_PROXY_SSL_HEADER": ("HTTP_X_FORWARDED_PROTO", "https")}
DEPLOYED = {**PROXY, "SECURE_SSL_REDIRECT": True, "SECURE_HSTS_SECONDS": 300}


def load(**values):
    """The settings module's globals with CYA_<name> set (or removed when None)."""
    names = ("TRUST_PROXY_SSL_HEADER", "SSL_REDIRECT", "HSTS_SECONDS")
    environment = {k: v for k, v in os.environ.items() if k not in {f"CYA_{n}" for n in names}}
    environment.update({f"CYA_{k}": v for k, v in values.items() if v is not None})
    with mock.patch.dict(os.environ, environment, clear=True):
        return runpy.run_path(str(SETTINGS_FILE))


class ParsingTests(SimpleTestCase):
    def test_everything_is_off_by_default(self):
        loaded = load()
        self.assertIsNone(loaded["SECURE_PROXY_SSL_HEADER"])
        self.assertIs(loaded["SECURE_SSL_REDIRECT"], False)
        self.assertEqual(loaded["SECURE_HSTS_SECONDS"], 0)

    def test_the_documented_deployment_configuration(self):
        loaded = load(TRUST_PROXY_SSL_HEADER="1", SSL_REDIRECT="1", HSTS_SECONDS="300")
        self.assertEqual(loaded["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertIs(loaded["SECURE_SSL_REDIRECT"], True)
        self.assertEqual(loaded["SECURE_HSTS_SECONDS"], 300)
        self.assertIs(loaded["SECURE_HSTS_INCLUDE_SUBDOMAINS"], False)
        self.assertIs(loaded["SECURE_HSTS_PRELOAD"], False)

    def test_malformed_or_unsafe_configuration_stops_startup(self):
        for values in (
            {"SSL_REDIRECT": "1"},  # a redirect without proxy trust would loop
            {"HSTS_SECONDS": "300"},  # HSTS without proxy trust would never be sent
            {"TRUST_PROXY_SSL_HEADER": "yes"},
            {"SSL_REDIRECT": "2", "TRUST_PROXY_SSL_HEADER": "1"},
            {"HSTS_SECONDS": "-1", "TRUST_PROXY_SSL_HEADER": "1"},
            {"HSTS_SECONDS": "63072001", "TRUST_PROXY_SSL_HEADER": "1"},
            {"HSTS_SECONDS": "5m", "TRUST_PROXY_SSL_HEADER": "1"},
        ):
            with self.subTest(values=values), self.assertRaises(ImproperlyConfigured):
                load(**values)


class DeployCheckTests(SimpleTestCase):
    def ids(self):
        return {m.id for m in run_checks(include_deployment_checks=True)}

    def test_deploy_check_has_no_https_warnings_under_the_deployment_settings(self):
        strong = "synthetic-" + "k3y-material-" * 6
        with override_settings(DEBUG=False, SECRET_KEY=strong):
            self.assertTrue({"security.W004", "security.W008"} <= self.ids())  # the baseline gap
            with override_settings(**DEPLOYED):
                found = self.ids()
        self.assertFalse({"security.W004", "security.W008"} & found, found)


@override_settings(**real_collaborator_settings())
class RequestTests(ChecklistCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client, self.user = self.harness.sign_in("https-crew")
        self.client.get("/")
        self.token = self.client.cookies["csrftoken"].value

    def fresh(self):
        """A client built under the current settings: SecurityMiddleware reads them once, when
        the client first builds its middleware. It keeps the signed-in session cookies."""
        client = Client(enforce_csrf_checks=True)
        client.cookies = self.client.cookies
        return client

    def save_preference(self, client, **headers):
        return client.post(
            "/api/v1/me/view-preferences",
            data=json.dumps(
                {
                    "operation_id": str(uuid4()),
                    "preferred_view": "NORMAL",
                    "needs_attention_only": False,
                }
            ),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
            HTTP_ORIGIN="https://testserver",
            # The proxy forwards the client's Host; without it Django would add the
            # upstream port (testserver:80), which no HTTPS origin matches.
            HTTP_HOST="testserver",
            **headers,
        )

    def test_a_proxied_https_request_is_secure_and_gets_hsts(self):
        with override_settings(**DEPLOYED):
            response = self.fresh().get("/", HTTP_X_FORWARDED_PROTO="https")
        self.assertNotEqual(response.status_code, 301)
        self.assertEqual(response["Strict-Transport-Security"], "max-age=300")

    def test_plain_http_is_redirected_to_https(self):
        with override_settings(**DEPLOYED):
            response = self.fresh().get("/fleet/?x=1", HTTP_X_FORWARDED_PROTO="http")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://testserver/fleet/?x=1")
        self.assertNotIn("Strict-Transport-Security", response)

    def test_a_spoofed_header_means_nothing_without_proxy_trust(self):
        with override_settings(SECURE_PROXY_SSL_HEADER=None, SECURE_HSTS_SECONDS=300):
            client = self.fresh()
            response = client.get("/", HTTP_X_FORWARDED_PROTO="https")
            self.assertNotIn("Strict-Transport-Security", response)
            refused = self.save_preference(client, HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(refused.status_code, 403)  # the HTTPS origin does not match plain HTTP

    def test_an_https_origin_post_succeeds_behind_the_proxy(self):
        with override_settings(**DEPLOYED):
            client = self.fresh()
            saved = self.save_preference(client, HTTP_X_FORWARDED_PROTO="https")
            missing = client.post(
                "/api/v1/me/view-preferences",
                data="{}",
                content_type="application/json",
                HTTP_ORIGIN="https://testserver",
                HTTP_X_FORWARDED_PROTO="https",
            )
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(missing.status_code, 403)  # CSRF is still required
