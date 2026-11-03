import json
import multiprocessing
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from django.test.runner import ParallelTestSuite

from cya.testing import runner
from cya.testing.network import NetworkAccessBlocked, NetworkGuard, guard_active

# 192.0.2.0/24 is TEST-NET-1 (RFC 5737): never routed, safe even if a guard failed.
UNROUTABLE = ("192.0.2.1", 9)


class ActiveGuardTests(SimpleTestCase):
    """The OfflineTestRunner installs the guard for the whole run."""

    def test_remote_connect_is_blocked(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with self.assertRaises(NetworkAccessBlocked):
                sock.connect(UNROUTABLE)
            with self.assertRaises(NetworkAccessBlocked):
                sock.connect_ex(UNROUTABLE)

    def test_remote_udp_send_is_blocked(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            with self.assertRaises(NetworkAccessBlocked):
                sock.sendto(b"x", UNROUTABLE)

    def test_name_lookup_is_blocked(self):
        with self.assertRaises(NetworkAccessBlocked):
            socket.getaddrinfo("example.invalid", 443)
        for lookup in (socket.gethostbyname, socket.gethostbyname_ex):
            with self.subTest(lookup=lookup.__name__):
                with self.assertRaises(NetworkAccessBlocked):
                    lookup("example.invalid")
        with self.assertRaises(NetworkAccessBlocked):
            socket.create_connection(("example.invalid", 443), timeout=1)

    def test_reverse_lookup_is_blocked(self):
        with self.assertRaises(NetworkAccessBlocked):
            socket.gethostbyaddr(UNROUTABLE[0])
        with self.assertRaises(NetworkAccessBlocked):
            socket.getnameinfo(UNROUTABLE, 0)
        with self.assertRaises(NetworkAccessBlocked):
            socket.getfqdn("example.invalid")  # uses gethostbyaddr internally

    def test_blocked_error_is_not_an_oserror(self):
        # Libraries retry or swallow OSError; a blocked call must stay loud.
        self.assertFalse(issubclass(NetworkAccessBlocked, OSError))

    def test_loopback_is_allowed(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=5):
                pass
        self.assertTrue(socket.getaddrinfo("localhost", 80))
        self.assertTrue(socket.gethostbyname("localhost"))


class GuardLifecycleTests(SimpleTestCase):
    def test_install_twice_is_an_error_and_uninstall_restores(self):
        names = ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo")
        before = [socket.socket.connect, socket.socket.sendto] + [getattr(socket, n) for n in names]
        guard = NetworkGuard()
        guard.install()
        try:
            self.assertTrue(guard.installed)
            with self.assertRaises(RuntimeError):
                guard.install()
        finally:
            guard.uninstall()
        self.assertFalse(guard.installed)
        after = [socket.socket.connect, socket.socket.sendto] + [getattr(socket, n) for n in names]
        self.assertEqual([a is b for a, b in zip(before, after, strict=True)], [True] * len(before))
        guard.uninstall()  # a second uninstall is a no-op


class WorkerGuardTests(SimpleTestCase):
    """Parallel workers install the guard whatever the start method (issue #53)."""

    def test_the_runner_uses_the_guarded_parallel_suite(self):
        self.assertIs(runner.OfflineTestRunner.parallel_test_suite, runner.OfflineParallelTestSuite)
        self.assertIs(runner.OfflineParallelTestSuite.init_worker, runner._init_guarded_worker)
        self.assertTrue(guard_active())  # this process runs under the guard too

    def test_the_worker_installs_the_guard_before_django_sets_it_up(self):
        calls = []
        with (
            mock.patch.object(runner, "guard_active", side_effect=[False, True]),
            mock.patch.object(runner, "NetworkGuard") as guard,
            mock.patch.object(ParallelTestSuite, "init_worker") as django_init,
        ):
            guard.return_value.install.side_effect = lambda: calls.append("guard")
            django_init.side_effect = lambda *a, **k: calls.append(("django", a, k))
            runner._init_guarded_worker("counter", "settings", debug_mode=True)
        self.assertEqual(
            calls, ["guard", ("django", ("counter", "settings"), {"debug_mode": True})]
        )

    def test_an_already_guarded_worker_keeps_its_guard(self):
        with (
            mock.patch.object(runner, "NetworkGuard") as guard,
            mock.patch.object(ParallelTestSuite, "init_worker") as django_init,
        ):
            runner._init_guarded_worker("counter")
        guard.assert_not_called()
        django_init.assert_called_once_with("counter")

    def test_a_worker_that_cannot_install_the_guard_fails_before_any_test(self):
        with (
            mock.patch.object(runner, "guard_active", return_value=False),
            mock.patch.object(runner, "NetworkGuard"),
            mock.patch.object(ParallelTestSuite, "init_worker") as django_init,
            self.assertRaisesRegex(RuntimeError, "not active in this worker"),
        ):
            runner._init_guarded_worker("counter")
        django_init.assert_not_called()

    def test_workers_are_guarded_under_every_start_method(self):
        # A separate interpreter: parallel test workers are daemons and cannot start their own.
        base = Path(settings.BASE_DIR)
        methods = [
            m
            for m in ("fork", "forkserver", "spawn")
            if m in multiprocessing.get_all_start_methods()
        ]
        self.assertIn("spawn", methods)
        for method in methods:
            with self.subTest(method=method):
                done = subprocess.run(
                    [sys.executable, "-m", "cya.tests.network_guard_probe", method],
                    cwd=base,
                    env={**os.environ, "PYTHONPATH": str(base)},
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                answer = json.loads(done.stdout.strip().splitlines()[-1])
                self.assertEqual(
                    answer["guarded"], {"active": True, "lookup": "blocked", "connect": "blocked"}
                )
                # Without the installer only a forked worker inherits the guard: the defect.
                self.assertEqual(answer["plain"]["active"], method == "fork")
