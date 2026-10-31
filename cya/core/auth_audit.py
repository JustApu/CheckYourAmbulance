"""Durable adapter for accounts.signin.AuthAuditEvent (no token/body logging).

Configure CYA_AUTH_AUDIT_SINK to record_auth_event only after a current,
uncached CYA_ROLE_LOOKUP is installed. This does not grant roles or invent
receipts for the separate sign-in/role command integration.
"""

import logging

from django.conf import settings
from django.db import connection, transaction
from django.utils.module_loading import import_string

from cya.accounts.models import Organization, User
from cya.accounts.signin import AuthAuditEvent
from cya.core.audit_repository import append_audit
from cya.core.audit_transactions import AuditPersistenceFailure

logger = logging.getLogger(__name__)
ACTIONS = frozenset({"SIGN_IN", "SIGN_OUT", "USER_PROVISIONED", "USER_PROFILE_UPDATED"})
OUTCOMES = {"SUCCEEDED": "SUCCEEDED", "REFUSED": "DENIED", "FAILED": "FAILED"}


def record_auth_event(event):
    """Success joins the caller transaction; post-rollback events commit alone."""
    try:
        if connection.in_atomic_block:
            return _append(event)
        with transaction.atomic(durable=True):
            return _append(event)
    except Exception as exc:
        if connection.in_atomic_block:
            transaction.set_rollback(True)
        logger.error("mandatory authentication audit unavailable")
        raise AuditPersistenceFailure("AUDIT_UNAVAILABLE") from exc


def _append(event):
    if (
        not isinstance(event, AuthAuditEvent)
        or event.action not in ACTIONS
        or event.outcome not in OUTCOMES
    ):
        raise ValueError("invalid authentication audit envelope")
    if event.organization_id is None:
        # R1 has exactly one configured organization. Do not fabricate a tenant
        # or misattribute anonymous failures when configuration is ambiguous.
        organizations = list(Organization.objects.all()[:2])
        if len(organizations) != 1:
            raise ValueError("authentication audit organization is not configured")
        org = organizations[0]
    else:
        org = Organization.objects.get(pk=event.organization_id)
    user, roles = None, []
    if event.user_id is not None:
        user = User.objects.select_for_update().get(pk=event.user_id, organization=org)
        lookup = settings.CYA_ROLE_LOOKUP
        if not lookup:
            raise ValueError("current role lookup is not configured")
        roles = list(import_string(lookup)(user))
    prior, new = {}, {}
    for field, before, after in event.changes:
        if (
            field not in {"email", "display_name"}
            or field in prior
            or not all(isinstance(v, str) for v in (before, after))
        ):
            raise ValueError("invalid profile audit fields")
        prior[field], new[field] = before, after
    if event.changes and event.action != "USER_PROFILE_UPDATED":
        raise ValueError("profile snapshot on wrong audit action")
    return append_audit(
        organization=org,
        actor_user=user,
        roles_at_time=roles,
        request_attempt_id=event.correlation_id,
        correlation_id=event.correlation_id,
        action=event.action,
        outcome=OUTCOMES[event.outcome],
        reason_code=event.reason_code.upper() if event.reason_code else None,
        target_type="USER" if user else None,
        target_id=str(user.pk) if user else None,
        prior_snapshot={"schema_version": 1, "values": prior} if prior else None,
        new_snapshot={"schema_version": 1, "values": new} if new else None,
    )
