"""Email delivery through the injected sender: retry, give-up, ambiguity, collision, no network."""

import socket
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from cya.core.models import AuditEvent, CommandReceipt
from cya.notifications import delivery, senders
from cya.notifications.models import NotificationDeliveryEvent
from cya.notifications.senders import SenderError
from cya.notifications.tests.support import NotificationCase
from cya.testing.notifications import StubSender, transient

LATER = timedelta(hours=2)


class DeliveryTests(NotificationCase):
    def setUp(self):
        super().setUp()
        self.source, _ = self.emit(user_ids=[str(self.crew.pk)])
        self.org_id = str(self.org.pk)

    def run_job(self, sender, hours=0, **kw):
        return delivery.deliver_pending(
            self.org_id, sender, now=timezone.now() + timedelta(hours=hours), base_url="", **kw
        )

    def attempts(self):
        return list(NotificationDeliveryEvent.objects.order_by("attempt_number", "recorded_at"))

    def test_success_is_recorded_once_and_not_repeated(self):
        sender = StubSender()
        report = self.run_job(sender)
        self.assertEqual((report.sent, report.errors, report.collisions), (1, 0, 0))
        self.assertEqual(
            [(a.attempt_number, a.outcome, a.error_code) for a in self.attempts()],
            [(1, "SENT", None)],
        )
        self.assertEqual(self.run_job(sender, hours=3).sent, 0)
        self.assertEqual(len(sender.sent), 1)

    def test_in_app_rows_are_never_emailed(self):
        sender = StubSender()
        self.run_job(sender)
        self.assertEqual({m.to for m in sender.sent}, {self.crew.email})
        self.assertEqual(NotificationDeliveryEvent.objects.count(), 1)

    def test_message_has_a_deep_link_and_no_payload_text(self):
        sender = StubSender()
        delivery.deliver_pending(
            self.org_id, sender, base_url="https://cya.example", now=timezone.now()
        )
        message = sender.sent[0]
        self.assertIn("https://cya.example/status/vehicles/synthetic/", message.body)
        self.assertEqual(message.subject, senders.SUBJECT)
        for text in ("Synthetic update", "Synthetic 1"):
            self.assertNotIn(text, message.subject + message.body)

    def test_transient_failure_retries_with_backoff_then_succeeds(self):
        sender = StubSender([transient(), transient()])
        self.assertEqual(self.run_job(sender).retrying, 1)
        self.assertEqual(
            self.run_job(sender).retrying + self.run_job(sender).sent, 0
        )  # not due yet
        self.assertEqual(self.run_job(sender, hours=1).retrying, 1)
        self.assertEqual(self.run_job(sender, hours=2).sent, 1)
        rows = self.attempts()
        self.assertEqual(
            [(a.attempt_number, a.outcome, a.error_code) for a in rows],
            [(1, "RETRY", "TRANSIENT"), (2, "RETRY", "TRANSIENT"), (3, "SENT", None)],
        )
        self.assertEqual(len(sender.sent), 1)

    def test_attempts_are_bounded_and_failures_are_visible_to_administrators(self):
        sender = StubSender([transient()] * 5)
        for hour in range(0, 12, 2):
            self.run_job(sender, hours=hour * 24, max_attempts=3)
        self.assertEqual([a.outcome for a in self.attempts()], ["RETRY", "RETRY", "GAVE_UP"])
        self.assertEqual(sender.calls, 3)
        (failure,) = delivery.failures(self.org_id)
        self.assertEqual(
            (failure["attempts"], failure["last_outcome"], failure["last_error_code"]),
            (3, "GAVE_UP", "TRANSIENT"),
        )

    def test_permanent_rejection_gives_up_at_once(self):
        report = self.run_job(StubSender([SenderError("REJECTED")]))
        self.assertEqual(report.gave_up, 1)
        self.assertEqual(self.run_job(StubSender(), hours=48).gave_up, 0)

    def test_unexpected_exception_text_is_never_stored(self):
        secret = "smtp password hunter2 for crew@example.test"
        self.run_job(StubSender([RuntimeError(secret)]))
        (attempt,) = self.attempts()
        self.assertEqual(attempt.error_code, "UNKNOWN")
        stored = repr(list(CommandReceipt.objects.values())) + repr(
            list(AuditEvent.objects.values())
        )
        self.assertNotIn("hunter2", stored)

    def test_inactive_recipient_is_not_emailed(self):
        self.crew.active = False
        self.crew.save()
        sender = StubSender()
        self.assertEqual(self.run_job(sender).gave_up, 1)
        self.assertEqual(sender.calls, 0)
        self.assertEqual(self.attempts()[0].error_code, "INVALID_ADDRESS")

    def test_ambiguous_crash_after_acceptance_may_resend_but_never_loses_intent(self):
        sender = StubSender()
        real = delivery._record
        with mock.patch.object(delivery, "_record", side_effect=RuntimeError("crash")):
            report = self.run_job(sender)
        self.assertEqual((report.errors, len(sender.sent), len(self.attempts())), (1, 1, 0))
        report = self.run_job(sender)
        self.assertEqual(report.sent, 1)
        self.assertEqual(len(sender.sent), 2)  # at-least-once: the message is sent again
        self.assertEqual([(a.attempt_number, a.outcome) for a in self.attempts()], [(1, "SENT")])
        self.assertIs(delivery._record, real)

    def test_colliding_job_fails_visibly_and_does_not_overwrite(self):
        first = self.run_job(StubSender([transient()]))
        self.assertEqual(first.retrying, 1)
        outbox = NotificationDeliveryEvent.objects.get().outbox
        # A second worker working from stale history repeats attempt 1 with a different outcome.
        stale = mock.patch.object(delivery, "_attempts", return_value={})
        with stale, self.assertLogs("cya.notifications.delivery", "ERROR"):
            report = self.run_job(StubSender(), hours=3)
        self.assertEqual((report.collisions, report.sent), (1, 0))
        self.assertEqual([(a.attempt_number, a.outcome) for a in self.attempts()], [(1, "RETRY")])
        self.assertEqual(outbox.pk, self.attempts()[0].outbox_id)

    def test_same_outcome_replay_is_one_row(self):
        with mock.patch.object(delivery, "_attempts", return_value={}):
            self.run_job(StubSender())
            report = self.run_job(StubSender(), hours=3)
        self.assertEqual(NotificationDeliveryEvent.objects.count(), 1)
        self.assertEqual(report.collisions, 0)

    def test_limit_bounds_one_pass(self):
        for _ in range(3):
            self.emit(user_ids=[str(self.crew.pk)])
        sender = StubSender()
        report = self.run_job(sender, limit=2)
        self.assertEqual((report.sent, sender.calls), (2, 2))

    def test_rows_in_backoff_cannot_starve_newer_messages(self):
        for _ in range(6):
            self.emit(user_ids=[str(self.crew.pk)])
        self.assertEqual(self.run_job(StubSender([transient()] * 7), limit=10).retrying, 7)
        self.emit(user_ids=[str(self.crew.pk)])
        sender = StubSender()
        report = self.run_job(sender, limit=1)  # scan page of 5 is smaller than the backlog
        self.assertEqual((report.sent, sender.calls), (1, 1))

    def test_tests_never_touch_the_network(self):
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            self.run_job(StubSender())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_smtp_adapter_uses_the_configured_backend_and_maps_failures(self):
        # Django resolves the host name for Message-ID; the network guard (rightly) blocks that.
        with mock.patch("django.core.mail.message.DNS_NAME", "cya.example.test"):
            report = self.run_job(senders.SmtpSender())
        self.assertEqual(report.sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.crew.email])
        with mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("down")):
            self.emit(user_ids=[str(self.crew.pk)])
            self.assertEqual(self.run_job(senders.SmtpSender(), hours=1).retrying, 1)
        self.assertEqual(self.attempts()[-1].error_code, "UNAVAILABLE")

    def test_unconfigured_sender_is_none(self):
        self.assertIsNone(senders.configured_sender())
