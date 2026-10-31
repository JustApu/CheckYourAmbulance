"""Sign-in, sign-out and sign-in status pages.

There is no password login and no other sign-in route (SPEC AUTH-5, OQ-13).
Pages are deliberately minimal; the application shell (issue 24 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/24)) styles them.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from django.http import HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from cya.accounts import signin
from cya.accounts.middleware import SESSION_AUTH_TIME_KEY, SESSION_USER_KEY
from cya.accounts.oidc import authorization_url, google_config
from cya.integrations import (
    IdentityProviderUnavailable,
    IdentityRejected,
    IntegrationNotConfigured,
    get_oidc_provider,
)

logger = logging.getLogger(__name__)

# The sign-in transaction cookie must reach the callback, which arrives as a
# cross-site top-level navigation from Google, so it is SameSite=Lax (the
# session cookie itself is Strict). It is signed, HttpOnly, Secure, limited to
# /auth/, expires after 10 minutes, and is deleted when the callback runs.
SIGNIN_COOKIE = "cya_signin"
SIGNIN_COOKIE_SALT = "cya.accounts.signin"
SIGNIN_COOKIE_MAX_AGE = 600
SIGNIN_COOKIE_PATH = "/auth/"
STATUS_PATH = "/auth/status/"

# Constant page fragments (no user data, so safe to mark as HTML).
UNAVAILABLE_HTML = mark_safe("<p>Sign-in is not available right now. Try again later.</p>")
NOT_SIGNED_IN_HTML = mark_safe('<p><a href="/auth/login/">Sign in</a></p>')
SIGNED_OUT_HTML = mark_safe('<p><a href="/auth/login/">Sign in again</a></p>')


def _page(request, title, body, status=200, refresh_to=None):
    html = render_to_string(
        "shell/base.html",
        {"title": title, "body": body, "refresh_to": refresh_to},
        request=request,
    )
    response = HttpResponse(html, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _unavailable(request, correlation_id=None):
    body = UNAVAILABLE_HTML
    if correlation_id:
        body = format_html("{}<p>Reference: {}</p>", UNAVAILABLE_HTML, correlation_id)
    return _page(request, "Sign-in unavailable", body, status=503)


def _log_failure(what, exc, correlation_id):
    """Log a handled failure without its message or traceback.

    Exceptions from adapters, collaborators and the database have no
    sanitization contract: their text could hold tokens, credentials or
    profile data (SPEC 16.2). Only a fixed description, the exception class
    names (including any chained causes) and the correlation ID are logged.
    """
    names = []
    seen = set()
    while exc is not None and id(exc) not in seen and len(names) < 5:
        seen.add(id(exc))
        names.append(type(exc).__name__)
        exc = exc.__cause__ or exc.__context__
    logger.error("%s (%s); correlation id %s", what, " <- ".join(names), correlation_id)


@never_cache
@require_GET
def sign_in(request):
    try:
        config = google_config()
        signin.audit_sink()  # refuse to start if refusals could not be audited
    except IntegrationNotConfigured:
        return _unavailable(request)
    transaction = signin.start_transaction()
    response = HttpResponseRedirect(
        authorization_url(
            config,
            state=transaction.state,
            nonce=transaction.nonce,
            code_challenge=transaction.code_challenge,
        )
    )
    response.set_signed_cookie(
        SIGNIN_COOKIE,
        json.dumps(transaction.as_dict()),
        salt=SIGNIN_COOKIE_SALT,
        max_age=SIGNIN_COOKIE_MAX_AGE,
        path=SIGNIN_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


def _read_transaction(request):
    raw = request.get_signed_cookie(
        SIGNIN_COOKIE, default=None, salt=SIGNIN_COOKIE_SALT, max_age=SIGNIN_COOKIE_MAX_AGE
    )
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        data = None
    return signin.SignInTransaction.from_dict(data)


@never_cache
@require_GET
def callback(request):
    correlation_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    response = _callback_response(request, correlation_id, now)
    # Every exit, including failures, retires the one-time transaction cookie.
    response.delete_cookie(SIGNIN_COOKIE, path=SIGNIN_COOKIE_PATH, samesite="Lax")
    return response


def _callback_response(request, correlation_id, now):
    """Run the sign-in command and map every outcome to a safe page; never raises."""
    try:
        transaction = _read_transaction(request)
        config = google_config()
        user = signin.complete_sign_in(
            request.GET,
            transaction,
            provider=get_oidc_provider(),
            config=config,
            correlation_id=correlation_id,
            now=now,
        )
    except (signin.SignInRefused, IdentityRejected) as exc:
        reason = signin.refusal_reason(exc)
        if not _audit(signin.record_refusal, reason, correlation_id, now):
            # A refusal that cannot be audited is not reported as one.
            return _unavailable(request, correlation_id)
        return _page(
            request,
            "Sign-in refused",
            format_html(
                "<p>This account cannot sign in. Use your organization Google Workspace "
                "account, or ask an administrator.</p><p>Reference: {} ({})</p>",
                reason,
                correlation_id,
            ),
            status=403,
        )
    except IdentityProviderUnavailable:
        _audit(signin.record_failure, "provider_unavailable", correlation_id, now)
        return _unavailable(request, correlation_id)
    except IntegrationNotConfigured:
        _audit(signin.record_failure, "not_configured", correlation_id, now)
        return _unavailable(request, correlation_id)
    except Exception as exc:
        # Collaborator or storage failure (role grant, audit, database). The
        # sign-in transaction has rolled back; record the failure separately.
        _log_failure("sign-in failed", exc, correlation_id)
        _audit(signin.record_failure, "internal_error", correlation_id, now)
        return _unavailable(request, correlation_id)
    # Start an empty session under a new key: nothing from an earlier session
    # (possibly another person on a shared device) carries over.
    request.session.flush()
    request.session[SESSION_USER_KEY] = str(user.id)
    request.session[SESSION_AUTH_TIME_KEY] = int(now.timestamp())
    # A redirect would inherit the cross-site context of Google's redirect,
    # and the SameSite=Strict session cookie would not be sent with it.
    # A page that refreshes to our own URL starts a same-site navigation.
    return _page(
        request,
        "Signed in",
        format_html('<p><a href="{}">Continue</a></p>', STATUS_PATH),
        refresh_to=STATUS_PATH,
    )


def _audit(recorder, reason, correlation_id, now):
    """Record a refusal or failure outside any business transaction.
    Returns False (and logs) if the audit sink is missing or failing."""
    try:
        recorder(reason, correlation_id=correlation_id, now=now)
    except Exception as exc:
        _log_failure(f"could not audit sign-in {reason}", exc, correlation_id)
        return False
    return True


@never_cache
@require_GET
def status(request):
    user = request.cya_user
    if user is None:
        return _page(request, "Not signed in", NOT_SIGNED_IN_HTML)
    return _page(
        request,
        "Signed in",
        format_html("<p>{} ({})</p>", user.display_name or user.email, user.email),
    )


@never_cache
@require_POST
def sign_out(request):
    user = request.cya_user
    # Signing out only removes access, so the session always ends, even if
    # the audit record cannot be written; that case is reported as a 503.
    request.session.flush()
    request.cya_user = None  # the confirmation page must not look signed in
    if user is None:
        return _page(request, "Signed out", SIGNED_OUT_HTML)
    correlation_id = str(uuid.uuid4())
    try:
        signin.record(
            signin.AuthAuditEvent(
                action="SIGN_OUT",
                outcome="SUCCEEDED",
                reason_code="",
                organization_id=str(user.organization_id),
                user_id=str(user.id),
                correlation_id=correlation_id,
                recorded_at=datetime.now(UTC),
            )
        )
    except Exception as exc:
        _log_failure("could not audit sign-out", exc, correlation_id)
        return _unavailable(request, correlation_id)
    return _page(request, "Signed out", SIGNED_OUT_HTML)
