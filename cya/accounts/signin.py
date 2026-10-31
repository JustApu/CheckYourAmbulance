"""Sign-in commands: start, complete (verify and provision), and audit.

Flow (OpenID Connect authorization code flow with PKCE):

1. start_transaction() creates a random state, nonce and PKCE verifier. The
   view keeps them in a short-lived signed cookie and redirects to Google.
2. complete_sign_in() checks the returned state, exchanges the code, verifies
   the ID token (cya.accounts.google), then finds or provisions the user.

Two collaborators are injected through settings and fail closed when unset:
the initial role granter (issue 22 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/22) owns role grants; AUTH-6 "Crew only", formerly "EMT only") and
the audit sink (issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23) owns durable audit). Successful sign-ins and
provisioning are audited inside the same transaction as the user write;
refusals are audited after that transaction has rolled back, so the refusal
record survives the rollback.
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.module_loading import import_string

from cya.accounts.google import GoogleIdTokenVerifier
from cya.accounts.models import Organization, User
from cya.integrations import IdentityRejected, IntegrationNotConfigured

MAX_CODE_LENGTH = 2048


class SignInRefused(Exception):
    """Sign-in was refused. args[0] is a short reason code safe to log and show."""

    @property
    def reason(self):
        return self.args[0]


@dataclass(frozen=True)
class SignInTransaction:
    state: str
    nonce: str
    code_verifier: str

    @property
    def code_challenge(self):
        digest = hashlib.sha256(self.code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def as_dict(self):
        return {"state": self.state, "nonce": self.nonce, "code_verifier": self.code_verifier}

    @classmethod
    def from_dict(cls, data):
        fields = ("state", "nonce", "code_verifier")
        if not isinstance(data, dict) or not all(
            isinstance(data.get(f), str) and data.get(f) for f in fields
        ):
            raise SignInRefused("signin_expired")
        return cls(*(data[f] for f in fields))


@dataclass(frozen=True)
class AuthAuditEvent:
    """What sign-in reports to the audit sink. Never contains tokens or codes.

    changes holds (field, prior value, new value) for USER_PROFILE_UPDATED, so
    the audit keeps the values an update replaced (SPEC AUD-3); it is empty
    for every other action.
    """

    action: str  # SIGN_IN, USER_PROVISIONED, USER_PROFILE_UPDATED, SIGN_OUT
    outcome: str  # SUCCEEDED, REFUSED, or FAILED (could not complete)
    reason_code: str
    organization_id: str | None
    user_id: str | None
    correlation_id: str
    recorded_at: datetime
    changes: tuple[tuple[str, str, str], ...] = ()


def start_transaction():
    return SignInTransaction(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        # RFC 7636: 43-128 characters; 64 random bytes encode to 86.
        code_verifier=secrets.token_urlsafe(64),
    )


def _setting_callable(name):
    path = getattr(settings, name, None)
    if not path:
        raise IntegrationNotConfigured(f"settings.{name} is not set.")
    return import_string(path)


def audit_sink():
    return _setting_callable("CYA_AUTH_AUDIT_SINK")


def record(event):
    audit_sink()(event)


def the_organization():
    """R1 has exactly one organization; anything else fails closed."""
    organizations = list(Organization.objects.filter(active=True)[:2])
    if len(organizations) != 1:
        raise SignInRefused("organization_not_configured")
    return organizations[0]


def complete_sign_in(params, sign_in, *, provider, config, correlation_id, now=None):
    """Verify the provider's response and return the signed-in User.

    Raises SignInRefused / IdentityRejected for refusals, and lets
    IdentityProviderUnavailable / IntegrationNotConfigured propagate.
    """
    now = now or datetime.now(UTC)
    if params.get("error"):
        raise SignInRefused("provider_error")
    state = params.get("state")
    if not isinstance(state, str) or not hmac.compare_digest(
        state.encode(), sign_in.state.encode()
    ):
        raise SignInRefused("state_mismatch")
    code = params.get("code")
    if not isinstance(code, str) or not code or len(code) > MAX_CODE_LENGTH:
        raise SignInRefused("missing_code")
    id_token = provider.exchange_code(
        code=code, redirect_uri=config.redirect_uri, code_verifier=sign_in.code_verifier
    )
    verifier = GoogleIdTokenVerifier(config.client_id, config.hosted_domain, provider)
    identity = verifier.verify(id_token, nonce=sign_in.nonce, now=now)
    organization = the_organization()
    try:
        return _find_or_provision(identity, organization, correlation_id, now)
    except IntegrityError:
        # A concurrent first sign-in for the same identity inserted the row
        # first; this transaction rolled back completely, so re-reading is safe.
        return _find_or_provision(identity, organization, correlation_id, now)


def _find_or_provision(identity, organization, correlation_id, now):
    granter = _setting_callable("CYA_INITIAL_ROLE_GRANTER")
    sink = audit_sink()
    with transaction.atomic():
        # Share the organization-first order with role/bootstrap commands.
        # This also serializes concurrent first sign-ins before user creation.
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        if not organization.active:
            raise SignInRefused("organization_not_configured")
        user = (
            User.objects.select_for_update()
            .filter(
                organization=organization,
                idp_issuer=identity.issuer,
                idp_subject=identity.subject,
            )
            .first()
        )
        if user is None:
            user = User(
                organization=organization,
                idp_issuer=identity.issuer,
                idp_subject=identity.subject,
                email=identity.email,
                display_name=identity.display_name,
            )
            user.save(force_insert=True)
            granter(user=user, command_id=correlation_id)
            sink(
                _event("USER_PROVISIONED", "SUCCEEDED", "", organization, user, correlation_id, now)
            )
        elif not user.active:
            raise SignInRefused("user_inactive")
        else:
            changes = tuple(
                (field, getattr(user, field), getattr(identity, field))
                for field in ("email", "display_name")
                if getattr(user, field) != getattr(identity, field)
            )
            if changes:
                User.objects.filter(pk=user.pk).update(
                    email=identity.email, display_name=identity.display_name
                )
                user.email, user.display_name = identity.email, identity.display_name
                sink(
                    _event(
                        "USER_PROFILE_UPDATED",
                        "SUCCEEDED",
                        "",
                        organization,
                        user,
                        correlation_id,
                        now,
                        changes=changes,
                    )
                )
        sink(_event("SIGN_IN", "SUCCEEDED", "", organization, user, correlation_id, now))
    return user


def record_refusal(reason, *, correlation_id, now=None):
    """Audit a refused sign-in. Call after any sign-in transaction has ended."""
    record(_event("SIGN_IN", "REFUSED", reason, None, None, correlation_id, now))


def record_failure(reason, *, correlation_id, now=None):
    """Audit a sign-in that could not complete (outage, storage error).
    Call after any sign-in transaction has rolled back."""
    record(_event("SIGN_IN", "FAILED", reason, None, None, correlation_id, now))


def _event(action, outcome, reason, organization, user, correlation_id, now, changes=()):
    return AuthAuditEvent(
        action=action,
        outcome=outcome,
        reason_code=reason,
        organization_id=str(organization.id) if organization else None,
        user_id=str(user.id) if user else None,
        correlation_id=correlation_id,
        recorded_at=now or datetime.now(UTC),
        changes=changes,
    )


def refusal_reason(exc):
    if isinstance(exc, SignInRefused | IdentityRejected):
        return exc.reason
    return "refused"
