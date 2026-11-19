"""Notification email through the Outgoing-mail relay (SPEC DEP-13, issue #224). No network."""

import smtplib
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from cya.notifications import delivery, relay, senders
from cya.notifications.commands import configure_mail_relay
from cya.notifications.models import NotificationDeliveryEvent
from cya.notifications.tests.support import NotificationCase
from cya.notifications.tests.test_mail_relay import KEY, SECRET, STUB, StubTransport, settings_for
from cya.notifications.tests.test_relay_transport import FakeSMTP


@override_settings(
    CYA_MAIL_RELAY_KEY=KEY, CYA_MAIL_RELAY_TRANSPORT=STUB, CYA_NOTIFICATION_SENDER=""
)
class RelayDeliveryTests(NotificationCase):
    def setUp(self):
        super().setUp()
        StubTransport.sent, StubTransport.fail = [], None
        self.source, _ = self.emit(user_ids=[str(self.crew.pk)])
        self.org_id = str(self.org.pk)

    def configure(self, **extra):
        configure_mail_relay(**settings_for(**extra), **self.context())

    def run_command(self):
        out = StringIO()
        call_command("deliver_notifications", stdout=out)
        return out.getvalue()

    def outcomes(self):
        return list(
            NotificationDeliveryEvent.objects.order_by("attempt_number").values_list(
                "outcome", "error_code"
            )
        )

    def test_email_goes_through_the_organizations_relay(self):
        self.configure()
        output = self.run_command()
        self.assertIn("sent=1", output)
        [sent] = StubTransport.sent
        self.assertEqual(sent["host"], "smtp-relay.example.test")
        self.assertEqual(sent["password"], SECRET)
        self.assertEqual(sent["to"], [self.crew.email])
        self.assertEqual(sent["subject"], senders.SUBJECT)
        self.assertIn("X-CYA-Reference", sent["headers"])
        self.assertEqual(self.outcomes(), [("SENT", None)])

    def test_ip_authentication_sends_without_a_password(self):
        self.configure(auth_mode="IP", username="", password="")
        self.run_command()
        self.assertEqual(StubTransport.sent[0]["password"], "")

    def test_without_a_relay_email_stays_queued_and_the_job_succeeds(self):
        output = self.run_command()
        self.assertIn("no outgoing-mail relay configured; email stays queued", output)
        self.assertEqual(StubTransport.sent, [])
        self.assertFalse(NotificationDeliveryEvent.objects.exists())

    def test_relay_failures_map_to_retry_or_give_up(self):
        self.configure()
        expected = {
            "CONNECT_FAILED": "TRANSIENT",
            "TIMEOUT": "TRANSIENT",
            "TLS_FAILED": "UNAVAILABLE",
            "AUTH_FAILED": "UNAVAILABLE",
            "KEY_MISSING": "UNAVAILABLE",
            "REJECTED": "REJECTED",
            "DEFERRED": "TRANSIENT",
            "UNKNOWN": "UNKNOWN",
        }
        relay_sender = senders.sender_for(self.org_id)
        for code, delivery_code in expected.items():
            StubTransport.fail = code
            with self.subTest(code=code), self.assertRaises(senders.SenderError) as caught:
                relay_sender.send(senders.OutgoingEmail("a@example.test", "s", "b", "r"))
            self.assertEqual(caught.exception.code, delivery_code)
        # A refused recipient is permanent: one attempt, given up.
        StubTransport.fail = "REJECTED"
        delivery.deliver_pending(self.org_id, relay_sender, now=timezone.now(), base_url="")
        self.assertEqual(self.outcomes(), [("GAVE_UP", "REJECTED")])

    def test_a_temporary_refusal_is_retried_not_given_up(self):
        # Codex review of PR #225: an SMTP 4xx reply must not give up on the first attempt.
        self.configure()
        StubTransport.fail = "DEFERRED"
        self.run_command()
        [(outcome, code)] = self.outcomes()
        self.assertEqual(code, "TRANSIENT")
        self.assertNotEqual(outcome, "GAVE_UP")

    @override_settings(CYA_MAIL_RELAY_KEY="")
    def test_a_missing_key_is_retried_not_dropped(self):
        with override_settings(CYA_MAIL_RELAY_KEY=KEY):
            self.configure()
        self.run_command()
        self.assertEqual(StubTransport.sent, [])
        [(outcome, code)] = self.outcomes()
        self.assertEqual(code, "UNAVAILABLE")
        self.assertNotEqual(outcome, "GAVE_UP")

    @override_settings(CYA_NOTIFICATION_SENDER="cya.testing.notifications.StubSender")
    def test_the_sender_setting_still_overrides_the_relay(self):
        self.configure()
        self.assertNotIsInstance(senders.sender_for(self.org_id), senders.RelaySender)


@override_settings(
    CYA_MAIL_RELAY_KEY=KEY,
    CYA_MAIL_RELAY_TRANSPORT="cya.notifications.relay.SmtpTransport",
    CYA_NOTIFICATION_SENDER="",
)
class SmtpReplyDeliveryTests(NotificationCase):
    """Codex review of PR #225: the SMTP reply code decides retry or give up, end to end."""

    def setUp(self):
        super().setUp()
        self.emit(user_ids=[str(self.crew.pk)])
        configure_mail_relay(**settings_for(), **self.context())

    def deliver(self, error):
        FakeSMTP.error, FakeSMTP.refused = error, {}
        with mock.patch.object(relay.smtplib, "SMTP", FakeSMTP):
            call_command("deliver_notifications", stdout=StringIO())
        return list(NotificationDeliveryEvent.objects.values_list("outcome", "error_code"))

    def test_a_4xx_refusal_is_retried(self):
        refused = smtplib.SMTPRecipientsRefused({self.crew.email: (451, b"try again later")})
        [(outcome, code)] = self.deliver(refused)
        self.assertEqual(code, "TRANSIENT")
        self.assertNotEqual(outcome, "GAVE_UP")

    def test_an_unrecognized_4xx_reply_is_retried(self):
        # Codex review of PR #225: a 4xx reply of any kind is retried.
        [(outcome, code)] = self.deliver(smtplib.SMTPResponseException(421, b"closing"))
        self.assertEqual(code, "TRANSIENT")
        self.assertNotEqual(outcome, "GAVE_UP")

    def test_an_unrecognized_5xx_reply_is_given_up(self):
        # Codex review of PR #225: a 5xx reply of any kind is permanent.
        self.assertEqual(
            self.deliver(smtplib.SMTPResponseException(502, b"not implemented")),
            [("GAVE_UP", "REJECTED")],
        )

    def test_a_5xx_refusal_is_given_up(self):
        refused = smtplib.SMTPRecipientsRefused({self.crew.email: (550, b"no such user")})
        self.assertEqual(self.deliver(refused), [("GAVE_UP", "REJECTED")])
