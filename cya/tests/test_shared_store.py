"""Several application processes (SPEC DEP-2, amendment 8.2.0; issue #278).

No Redis server runs in CI. These tests cover the configuration rules, that the shared layer is
selected and constructed without connecting, and the shared sign-in limiter against a fake store,
including its fallback when the store fails. Cross-process delivery is verified on the
rehearsal host (docs/deployment-runbook.md).
"""

from unittest import mock

import redis
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from cya.accounts.ratelimit import FixedWindowLimiter, SharedWindowLimiter
from cya.core import shared_store


class ConfigurationTests(SimpleTestCase):
    def test_unset_is_one_process_in_memory(self):
        self.assertEqual(shared_store.channel_layers("", ""), shared_store.IN_MEMORY)

    def test_a_socket_or_loopback_with_a_password_selects_the_shared_layer(self):
        for url in (
            "unix:///run/cya-store/store.sock",
            "redis://127.0.0.1:6379/0",
            "redis://[::1]:6379",
            "redis://localhost:6379",
        ):
            with self.subTest(url=url):
                layers = shared_store.channel_layers(url, "secret")
                self.assertEqual(
                    layers["default"]["BACKEND"], "channels_redis.core.RedisChannelLayer"
                )
                self.assertEqual(
                    layers["default"]["CONFIG"]["hosts"], [{"address": url, "password": "secret"}]
                )

    def test_what_dep2_does_not_allow_is_refused_at_startup(self):
        refused = {
            "another host": ("redis://10.0.0.5:6379/0", "secret"),
            "a public name": ("redis://store.example.org:6379", "secret"),
            "no password": ("unix:///run/cya-store/store.sock", ""),
            "credentials in the URL": ("redis://:secret@127.0.0.1:6379", "secret"),
            "another scheme": ("rediss://127.0.0.1:6380", "secret"),
            "a relative socket": ("unix://store.sock", "secret"),
            "a password without a store": ("", "secret"),
        }
        for case, (url, password) in refused.items():
            with self.subTest(case=case), self.assertRaises(ImproperlyConfigured):
                shared_store.channel_layers(url, password)

    def test_the_shared_layer_is_built_without_connecting(self):
        from channels.layers import channel_layers

        config = shared_store.channel_layers("redis://127.0.0.1:1/0", "secret")
        with override_settings(CHANNEL_LAYERS=config):
            channel_layers.backends.clear()
            try:
                layer = channel_layers["default"]
                self.assertEqual(type(layer).__name__, "RedisChannelLayer")
            finally:
                channel_layers.backends.clear()


class FakeStore:
    """incr/expire through a pipeline, as SharedWindowLimiter uses them."""

    def __init__(self):
        self.counts, self.expiry, self.fail = {}, {}, False

    def pipeline(self):
        store, ops = self, []

        class Pipe:
            def incr(self, name):
                ops.append(("incr", name))

            def expire(self, name, seconds):
                ops.append(("expire", name, seconds))

            def execute(self):
                if store.fail:
                    raise redis.ConnectionError("down")
                out = []
                for op in ops:
                    if op[0] == "incr":
                        store.counts[op[1]] = store.counts.get(op[1], 0) + 1
                        out.append(store.counts[op[1]])
                    else:
                        store.expiry[op[1]] = op[2]
                        out.append(True)
                return out

        return Pipe()


class SharedLimiterTests(SimpleTestCase):
    def setUp(self):
        self.store, self.now = FakeStore(), [1000.0]
        self.clock = lambda: self.now[0]

    def limiter(self):
        return SharedWindowLimiter(self.store, FixedWindowLimiter(), clock=self.clock)

    def test_two_processes_share_one_limit(self):
        first, second = self.limiter(), self.limiter()
        key = ("login", "192.0.2.7")
        self.assertTrue(first.hit(key, 3, 60)[0])
        self.assertTrue(second.hit(key, 3, 60)[0])
        self.assertTrue(first.hit(key, 3, 60)[0])
        allowed, retry_after = second.hit(key, 3, 60)
        self.assertFalse(allowed, "the fourth attempt across both processes is refused")
        self.assertEqual(retry_after, 21)  # window 960..1020, now 1000
        self.assertTrue(second.hit(("login", "192.0.2.8"), 3, 60)[0], "another address is separate")
        self.assertTrue(
            second.hit(("callback", "192.0.2.7"), 3, 60)[0], "another route is separate"
        )

    def test_keys_expire_with_their_window_and_do_not_hold_the_address(self):
        self.limiter().hit(("login", "192.0.2.7"), 3, 60)
        (name,) = self.store.counts
        self.assertNotIn("192.0.2.7", name)
        self.assertEqual(self.store.expiry[name], 61)
        self.now[0] = 1020.0  # the next window
        self.limiter().hit(("login", "192.0.2.7"), 3, 60)
        self.assertEqual(len(self.store.counts), 2)

    def test_a_store_failure_falls_back_to_this_process(self):
        limiter = self.limiter()
        self.store.fail = True
        with self.assertLogs("cya.accounts.ratelimit", "ERROR") as logs:
            results = [limiter.hit(("login", "192.0.2.7"), 2, 60)[0] for _ in range(3)]
        self.assertEqual(results, [True, True, False], "the process's own limit still applies")
        self.assertNotIn("192.0.2.7", "\n".join(logs.output))

    @override_settings(CYA_SHARED_STORE_URL="", CYA_SHARED_STORE_PASSWORD="")
    def test_one_process_counts_in_memory(self):
        from cya.accounts.ratelimit import make_limiter

        self.assertIsInstance(make_limiter(), FixedWindowLimiter)

    @override_settings(
        CYA_SHARED_STORE_URL="unix:///run/cya-store/store.sock", CYA_SHARED_STORE_PASSWORD="secret"
    )
    def test_a_configured_store_shares_the_counters_without_connecting(self):
        from cya.accounts.ratelimit import make_limiter

        with mock.patch.object(redis.Redis, "execute_command") as execute:
            limiter = make_limiter()
        self.assertIsInstance(limiter, SharedWindowLimiter)
        execute.assert_not_called()
