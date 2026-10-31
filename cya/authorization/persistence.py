"""Current role reads and atomic, receipt-linked provisioning/operator writes."""

import hashlib
import json
import logging
from uuid import uuid4

from django.db import InterfaceError, OperationalError, connection, transaction

from cya.accounts.models import Organization, User
from cya.authorization.backend import AuthorizationUnavailable, load_state
from cya.authorization.lifecycle import BootstrapRefused, RoleEvent
from cya.authorization.models import RoleGrant
from cya.authorization.policy import Role
from cya.core.audit_repository import append_audit, append_receipt
from cya.core.audit_transactions import AuditPersistenceFailure
from tools.command_identity import worker_operation_id

logger = logging.getLogger(__name__)


def record_bootstrap_failure(organization_id, user_id, correlation_id, *, error):
    """Audit an operator attempt after rollback; dry runs never call this."""
    try:
        # A lost commit response must not claim a confirmed rollback.
        cause, unknown = error, False
        for _ in range(8):
            if cause is None:
                break
            unknown |= isinstance(cause, InterfaceError) or (
                isinstance(cause, OperationalError)
                and bool(cause.args)
                and cause.args[0] in (2006, 2013, 2055)
            )
            cause = cause.__cause__ or cause.__context__
        refused = isinstance(error.__cause__, BootstrapRefused)
        if unknown:
            connection.close()
        with transaction.atomic(durable=True):
            org = Organization.objects.get(pk=organization_id)
            target = User.objects.filter(pk=user_id, organization=org).first()
            append_audit(
                organization=org,
                roles_at_time=[],
                service_identity="operator.bootstrap",
                job_key=json.dumps([user_id, correlation_id], separators=(",", ":")),
                request_attempt_id=correlation_id,
                correlation_id=correlation_id,
                action="BOOTSTRAP_ADMINISTRATOR",
                outcome="OUTCOME_UNKNOWN" if unknown else ("DENIED" if refused else "FAILED"),
                reason_code="OUTCOME_UNKNOWN"
                if unknown
                else ("BOOTSTRAP_REFUSED" if refused else "BOOTSTRAP_FAILED"),
                target_type="USER" if target else None,
                target_id=target.pk if target else None,
            )
    except Exception as exc:
        logger.error("mandatory bootstrap audit unavailable")
        raise AuditPersistenceFailure("AUDIT_UNAVAILABLE") from exc


def digest(values):
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def append_change(*, user, change, receipt, actor_roles):
    """Caller holds organization/user locks and owns the surrounding transaction."""
    return RoleGrant.objects.create(
        organization_id=user.organization_id,
        user=user,
        command=receipt,
        actor_user_id=receipt.actor_user_id,
        actor_roles_at_time=list(actor_roles),
        recorded_at=receipt.recorded_at,
        sequence=change.sequence,
        role=change.role.value,
        action=change.action,
        prior_grant_id=change.prior_grant_id,
    )


class DatabaseRoleBackend:
    def role_events(self, *, organization_id, user_id):
        rows = RoleGrant.objects.filter(organization_id=organization_id, user_id=user_id).order_by(
            "sequence"
        )
        return (
            RoleEvent(
                row.pk,
                row.organization_id,
                row.user_id,
                row.sequence,
                row.role,
                row.action,
                row.prior_grant_id,
            )
            for row in rows.iterator()
        )

    def has_administrator_history(self, *, organization_id):
        return RoleGrant.objects.filter(
            organization_id=organization_id, role="ADMINISTRATOR", action="GRANT"
        ).exists()

    def append_role_change(self, *, user, change, correlation_id, origin):
        if not connection.in_atomic_block:
            raise AuthorizationUnavailable("Role evidence requires a transaction")
        services = {
            "INITIAL_PROVISIONING": "identity.provisioning",
            "OPERATOR_BOOTSTRAP": "operator.bootstrap",
        }
        if origin not in services:
            raise ValueError("Unknown role origin")
        command_type = (
            "INITIAL_CREW" if origin == "INITIAL_PROVISIONING" else "BOOTSTRAP_ADMINISTRATOR"
        )
        service = services[origin]
        job_key = json.dumps([user.pk, correlation_id], separators=(",", ":"))
        receipt = append_receipt(
            organization=user.organization,
            operation_id=str(
                worker_operation_id(str(user.organization_id), service, command_type, job_key)
            ),
            service_identity=service,
            job_key=job_key,
            command_type=command_type,
            request_digest=digest({"user": user.pk, "role": change.role.value, "origin": origin}),
            response_status=200,
            response_body={"code": "SAVED", "user_id": user.pk},
            correlation_id=correlation_id,
        )
        append_change(user=user, change=change, receipt=receipt, actor_roles=())
        append_audit(
            organization=user.organization,
            command=receipt,
            service_identity=service,
            job_key=job_key,
            roles_at_time=[],
            request_attempt_id=correlation_id,
            correlation_id=correlation_id,
            action=command_type,
            outcome="SUCCEEDED",
            target_type="USER",
            target_id=user.pk,
            new_snapshot={
                "schema_version": 1,
                "values": {"role": change.role.value, "action": change.action},
            },
        )

    def record_denial(self, denial):
        # This is called only after the guarded body has rolled back. Anonymous
        # or cross-tenant requests retain no fabricated user/service identity.
        if connection.in_atomic_block:
            raise AuthorizationUnavailable("Denial audit requires an independent transaction")
        with transaction.atomic(durable=True):
            org = Organization.objects.get(pk=denial.organization_id)
            user = (
                User.objects.get(pk=denial.actor_user_id, organization=org)
                if denial.actor_user_id
                else None
            )
            attempt = str(uuid4())
            append_audit(
                organization=org,
                actor_user=user,
                roles_at_time=list(denial.actor_roles),
                request_attempt_id=attempt,
                correlation_id=attempt,
                action=denial.action.upper(),
                outcome="DENIED",
                reason_code=denial.reason_code,
            )


def current_role_codes(user):
    """Uncached role lookup for sign-in and shell; current inactive users get none."""
    current = User.objects.select_related("organization").get(
        pk=user.pk, organization_id=user.organization_id
    )
    if not current.active or not current.organization.active:
        return ()
    grants = load_state(DatabaseRoleBackend(), current).grants
    canonical_roles = {Role.CREW if role == Role.EMT else role for role in grants}
    return tuple(sorted(role.value for role in canonical_roles))
