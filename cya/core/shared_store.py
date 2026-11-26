"""The shared store for several application processes (SPEC DEP-2, amendment 8.2.0; issue #278).

With one process (the default) live updates use Channels' in-memory layer and the sign-in limiter
counts in process memory. With several, both go through one Redis-compatible server on the same
host, named by CYA_SHARED_STORE_URL:

- unix:///path/to/socket, or redis://127.0.0.1:port/db (loopback only: DEP-2 keeps the server on
  the host and reachable only by the application, DEP-10);
- CYA_SHARED_STORE_PASSWORD is required: the server must not accept unauthenticated clients.

The server holds no record of work: only in-flight live-update messages and sign-in counters,
both with expiry. Nothing here connects at import or startup; when the server is unavailable,
live updates stop (screens say so and reconcile when it returns) and sign-in counting falls back
to each process (cya.accounts.ratelimit).
"""

import ipaddress
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

IN_MEMORY = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
PREFIX = "cya"
LOOPBACK_NAMES = {"localhost"}


def validated(url, password):
    """(url, password) when a shared store is configured and acceptable, else None.

    Refuses, at startup, a store DEP-2 does not allow: another scheme, a non-loopback host,
    a URL carrying its own credentials (they belong in the root-only password variable), or no
    password.
    """
    if not url:
        if password:
            raise ImproperlyConfigured(
                "CYA_SHARED_STORE_PASSWORD is set without CYA_SHARED_STORE_URL."
            )
        return None
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise ImproperlyConfigured(
            "CYA_SHARED_STORE_URL must not carry credentials; use CYA_SHARED_STORE_PASSWORD."
        )
    if parts.scheme == "unix":
        if not parts.path.startswith("/"):
            raise ImproperlyConfigured(
                "CYA_SHARED_STORE_URL unix:// needs an absolute socket path."
            )
    elif parts.scheme == "redis":
        host = parts.hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host in LOOPBACK_NAMES
        if not loopback:
            raise ImproperlyConfigured(
                "CYA_SHARED_STORE_URL must name a server on this host (loopback or a Unix socket)."
            )
    else:
        raise ImproperlyConfigured("CYA_SHARED_STORE_URL must start with unix:// or redis://.")
    if not password:
        raise ImproperlyConfigured("CYA_SHARED_STORE_URL needs CYA_SHARED_STORE_PASSWORD.")
    return url, password


def channel_layers(url, password):
    """CHANNEL_LAYERS for the configured store: the shared Redis layer, or the in-memory one."""
    store = validated(url, password)
    if store is None:
        return IN_MEMORY
    url, password = store
    return {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [{"address": url, "password": password}],
                "prefix": PREFIX,
                # A hint undelivered after a minute is stale; pages reconcile on reconnect.
                "expiry": 60,
            },
        }
    }
