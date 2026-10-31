import socket

from django.test import SimpleTestCase

from cya.testing.network import NetworkAccessBlocked, NetworkGuard

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
