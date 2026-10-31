"""Input rules shared by the outbox writers: closed channel set, event codes, payloads, links."""

import json
import re

from cya.notifications.models import CHANNELS, EVENT_TYPE_PATTERN, TEMPLATE_PATTERN

MAX_PAYLOAD_KEYS = 12
MAX_PAYLOAD_VALUE = 200
MAX_PAYLOAD_BYTES = 2000
PAYLOAD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DEEP_LINK = re.compile(r"^/[A-Za-z0-9._~/?=&%-]{0,299}$")


class NotificationError(ValueError):
    """A notification rule was violated; the command rolls back and is audited as failed."""

    def __init__(self, message, *, code="INVALID_NOTIFICATION", status=422):
        super().__init__(message)
        self.code = code
        self.status = status


def event_type(value):
    if not isinstance(value, str) or not re.fullmatch(EVENT_TYPE_PATTERN, value):
        raise NotificationError("event_type is invalid", code="MALFORMED_REQUEST", status=400)
    return value


def template_key(value):
    if not isinstance(value, str) or not re.fullmatch(TEMPLATE_PATTERN, value):
        raise NotificationError("template_key is invalid", code="MALFORMED_REQUEST", status=400)
    return value


def channel(value):
    if value not in CHANNELS:
        raise NotificationError(
            f"channel must be one of {list(CHANNELS)}", code="MALFORMED_REQUEST", status=400
        )
    return value


def deep_link(value):
    """A same-origin path only: never a scheme, host or protocol-relative URL."""
    if not isinstance(value, str) or not DEEP_LINK.fullmatch(value) or value.startswith("//"):
        raise NotificationError(
            "deep_link must be a same-site path", code="MALFORMED_REQUEST", status=400
        )
    return value


def payload(value):
    """Minimal operational detail: short strings, integers and booleans under closed key names.

    The payload carries identifiers and labels, never free text from a patient record (NOT-4).
    """
    if not isinstance(value, dict) or len(value) > MAX_PAYLOAD_KEYS:
        raise NotificationError("payload is invalid", code="MALFORMED_REQUEST", status=400)
    for key, item in value.items():
        text_ok = isinstance(item, str) and 0 < len(item) <= MAX_PAYLOAD_VALUE
        number_ok = isinstance(item, (bool, int)) and not isinstance(item, float)
        if not isinstance(key, str) or not PAYLOAD_KEY.fullmatch(key) or not (text_ok or number_ok):
            raise NotificationError("payload is invalid", code="MALFORMED_REQUEST", status=400)
    if len(json.dumps(value, separators=(",", ":")).encode()) > MAX_PAYLOAD_BYTES:
        raise NotificationError("payload is too large", code="MALFORMED_REQUEST", status=400)
    return value
