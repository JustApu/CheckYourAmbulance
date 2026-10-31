"""Initial sign-in role collaborator backed by receipt-linked role evidence."""

from django.db import connection

from cya.accounts.models import User
from cya.authorization.backend import (
    AuthorizationUnavailable,
    backend_call,
    canonical_id,
    get_backend,
    load_state,
)
from cya.authorization.lifecycle import plan_initial_crew
from cya.authorization.locking import locked_identities


def grant_initial_crew(*, user, command_id):
    """Interface required by accounts.signin, inside its provisioning transaction.

    command_id is the sign-in correlation ID supplied by accounts, not an
    already-persisted command receipt ID. The backend resolves the durable
    receipt/audit linkage. Return only after append succeeds; failures propagate
    so sign-in rolls the newly created user back. This never opens a new commit.
    """
    canonical_id(command_id)
    if not connection.in_atomic_block or not isinstance(user, User) or user._state.adding:
        raise AuthorizationUnavailable("Initial role grant requires a persisted user transaction")
    # The provisioning caller takes the organization lock before user creation.
    # Reacquiring it is safe and gives direct callers the same lock order.
    with locked_identities(str(user.organization_id), [str(user.pk)]) as (org, users):
        current = users[str(user.pk)]
        if not current.active or not org.active:
            raise AuthorizationUnavailable("Initial role grant requires an active identity")
        backend = get_backend()
        change = plan_initial_crew(load_state(backend, current))
        backend_call(
            backend,
            "append_role_change",
            user=current,
            change=change,
            correlation_id=command_id,
            origin="INITIAL_PROVISIONING",
        )


# Deprecated backward-compatibility alias for grant_initial_crew
grant_initial_emt = grant_initial_crew
