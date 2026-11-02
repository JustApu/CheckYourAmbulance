"""Application-level sign-in rate limiting (SPEC 16.2, DEP-10; issue 26). No network."""

import os
import runpy
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, RequestFactory, SimpleTestCase, override_settings

from cya.accounts.ratelimit import LIMITER, FixedWindowLimiter, client_address
from cya.checklists.tests.support import ChecklistCase
from cya.core.models import AuditEvent
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

SETTINGS_FILE = Path(settings.BASE_DIR) / "cya" / "settings.py"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LimiterTests(SimpleTestCase):
    def setUp(self):
        self.clock = Clock()
        self.limiter = FixedWindowLimiter(clock=self.clock)

    def hits(self, key, n, limit=3, window=60):
        return [self.limiter.hit(key, limit, window)[0] for _ in range(n)]

    def test_under_at_and_over_the_limit(self):
        self.assertEqual(self.hits("a", 4), [True, True, True, False])
        allowed, retry_after = self.limiter.hit("a", 3, 60)
        self.assertFalse(allowed)
        self.assertEqual(retry_after, 61)

    def test_the_window_resets(self):
        self.hits("a", 4)
        self.clock.now += 59
        self.assertEqual(self.hits("a", 1), [False])
        self.clock.now += 1
        self.assertEqual(self.hits("a", 3), [True, True, True])

    def test_keys_are_independent(self):
        self.hits("a", 4)
        self.assertEqual(self.hits("b", 3), [True, True, True])

    def test_memory_stays_bounded(self):
        limiter = FixedWindowLimiter(clock=self.clock, max_keys=2)
        for key in ("a", "b", "c"):
            limiter.hit(key, 3, 60)
        self.assertEqual(len(limiter._counts), 2)
        self.assertNotIn("a", limiter._counts)  # the oldest entry goes first
        self.clock.now += 61
        limiter.hit("d", 3, 60)
        limiter.hit("e", 3, 60)
        self.assertEqual(set(limiter._counts), {"d", "e"})  # expired windows dropped


class AddressTests(SimpleTestCase):
    def request(self, **meta):
        return RequestFactory().get("/auth/login/", REMOTE_ADDR="127.0.0.1", **meta)

    def test_without_proxy_trust_no_client_header_is_read(self):
        with override_settings(CYA_TRUST_PROXY_SSL_HEADER=False):
            self.assertEqual(
                client_address(self.request(HTTP_X_REAL_IP="203.0.113.9")), "127.0.0.1"
            )

    def test_behind_the_trusted_proxy_the_overwritten_header_is_the_key(self):
        with override_settings(CYA_TRUST_PROXY_SSL_HEADER=True):
            self.assertEqual(
                client_address(self.request(HTTP_X_REAL_IP="203.0.113.9")), "203.0.113.9"
            )
            self.assertEqual(
                client_address(self.request(HTTP_X_REAL_IP="2001:db8::1")), "2001:db8::1"
            )
            for bad in ("", "203.0.113.9, 198.51.100.1", "not-an-address"):
                with self.subTest(bad=bad):
                    # A misconfigured proxy errs towards one shared key, never towards trust.
                    self.assertEqual(client_address(self.request(HTTP_X_REAL_IP=bad)), "127.0.0.1")


class SettingsTests(SimpleTestCase):
    def load(self, **values):
        names = {"CYA_SIGNIN_RATE_LIMIT", "CYA_SIGNIN_RATE_WINDOW_SECONDS"}
        environment = {k: v for k, v in os.environ.items() if k not in names}
        environment.update({f"CYA_{k}": v for k, v in values.items()})
        with mock.patch.dict(os.environ, environment, clear=True):
            return runpy.run_path(str(SETTINGS_FILE))

    def test_defaults(self):
        loaded = self.load()
        self.assertEqual(
            (loaded["CYA_SIGNIN_RATE_LIMIT"], loaded["CYA_SIGNIN_RATE_WINDOW_SECONDS"]), (30, 60)
        )

    def test_malformed_configuration_stops_startup(self):
        for values in (
            {"SIGNIN_RATE_LIMIT": "0"},
            {"SIGNIN_RATE_LIMIT": "1001"},
            {"SIGNIN_RATE_LIMIT": "ten"},
            {"SIGNIN_RATE_WINDOW_SECONDS": "0"},
            {"SIGNIN_RATE_WINDOW_SECONDS": "3601"},
            {"SIGNIN_RATE_WINDOW_SECONDS": "-5"},
        ):
            with self.subTest(values=values), self.assertRaises(ImproperlyConfigured):
                self.load(**values)


@override_settings(**real_collaborator_settings())
class HttpTests(ChecklistCase):
    def make_org(self):
        self.harness = RoleHttpHarness()  # activates the stub provider, which resets LIMITER
        return self.harness.org

    def test_a_sign_in_under_the_limit_succeeds(self):
        with override_settings(CYA_SIGNIN_RATE_LIMIT=2):
            client, user = self.harness.sign_in("limited-crew")  # one login, one callback
        self.assertEqual(client.get("/auth/status/").status_code, 200)
        self.assertTrue(user.pk)

    def test_over_the_limit_is_refused_before_the_view_runs(self):
        client = Client()
        with override_settings(CYA_SIGNIN_RATE_LIMIT=2, CYA_SIGNIN_RATE_WINDOW_SECONDS=60):
            first, second = client.get("/auth/login/"), client.get("/auth/login/")
            audits = AuditEvent.objects.count()
            third = client.get("/auth/login/")
            refused_audits = AuditEvent.objects.count()
            # The callback has its own counter: it is reached (and refuses the bad state itself).
            callback = client.get("/auth/callback/", {"state": "x", "code": "y"})
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(third.status_code, 429)
        self.assertIn("Too many sign-in attempts", third.content.decode())
        self.assertGreaterEqual(int(third["Retry-After"]), 1)
        self.assertNotIn("cya_signin", third.cookies)  # no sign-in transaction started
        self.assertNotIn("cya_session", third.cookies)
        self.assertEqual(refused_audits, audits)  # the refusal writes no audit record
        self.assertNotEqual(callback.status_code, 429)

    def test_a_spoofed_address_header_does_not_escape_the_limit(self):
        client = Client()
        with override_settings(CYA_SIGNIN_RATE_LIMIT=1, CYA_TRUST_PROXY_SSL_HEADER=False):
            client.get("/auth/login/", HTTP_X_REAL_IP="203.0.113.1")
            refused = client.get("/auth/login/", HTTP_X_REAL_IP="203.0.113.2")
        self.assertEqual(refused.status_code, 429)

    def test_behind_the_trusted_proxy_each_client_has_its_own_counter(self):
        client = Client()
        proxy = {
            "CYA_SIGNIN_RATE_LIMIT": 1,
            "CYA_TRUST_PROXY_SSL_HEADER": True,
            "SECURE_PROXY_SSL_HEADER": ("HTTP_X_FORWARDED_PROTO", "https"),
        }
        with override_settings(**proxy):
            first = client.get("/auth/login/", HTTP_X_REAL_IP="203.0.113.1")
            other = client.get("/auth/login/", HTTP_X_REAL_IP="203.0.113.2")
            again = client.get("/auth/login/", HTTP_X_REAL_IP="203.0.113.1")
        self.assertEqual((first.status_code, other.status_code, again.status_code), (302, 302, 429))

    def test_status_and_sign_out_are_not_limited(self):
        client = Client()
        with override_settings(CYA_SIGNIN_RATE_LIMIT=1):
            codes = {client.get("/auth/status/").status_code for _ in range(3)}
        self.assertEqual(codes, {200})
        LIMITER.reset()
