"""Authorization command boundary; adapters supply trusted current DB facts.

The caller owns the command transaction and identity-row locks. A denial must
be caught outside that transaction and persisted using record_denial below.
HTTP/session integration and the durable audit adapter are separate wiring;
there is deliberately no successful fallback for a missing/failed audit sink.
"""

import logging
from dataclasses import dataclass

from cya.authorization.policy import POLICY, permitted

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    organization_id: str
    user_id: str
    roles: frozenset[str]
    active: bool


@dataclass(frozen=True)
class Denial:
    organization_id: str
    actor_user_id: str | None
    actor_roles: tuple[str, ...]
    action: str
    reason_code: str


class PermissionDenied(Exception):
    """Command rejected; the business transaction must roll back before audit."""

    def __init__(self, denial):
        super().__init__(denial.reason_code)
        self.denial = denial


class AuditUnavailable(Exception):
    """No durable denial acknowledgement is possible; return service failure."""


def require_permission(principal, organization_id, action, *, context=None):
    """Raise before any business mutation. Principal must be loaded per request."""
    same_organization = (
        isinstance(principal, Principal) and principal.organization_id == organization_id
    )
    if same_organization and permitted(
        action, principal.user_id, principal.roles, active=principal.active, context=context
    ):
        return
    # Do not put unbounded attacker-controlled action strings in the audit log.
    audit_action = action if isinstance(action, str) and action in POLICY else "UNLISTED_ACTION"
    raise PermissionDenied(
        Denial(
            organization_id=organization_id,
            actor_user_id=principal.user_id if same_organization else None,
            actor_roles=tuple(sorted(principal.roles)) if same_organization else (),
            action=audit_action,
            reason_code="FORBIDDEN",
        )
    )


def record_denial(error, audit_sink):
    """Call only after the rejected transaction rolled back.

    audit_sink must commit the Denial in an independent audit transaction or
    raise. A failed audit never becomes an ordinary successfully logged 403.
    The production adapter is supplied by the audit integration, not logging
    to stdout or an in-memory success fallback.
    """
    if not isinstance(error, PermissionDenied):
        raise TypeError("record_denial requires PermissionDenied")
    if not callable(audit_sink):
        raise AuditUnavailable("denial audit is not configured")
    try:
        audit_sink(error.denial)
    except Exception as exc:
        logger.error("mandatory denial audit unavailable")
        raise AuditUnavailable("denial audit failed") from exc
