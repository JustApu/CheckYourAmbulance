"""SMTP replies as relay send codes (issue #224; Codex review of PR #225). No network."""

import smtplib
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from cya.notifications import relay

RELAY = SimpleNamespace(
    sender="cya@example.test",
    host="smtp-relay.example.test",
    port=587,
    tls_mode="STARTTLS",
    auth_mode="IP",
    username="",
)


class FakeSMTP:
    error = None
    refused = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, username, password):
        pass

    def send_message(self, message):
        if FakeSMTP.error is not None:
            raise FakeSMTP.error
        return FakeSMTP.refused


class SmtpReplyCodeTests(SimpleTestCase):
    def code_for(self, error=None, refused=None):
        FakeSMTP.error, FakeSMTP.refused = error, refused or {}
        with mock.patch.object(relay.smtplib, "SMTP", FakeSMTP):
            try:
                relay.SmtpTransport().send(RELAY, "", ["a@example.test"], "s", "b")
            except relay.SendFailure as failure:
                return failure.code
        return "SENT"

    def test_4xx_refusals_are_deferred_and_5xx_refusals_rejected(self):
        cases = [
            (smtplib.SMTPRecipientsRefused({"a@example.test": (450, b"busy")}), "DEFERRED"),
            (smtplib.SMTPRecipientsRefused({"a@example.test": (550, b"no user")}), "REJECTED"),
            (smtplib.SMTPSenderRefused(451, b"try later", "cya@example.test"), "DEFERRED"),
            (smtplib.SMTPSenderRefused(553, b"not allowed", "cya@example.test"), "REJECTED"),
            (smtplib.SMTPDataError(452, b"insufficient storage"), "DEFERRED"),
            (smtplib.SMTPDataError(554, b"rejected"), "REJECTED"),
            (smtplib.SMTPResponseException(421, b"closing"), "DEFERRED"),
            (smtplib.SMTPConnectError(554, b"no service for you"), "REJECTED"),
            (smtplib.SMTPConnectError(421, b"too busy"), "DEFERRED"),
            (smtplib.SMTPHeloError(550, b"not allowed"), "REJECTED"),
            (smtplib.SMTPResponseException(454, b"TLS not available now"), "DEFERRED"),
            (smtplib.SMTPAuthenticationError(535, b"bad credentials"), "AUTH_FAILED"),
            (smtplib.SMTPNotSupportedError("no STARTTLS"), "TLS_FAILED"),
            (smtplib.SMTPResponseException(502, b"unknown"), "REJECTED"),
            (smtplib.SMTPResponseException(-1, b"broken"), "CONNECT_FAILED"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, code=expected):
                self.assertEqual(self.code_for(error=error), expected)

    def test_a_partial_refusal_is_rejected_unless_every_reply_is_temporary(self):
        self.assertEqual(self.code_for(refused={"a@example.test": (451, b"later")}), "DEFERRED")
        mixed = {"a@example.test": (451, b"later"), "b@example.test": (550, b"no")}
        self.assertEqual(self.code_for(refused=mixed), "REJECTED")

    def test_connection_problems_keep_their_codes(self):
        self.assertEqual(self.code_for(error=smtplib.SMTPServerDisconnected()), "CONNECT_FAILED")
        self.assertEqual(self.code_for(error=TimeoutError()), "TIMEOUT")
        self.assertEqual(self.code_for(), "SENT")
