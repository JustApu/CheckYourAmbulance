"""Fail-closed boundary to current role evidence and durable writes.

The default factory is the database implementation. There is no in-memory production
fallback. A configured backend must read from the current DB transaction,
append role evidence plus receipt/audit atomically in that transaction, and
commit denial audit independently after a rejected transaction rolls back.
"""

from typing import Protocol
from uuid import UUID

from django.conf import settings
from django.utils.module_loading import import_string

from cya.authorization.lifecycle import current_roles


class AuthorizationUnavailable(RuntimeError):
    """Role evidence or its required durable writer is unavailable."""


class RoleBackend(Protocol):
    def role_events(self, *, organization_id, user_id):
        """Return ordered RoleEvents for exactly this identity, without caching."""

    def has_administrator_history(self, *, organization_id):
        """Include all past administrator grants, revoked and inactive included."""

    def append_role_change(self, *, user, change, correlation_id, origin):
        """Append receipt-linked evidence AND audit in the caller's transaction.

        origin is INITIAL_PROVISIONING or OPERATOR_BOOTSTRAP. correlation_id
        is request context, not a fabricated receipt FK. Resolve receipt and
        human/service attribution using issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23)'s actual command API. Never
        commit independently, perform external side effects, or retry writes.
        """

    def record_denial(self, denial):
        """Durably commit the safe denial in an independent audit transaction."""


def canonical_id(value):
    if not isinstance(value, str):
        raise ValueError("A canonical UUID is required")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("A canonical UUID is required") from None
    if str(parsed) != value:
        raise ValueError("A canonical UUID is required")
    return value


def get_backend():
    path = getattr(settings, "CYA_AUTHORIZATION_BACKEND", None)
    if not path:
        raise AuthorizationUnavailable("Authorization persistence is not configured")
    try:
        backend = import_string(path)()
        methods = (
            "role_events",
            "has_administrator_history",
            "append_role_change",
            "record_denial",
        )
        if not all(callable(getattr(backend, name, None)) for name in methods):
            raise TypeError("Incomplete authorization backend")
    except Exception as exc:
        raise AuthorizationUnavailable("Authorization backend cannot be loaded") from exc
    return backend


def backend_call(backend, method, *args, **kwargs):
    try:
        return getattr(backend, method)(*args, **kwargs)
    except Exception as exc:
        raise AuthorizationUnavailable("Authorization backend operation failed") from exc


def load_state(backend, user):
    try:
        events = backend_call(
            backend, "role_events", organization_id=str(user.organization_id), user_id=str(user.pk)
        )
        return current_roles(
            events, organization_id=str(user.organization_id), user_id=str(user.pk)
        )
    except Exception as exc:
        raise AuthorizationUnavailable("Current role evidence is invalid") from exc
