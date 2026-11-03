"""Where a person goes after signing in: back to the page that asked them to (issue #52).

A page that needs a session redirects to /auth/login/?next=<its path>. The sign-in view keeps a
safe path in its signed, short-lived transaction cookie, and the callback returns to it; any other
value falls back to the status page. Only a path of this application is accepted, never a URL with
a scheme or host, so sign-in cannot be used to send someone elsewhere (an open redirect). The path
is not logged: its query string may carry identifiers.
"""

from urllib.parse import urlencode, urlsplit

from django.http import HttpResponseRedirect
from django.utils.http import url_has_allowed_host_and_scheme

SIGN_IN_PATH = "/auth/login/"
MAX_RETURN_PATH = 512
# Sign-in itself, JSON endpoints and sockets are never a page to come back to.
REFUSED_PREFIXES = ("/auth/", "/api/", "/ws/")


def safe_return_path(value):
    """value if it is a same-origin page path of this application, otherwise None."""
    if not isinstance(value, str) or not value or len(value) > MAX_RETURN_PATH:
        return None
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value):
        return None
    # Django's own check for redirect targets: with no allowed host, only a relative URL passes.
    if not url_has_allowed_host_and_scheme(value, allowed_hosts=set(), require_https=True):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path.startswith(REFUSED_PREFIXES):
        return None
    return value


def sign_in_location(path):
    """The sign-in URL that returns to path afterwards, or plain sign-in if path is not safe."""
    target = safe_return_path(path)
    if target is None:
        return SIGN_IN_PATH
    return f"{SIGN_IN_PATH}?{urlencode({'next': target})}"


def sign_in_redirect(request):
    """The redirect to sign-in for a request without a session, naming the page to return to.

    Only a GET page is returned to; a form submission starts sign-in plainly.
    """
    if request.method != "GET":
        return HttpResponseRedirect(SIGN_IN_PATH)
    return HttpResponseRedirect(sign_in_location(request.get_full_path()))
