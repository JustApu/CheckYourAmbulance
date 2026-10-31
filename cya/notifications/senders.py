"""Email transport boundary: an injected sender, a safe message, fixed error codes (AC-28, NOT-4).

Delivery code never imports a mail library. A sender is any object with send(message); it raises
SenderError(code) for a failure it understands, and any other exception is recorded as UNKNOWN so
no exception text, address or provider detail reaches the database. The message carries a fixed
subject and a deep link, never payload text: patient data cannot leak through an email body.

SmtpSender sends through Django's configured email backend. SMTP has no idempotency key, so a
message handed to the server whose attempt row was never written can be sent again (at-least-once);
see docs/notifications.md.
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils.module_loading import import_string

from cya.notifications.models import ERROR_CODES

SUBJECT = "Check Your Ambulance: you have a new notification"
PERMANENT_CODES = frozenset({"REJECTED", "INVALID_ADDRESS"})


class SenderError(Exception):
    """A transport failure with a code from the fixed set; the message is never stored."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code if code in ERROR_CODES else "UNKNOWN"


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str
    reference: str  # the outbox id, so a transport that supports it can deduplicate


def render(outbox, to, base_url):
    link = f"{base_url.rstrip('/')}{outbox.deep_link}" if base_url else outbox.deep_link
    body = (
        "There is an update waiting for you in Check Your Ambulance.\n\n"
        f"Open it here: {link}\n\n"
        "Sign in to see the details. This message contains no inspection or patient information."
    )
    return OutgoingEmail(to=to, subject=SUBJECT, body=body, reference=str(outbox.pk))


class SmtpSender:
    """Django email backend adapter; at-least-once, with no transport idempotency claim."""

    def send(self, message):
        mail = EmailMessage(
            subject=message.subject,
            body=message.body,
            to=[message.to],
            headers={"X-CYA-Reference": message.reference},
        )
        try:
            if mail.send(fail_silently=False) != 1:
                raise SenderError("REJECTED")
        except SenderError:
            raise
        except Exception as error:
            raise SenderError("UNAVAILABLE") from error


def configured_sender():
    """The sender named by CYA_NOTIFICATION_SENDER (a dotted path to a factory), else None."""
    path = getattr(settings, "CYA_NOTIFICATION_SENDER", "")
    return import_string(path)() if path else None
