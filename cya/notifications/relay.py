"""The organization's outgoing-mail relay (SPEC 4.2.0 DEP-13, DEP-10; issue #8).

Validation of the settings an Administrator enters, encryption of the relay password, the SMTP
transport behind the "send test email" action, and the host-copy payload that the root-run
export writes for the host alerts.

- **TLS is required.** STARTTLS or implicit TLS; a plaintext relay is refused.
- **Authentication** is a username and password, or the server's IP address alone (the relay
  operator allows it on their side, as Google Workspace's SMTP relay can). With IP authentication
  no credential is stored.
- **The password** is stored as Fernet ciphertext under CYA_MAIL_RELAY_KEY, a key held in the
  root-owned secrets file (DEP-10 exception). It is never rendered back; the audit snapshot
  records only whether one is set.
- **Errors** from the transport map to a fixed set of safe codes. No exception text, address or
  provider reply reaches the database or the page.
"""

import json
import re
import smtplib
import ssl
from email.message import EmailMessage

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.module_loading import import_string

from cya.notifications.models import AUTH_MODES, TLS_MODES

MAX_RECIPIENTS = 10  # per role
HOST_RE = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
TIMEOUT_SECONDS = 20
TEST_SUBJECT = "Check Your Ambulance: test message from the outgoing-mail relay"
TEST_BODY = (
    "This is a test message sent from the Check Your Ambulance outgoing-mail relay settings.\n\n"
    "If you received it, operations alerts can reach you. No reply is needed."
)
# The fixed, safe outcome codes of a test send (and of the host alert sender).
SEND_CODES = (
    "SENT",
    "CONNECT_FAILED",
    "TLS_FAILED",
    "AUTH_FAILED",
    "REJECTED",
    "TIMEOUT",
    "KEY_MISSING",
    "UNKNOWN",
)


class RelayError(ValueError):
    """Invalid relay settings; the message is safe to show on the page."""


class SendFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code if code in SEND_CODES else "UNKNOWN"


def _fernet():
    key = getattr(settings, "CYA_MAIL_RELAY_KEY", "")
    if not key:
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        return None


def encrypt_password(password):
    fernet = _fernet()
    if fernet is None:
        raise RelayError(
            "the server has no mail relay key; ask the operator to set CYA_MAIL_RELAY_KEY in the "
            "secrets file before saving a password"
        )
    return fernet.encrypt(password.encode()).decode()


def decrypt_password(ciphertext):
    """The stored password, or raise SendFailure("KEY_MISSING") if it cannot be read."""
    if not ciphertext:
        return ""
    fernet = _fernet()
    if fernet is None:
        raise SendFailure("KEY_MISSING")
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise SendFailure("KEY_MISSING") from None


def _addresses(values, role):
    if isinstance(values, str):
        values = [v for v in re.split(r"[\s,;]+", values) if v]
    if not isinstance(values, (list, tuple)):
        raise RelayError(f"{role} addresses are not valid")
    cleaned = []
    for value in values:
        value = str(value).strip()
        try:
            validate_email(value)
        except ValidationError:
            raise RelayError(
                f"{role} address {value[:80]!r} is not a valid email address"
            ) from None
        if value.lower() not in (c.lower() for c in cleaned):
            cleaned.append(value)
    if not cleaned:
        raise RelayError(f"enter at least one {role} address")
    if len(cleaned) > MAX_RECIPIENTS:
        raise RelayError(f"enter at most {MAX_RECIPIENTS} {role} addresses")
    return cleaned


def validate(*, host, port, tls_mode, auth_mode, username, sender, operator, deputy):
    """The normalized settings, or raise RelayError naming the first problem."""
    host = (host or "").strip()
    if not HOST_RE.fullmatch(host):
        raise RelayError("enter the relay's host name")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise RelayError("enter the relay's port number") from None
    if not 1 <= port <= 65535:
        raise RelayError("the port must be between 1 and 65535")
    if tls_mode not in TLS_MODES:
        # SPEC DEP-13: a plaintext connection is refused.
        raise RelayError("choose STARTTLS or TLS; a relay without encryption is not allowed")
    if auth_mode not in AUTH_MODES:
        raise RelayError("choose how the relay authenticates this server")
    username = (username or "").strip()
    if auth_mode == "PASSWORD" and not username:
        raise RelayError("enter the relay username, or choose IP address authentication")
    if len(username) > 254:
        raise RelayError("the username is too long")
    sender = (sender or "").strip()
    try:
        validate_email(sender)
    except ValidationError:
        raise RelayError("enter a valid sender address") from None
    return {
        "host": host.lower(),
        "port": port,
        "tls_mode": tls_mode,
        "auth_mode": auth_mode,
        "username": username if auth_mode == "PASSWORD" else "",
        "sender": sender,
        "operator_addresses": _addresses(operator, "operator"),
        "deputy_addresses": _addresses(deputy, "deputy"),
    }


def snapshot(relay):
    """The audited view of a relay: every field except the password, which is only 'set'."""
    if relay is None:
        return None
    return {
        "schema_version": 1,
        "values": {
            "host": relay.host,
            "port": relay.port,
            "tls_mode": relay.tls_mode,
            "auth_mode": relay.auth_mode,
            "username": relay.username,
            "password_set": bool(relay.password_ciphertext),
            "sender": relay.sender,
            "operator_addresses": list(relay.operator_addresses),
            "deputy_addresses": list(relay.deputy_addresses),
            "revision": relay.revision,
        },
    }


def host_copy(relay, organization_id):
    """The JSON document the root-run export writes for the host alerts (DEP-13 host copy)."""
    return json.dumps(
        {
            "schema_version": 1,
            "organization_id": str(organization_id),
            "revision": relay.revision,
            "host": relay.host,
            "port": relay.port,
            "tls_mode": relay.tls_mode,
            "auth_mode": relay.auth_mode,
            "username": relay.username,
            "password": decrypt_password(relay.password_ciphertext),
            "sender": relay.sender,
            "operator_addresses": list(relay.operator_addresses),
            "deputy_addresses": list(relay.deputy_addresses),
        },
        indent=2,
        sort_keys=True,
    )


class SmtpTransport:
    """Send one message through a relay over TLS; map every failure to a safe code."""

    def send(self, relay, password, recipients, subject, body):
        message = EmailMessage()
        message["From"] = relay.sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)
        context = ssl.create_default_context()
        try:
            if relay.tls_mode == "TLS":
                client = smtplib.SMTP_SSL(
                    relay.host, relay.port, timeout=TIMEOUT_SECONDS, context=context
                )
            else:
                client = smtplib.SMTP(relay.host, relay.port, timeout=TIMEOUT_SECONDS)
            with client:
                if relay.tls_mode == "STARTTLS":
                    client.starttls(context=context)
                if relay.auth_mode == "PASSWORD":
                    client.login(relay.username, password)
                refused = client.send_message(message)
            if refused:
                raise SendFailure("REJECTED")
        except SendFailure:
            raise
        except smtplib.SMTPAuthenticationError:
            raise SendFailure("AUTH_FAILED") from None
        except (smtplib.SMTPNotSupportedError, ssl.SSLError, ssl.CertificateError):
            raise SendFailure("TLS_FAILED") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise SendFailure("REJECTED") from None
        except TimeoutError:
            raise SendFailure("TIMEOUT") from None
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError):
            raise SendFailure("CONNECT_FAILED") from None
        except Exception:
            raise SendFailure("UNKNOWN") from None


def transport():
    return import_string(settings.CYA_MAIL_RELAY_TRANSPORT)()


def send_test(relay):
    """Send the test message to every operator and deputy address; return a SEND_CODES code."""
    try:
        password = decrypt_password(relay.password_ciphertext)
        recipients = list(dict.fromkeys([*relay.operator_addresses, *relay.deputy_addresses]))
        transport().send(relay, password, recipients, TEST_SUBJECT, TEST_BODY)
    except SendFailure as failure:
        return failure.code
    return "SENT"
