"""Notification email through the Outgoing-mail relay (SPEC DEP-13, issue #224). No network."""

from io import StringIO

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from cya.notifications import delivery, senders
from cya.notifications.commands import configure_mail_relay
from cya.notifications.models import NotificationDeliveryEvent
from cya.notifications.tests.support import NotificationCase
from cya.notifications.tests.test_mail_relay import KEY, SECRET, STUB, StubTransport, settings_for


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
