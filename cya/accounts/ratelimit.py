"""Application-level rate limiting of the sign-in routes (SPEC 16.2, DEP-10; issue 26).

The proxy limits sign-in too; this is the second, application layer DEP-10 requires.

- **Routes:** /auth/login/ and /auth/callback/, the two that drive the sign-in flow, each with
  its own counter. /auth/status/ only reads the session, and /auth/logout/ only ends one;
  limiting sign-out could stop someone ending their session, so neither is limited.
- **Key:** the client address. Without proxy trust (CYA_TRUST_PROXY_SSL_HEADER, issue 25) it is
  the connection's REMOTE_ADDR and no client-supplied header is read. With proxy trust it is the
  X-Real-IP header, which the proxy must overwrite on every request (docs/deployment-runbook.md);
  a missing or malformed value falls back to REMOTE_ADDR, which behind the proxy is shared by
  everyone, so a misconfigured proxy errs towards limiting, never towards trusting.
- **Window:** a fixed window of CYA_SIGNIN_RATE_WINDOW_SECONDS allowing CYA_SIGNIN_RATE_LIMIT
  requests per route and address. The defaults allow a whole crew signing in from one station
  address in the same minute while still bounding scripted attempts.
- **Over the limit:** 429 with Retry-After and a fixed page, decided before the view runs, so no
  sign-in cookie, session, audit record or call to Google is made.
- **Storage:** with one process, process memory; counters reset on restart. At most MAX_KEYS
  addresses are tracked; past that, expired windows are dropped first and then the oldest
  entries, so memory stays bounded under many addresses. With several processes (DEP-2,
  amendment 8.2.0) the counters live in the shared store (CYA_SHARED_STORE_URL), so the limit
  applies across processes: one key per route, address and window, which expires with the
  window. If the store fails, each process counts on its own (the in-memory limiter) and the
  proxy's limit still applies; the failure is logged without the address.
"""

import hashlib
import hmac
import ipaddress
import logging
import threading
import time
from collections import OrderedDict
from functools import wraps

from django.conf import settings
from django.utils.html import format_html

logger = logging.getLogger(__name__)
MAX_KEYS = 10000
REFUSED_HTML = format_html(
    "<p>{}</p>", "Too many sign-in attempts from this address. Wait a minute and try again."
)


class FixedWindowLimiter:
    """Thread-safe fixed-window counters per key, bounded to max_keys entries."""

    def __init__(self, clock=time.monotonic, max_keys=MAX_KEYS):
        self._clock = clock
        self._max_keys = max_keys
        self._counts = OrderedDict()  # key -> (window start, count), oldest first
        self._lock = threading.Lock()

    def hit(self, key, limit, window):
        """(allowed, seconds until the window resets) after counting one request for key."""
        now = self._clock()
        with self._lock:
            start, count = self._counts.pop(key, (now, 0))
            if now - start >= window:
                start, count = now, 0
            count += 1
            self._counts[key] = (start, count)
            if len(self._counts) > self._max_keys:
                self._trim(now, window)
            return count <= limit, max(1, int(window - (now - start)) + 1)

    def _trim(self, now, window):
        for key in [k for k, (start, _c) in self._counts.items() if now - start >= window]:
            del self._counts[key]
        while len(self._counts) > self._max_keys:
            self._counts.popitem(last=False)

    def reset(self):
        with self._lock:
            self._counts.clear()


class SharedWindowLimiter:
    """Fixed-window counters in the shared store, so several processes share one limit.

    Windows are aligned to the epoch (one key per route, address and window start), so every
    process counts into the same key without coordination. Unlike the in-memory limiter, whose
    window starts at an address's first request, a window here starts on the clock: an address
    that spends its limit just before a boundary can spend it again just after, so up to twice
    the limit can pass in a short interval. The proxy's limit still applies (DEP-10). Key names
    are an HMAC of the route, address and window under SECRET_KEY, so addresses are not
    stored in clear. Any store error falls back to this process's own counters.
    """

    def __init__(self, client, fallback, clock=time.time):
        self._client, self._fallback, self._clock = client, fallback, clock

    def hit(self, key, limit, window):
        import redis

        now = self._clock()
        start = int(now // window) * window
        name = (
            "cya:signin:"
            + hmac.new(
                settings.SECRET_KEY.encode(), repr((key, window, start)).encode(), hashlib.sha256
            ).hexdigest()
        )
        try:
            pipe = self._client.pipeline()
            pipe.incr(name)
            pipe.expire(name, window + 1)
            count, _ok = pipe.execute()
        except redis.RedisError:
            logger.error("shared sign-in counters unavailable; counting in this process")
            return self._fallback.hit(key, limit, window)
        return count <= limit, max(1, int(start + window - now) + 1)

    def reset(self):
        self._fallback.reset()


def make_limiter():
    """The limiter for this deployment: shared with a configured store, else process memory."""
    local = FixedWindowLimiter()
    if not settings.CYA_SHARED_STORE_URL:
        return local
    import redis

    client = redis.Redis.from_url(
        settings.CYA_SHARED_STORE_URL,
        password=settings.CYA_SHARED_STORE_PASSWORD,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
    )
    return SharedWindowLimiter(client, local)


LIMITER = make_limiter()


def client_address(request):
    """The address the limit is keyed on; a client-supplied header only behind trusted proxy."""
    remote = request.META.get("REMOTE_ADDR", "")
    if settings.CYA_TRUST_PROXY_SSL_HEADER:
        forwarded = request.META.get("HTTP_X_REAL_IP", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return remote
    return remote


def limited(route):
    """Refuse a request to this sign-in route with 429 once its address is over the limit."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            allowed, retry_after = LIMITER.hit(
                (route, client_address(request)),
                settings.CYA_SIGNIN_RATE_LIMIT,
                settings.CYA_SIGNIN_RATE_WINDOW_SECONDS,
            )
            if not allowed:
                from cya.accounts.views import _page

                response = _page(request, "Too many attempts", REFUSED_HTML, status=429)
                response["Retry-After"] = str(retry_after)
                return response
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
