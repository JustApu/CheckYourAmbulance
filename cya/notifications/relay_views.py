"""Outgoing-mail relay settings page (SPEC 4.2.0 DEP-13, AC-38; issue #8).

Administrators only (configure_schedules_thresholds_notifications); anyone else gets 403 and an
audited denial, and the command refuses a direct post the same way. The password is write-only:
the form never contains it, and leaving the field blank keeps the stored one. "Send test email"
records an audited request, then sends through the saved relay and reports a fixed safe code.
"""

import logging
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.kits import admin_pages
from cya.kits.views import UNAVAILABLE, _context_ids
from cya.notifications import relay as relay_rules
from cya.notifications.commands import (
    POLICY_ACTION,
    configure_mail_relay,
    request_mail_relay_test,
)
from cya.notifications.models import MailRelay

logger = logging.getLogger(__name__)

DENIAL = "CONFIGURE_NOTIFICATIONS"
SAVED = {"saved": "Relay settings saved."}
TEST_RESULTS = {
    "SENT": "Test email sent to every operator and deputy address. Check that it arrived.",
    "CONNECT_FAILED": "The relay could not be reached. Check the host and port.",
    "TLS_FAILED": "The relay did not accept an encrypted connection. Check the TLS setting.",
    "AUTH_FAILED": "The relay refused the username or password.",
    "REJECTED": "The relay refused the message. Check the sender and recipient addresses, and "
    "that the relay allows this server.",
    "TIMEOUT": "The relay did not answer in time.",
    "KEY_MISSING": "The server cannot read the stored password. Ask the operator to check the "
    "mail relay key in the secrets file.",
    "UNKNOWN": "The test email could not be sent.",
    "CHANGED": "The relay settings changed while the test was starting. Nothing was sent; try "
    "again.",
}


def _form_from(relay):
    if relay is None:
        return {"tls_mode": "STARTTLS", "auth_mode": "PASSWORD", "port": "587"}
    return {
        "host": relay.host,
        "port": str(relay.port),
        "tls_mode": relay.tls_mode,
        "auth_mode": relay.auth_mode,
        "username": relay.username,
        "sender": relay.sender,
        "operator": "\n".join(relay.operator_addresses),
        "deputy": "\n".join(relay.deputy_addresses),
    }


def _page(request, user, *, status=200, errors=None, form=None, test_result=None):
    relay = MailRelay.objects.filter(organization_id=user.organization_id).first()
    context = {
        "title": "Outgoing mail",
        "relay": relay,
        "password_set": bool(relay and relay.password_ciphertext),
        "revision": relay.revision if relay else 0,
        "form": form or _form_from(relay),
        "operation_id": admin_pages.new_operation_id(),
        "test_operation_id": admin_pages.new_operation_id(),
        "errors": errors or [],
        "saved": SAVED.get(request.GET.get("saved", "")),
        "test_result": test_result,
        "max_recipients": relay_rules.MAX_RECIPIENTS,
    }
    return render(request, "notifications/mail_relay.html", context, status=status)


def _ids(request, user):
    operation_id = admin_pages.operation_id_from(request)
    if operation_id is None:
        return None
    return {**_context_ids(user), "operation_id": operation_id}


def _run(request, render_error, call):
    try:
        return call(), None
    except relay_rules.RelayError as error:
        return None, render_error(400, [str(error)])
    except CommandDenied:
        return None, render(request, "shell/forbidden.html", {"title": "Not permitted"}, status=403)
    except OperationConflict:
        return None, render_error(
            409, ["this form was already used for a different change; try again"]
        )
    except UNAVAILABLE as error:
        logger.error("mail relay change unavailable (%s)", type(error).__name__)
        return None, render_error(
            503, ["the change could not be completed; nothing was changed. Try again."]
        )


@never_cache
@require_http_methods(["GET", "POST"])
def mail_relay(request):
    allowed, refused = admin_pages.allowed(request, POLICY_ACTION, DENIAL)
    if refused:
        return refused
    user, _roles = allowed
    if request.method == "GET":
        return _page(request, user)
    stage = request.POST.get("stage")
    form = {
        key: request.POST.get(key, "")
        for key in (
            "host",
            "port",
            "tls_mode",
            "auth_mode",
            "username",
            "sender",
            "operator",
            "deputy",
        )
    }

    def render_error(status, errors):
        return _page(
            request, user, status=status, errors=errors, form=form if stage == "save" else None
        )

    ids = _ids(request, user)
    if stage not in ("save", "test") or ids is None:
        return render_error(400, ["the form was not valid; try again"])
    if stage == "test":
        result, failed = _run(request, render_error, lambda: request_mail_relay_test(**ids))
        if failed:
            return failed
        # Send only through the revision the audited request named (PR #188 review): a save
        # that lands in between is never tested under the earlier request.
        relay = MailRelay.objects.filter(
            organization_id=user.organization_id, revision=result.body["revision"]
        ).first()
        code = relay_rules.send_test(relay) if relay is not None else "CHANGED"
        logger.info("mail relay test: %s", code)
        return _page(request, user, test_result={"ok": code == "SENT", "text": TEST_RESULTS[code]})
    try:
        base_revision = int(request.POST.get("base_revision", ""))
    except ValueError:
        return render_error(400, ["the form was not valid; try again"])
    password = request.POST.get("password", "")
    _result, failed = _run(
        request,
        render_error,
        lambda: configure_mail_relay(
            host=form["host"],
            port=form["port"],
            tls_mode=form["tls_mode"],
            auth_mode=form["auth_mode"],
            username=form["username"],
            password=password or None,
            sender=form["sender"],
            operator_addresses=form["operator"],
            deputy_addresses=form["deputy"],
            base_revision=base_revision,
            **ids,
        ),
    )
    if failed:
        return failed
    return HttpResponseRedirect(reverse("mail-relay") + "?" + urlencode({"saved": "saved"}))
