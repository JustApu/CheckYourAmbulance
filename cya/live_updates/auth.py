"""Current signed-cookie identity and per-vehicle authorization for WebSockets."""

import time

from channels.db import database_sync_to_async
from django.conf import settings

from cya.accounts.middleware import SESSION_AUTH_TIME_KEY, SESSION_USER_KEY, _is_uuid
from cya.accounts.models import User
from cya.checklists.models import Vehicle
from cya.shell.navigation import ALL_ROLES, roles_for


def _active_socket_access(session, vehicle_id):
    user_id = session.get(SESSION_USER_KEY)
    auth_time = session.get(SESSION_AUTH_TIME_KEY)
    if not _is_uuid(user_id) or type(auth_time) is not int:
        return None
    age = time.time() - auth_time
    if not -60 <= age <= settings.CYA_SESSION_MAX_AGE:
        return None
    return _current_access(user_id, vehicle_id, auth_time)


def _current_access(user_id, vehicle_id, auth_time):
    """Recheck account, current roles, vehicle ownership and session age in the database."""
    if time.time() - auth_time > settings.CYA_SESSION_MAX_AGE:
        return None
    user = (
        User.objects.select_related("organization")
        .filter(pk=user_id, active=True, organization__active=True)
        .first()
    )
    if user is None:
        return None
    if not roles_for(user) & ALL_ROLES:
        return None
    if not Vehicle.objects.filter(
        pk=vehicle_id, organization_id=user.organization_id, active=True
    ).exists():
        return None
    return str(user.pk), str(user.organization_id)


@database_sync_to_async
def socket_access(session, vehicle_id):
    return _active_socket_access(session, vehicle_id)


@database_sync_to_async
def socket_access_still_valid(user_id, organization_id, vehicle_id, auth_time):
    access = _current_access(user_id, vehicle_id, auth_time)
    return access == (str(user_id), str(organization_id))
