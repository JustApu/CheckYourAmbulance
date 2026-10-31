"""Recording role backend for tests only; no persistence guarantee is claimed."""

from uuid import uuid4

from cya.authorization.lifecycle import RoleEvent
from cya.authorization.policy import Role


class RecordingBackend:
    def __init__(self):
        self.events = {}
        self.appends = []
        self.denials = []
        self.fail_append = False
        self.fail_denial = False
        self.on_denial = None

    def set_roles(self, user, *roles):
        self.events[str(user.pk)] = [
            RoleEvent(str(uuid4()), str(user.organization_id), str(user.pk), index, role, "GRANT")
            for index, role in enumerate(roles, 1)
        ]

    def role_events(self, *, organization_id, user_id):
        return tuple(self.events.get(user_id, ()))

    def has_administrator_history(self, *, organization_id):
        return any(
            event.organization_id == organization_id
            and event.role == Role.ADMINISTRATOR
            and event.action == "GRANT"
            for events in self.events.values()
            for event in events
        )

    def append_role_change(self, *, user, change, correlation_id, origin):
        if self.fail_append:
            raise RuntimeError("synthetic unavailable writer")
        self.appends.append((str(user.pk), change, correlation_id, origin))
        self.events.setdefault(str(user.pk), []).append(
            RoleEvent(
                str(uuid4()),
                str(user.organization_id),
                str(user.pk),
                change.sequence,
                change.role,
                change.action,
                change.prior_grant_id,
            )
        )

    def record_denial(self, denial):
        if self.fail_denial:
            raise RuntimeError("synthetic unavailable audit")
        if self.on_denial:
            self.on_denial()
        self.denials.append(denial)


BACKEND = None


def configure_backend():
    global BACKEND
    BACKEND = RecordingBackend()
    return BACKEND


def recording_backend():
    if BACKEND is None:
        raise RuntimeError("Test backend is not initialized")
    return BACKEND
