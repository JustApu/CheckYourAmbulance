"""Attach the signed-in user to each request, re-validated every time.

request.cya_user is the active User, or None. On every request the session
must name an existing, active user in an active organization, and the Google
sign-in must be younger than settings.CYA_SESSION_MAX_AGE seconds. Otherwise
the session is cleared. So a user deactivated here loses access on their next
request, and a user disabled in Google Workspace loses access once the session
ages out, because signing in again requires Google to accept them (AUTH-2,
AUTH-4). Roles are not cached in the session: authorization loads them from
the database on each command (SPEC 3.2).
"""

import time
import uuid

from cya.accounts.models import User

SESSION_USER_KEY = "cya_user_id"
SESSION_AUTH_TIME_KEY = "cya_auth_time"
CLOCK_SKEW_SECONDS = 60


def session_user(request, max_age):
    session = request.session
    user_id = session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    auth_time = session.get(SESSION_AUTH_TIME_KEY)
    age = time.time() - auth_time if isinstance(auth_time, int) else None
    user = None
    if age is not None and -CLOCK_SKEW_SECONDS <= age <= max_age and _is_uuid(user_id):
        user = (
            User.objects.select_related("organization")
            .filter(pk=user_id, active=True, organization__active=True)
            .first()
        )
    if user is None:
        session.flush()
    return user


def _is_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings

        request.cya_user = session_user(request, settings.CYA_SESSION_MAX_AGE)
        return self.get_response(request)
