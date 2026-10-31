"""Block Python-level network access during tests (AGENTS.md rule 6).

While installed, sockets may connect only to Unix-domain or loopback
addresses, and forward and reverse name lookups are refused except for
loopback names and addresses. Anything else raises NetworkAccessBlocked at
once, before any packet is sent.

Limit: this patches Python's socket module. C extensions that open their own
sockets are not intercepted; the MariaDB driver (mysqlclient) is one, which is
what lets tests reach the test database. Tests must still use stubs for
external services; this guard catches mistakes, it is not a sandbox.
"""

import ipaddress
import socket

LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})
SOCKET_METHODS = ("connect", "connect_ex", "sendto")
# Every socket-module function that resolves names or addresses (forward and
# reverse). socket.getfqdn and socket.create_connection call these.
RESOLVER_FUNCTIONS = (
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
)


class NetworkAccessBlocked(RuntimeError):
    """A test tried to reach the network. Not an OSError, so it is not retried."""


def _is_loopback_host(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _allowed(sock, address):
    if sock.family == getattr(socket, "AF_UNIX", None):
        return True
    host = address[0] if isinstance(address, tuple) and address else None
    return host is not None and _is_loopback_host(host)


class NetworkGuard:
    """Install/uninstall the patches; installing twice is an error."""

    def __init__(self):
        self._originals = None

    @property
    def installed(self):
        return self._originals is not None

    def install(self):
        if self.installed:
            raise RuntimeError("network guard is already installed")
        originals = {name: getattr(socket.socket, name) for name in SOCKET_METHODS}
        originals.update({name: getattr(socket, name) for name in RESOLVER_FUNCTIONS})

        def guard_method(name):
            def guarded(sock, *args):
                # connect(address), connect_ex(address), sendto(data[, flags], address)
                address = args[-1] if args else None
                if not _allowed(sock, address):
                    raise NetworkAccessBlocked(f"test tried {name} to {address!r}")
                return originals[name](sock, *args)

            return guarded

        def guard_resolver(name):
            def guarded(target, *args, **kwargs):
                # getnameinfo takes a socket address; the others take a host name/IP.
                host = target[0] if name == "getnameinfo" and isinstance(target, tuple) else target
                if not _is_loopback_host(host):
                    raise NetworkAccessBlocked(f"test tried {name} for {host!r}")
                return originals[name](target, *args, **kwargs)

            return guarded

        for name in SOCKET_METHODS:
            setattr(socket.socket, name, guard_method(name))
        for name in RESOLVER_FUNCTIONS:
            setattr(socket, name, guard_resolver(name))
        self._originals = originals

    def uninstall(self):
        if not self.installed:
            return
        for name in SOCKET_METHODS:
            setattr(socket.socket, name, self._originals[name])
        for name in RESOLVER_FUNCTIONS:
            setattr(socket, name, self._originals[name])
        self._originals = None
