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
- **Storage:** process memory, which suits the approved single process (DEP-2). Counters reset on
  restart. At most MAX_KEYS addresses are tracked; past that, expired windows are dropped first
  and then the oldest entries, so memory stays bounded under many addresses.
"""

import ipaddress
import threading
import time
from collections import OrderedDict
from functools import wraps

from django.conf import settings
from django.utils.html import format_html

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


LIMITER = FixedWindowLimiter()


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
