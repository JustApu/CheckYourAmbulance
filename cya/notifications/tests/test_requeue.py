"""Sending given-up notification email again (SPEC 13 NOT-4; issue #224, Codex review of PR #225).

A 5xx relay reply gives up at once (RFC 5321). An administrator who fixes the relay sends the
given-up email again; delivery then counts attempts afresh. Real SmtpTransport, fake SMTP server.
"""

import smtplib
from datetime import timedelta
from unittest import mock

from django.db import DatabaseError, transaction
from django.test import override_settings
from django.utils import timezone

from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.notifications import delivery, relay, senders
from cya.notifications.commands import configure_mail_relay, requeue_email
from cya.notifications.models import NotificationDeliveryEvent, NotificationRequeue
from cya.notifications.tests.support import NotificationCase
from cya.notifications.tests.test_mail_relay import KEY, settings_for
from cya.notifications.tests.test_relay_transport import FakeSMTP

REFUSED = smtplib.SMTPConnectError(554, b"no service for this server")


@override_settings(
    CYA_MAIL_RELAY_KEY=KEY,
    CYA_MAIL_RELAY_TRANSPORT="cya.notifications.relay.SmtpTransport",
    CYA_NOTIFICATION_SENDER="",
)
class RequeueTests(NotificationCase):
    def setUp(self):
        super().setUp()
        self.org_id = str(self.org.pk)
        self.emit(user_ids=[str(self.crew.pk)])
        configure_mail_relay(**settings_for(), **self.context())
        self.later = timezone.now()

    def deliver(self, error=None, **options):
        self.later += timedelta(days=1)
        FakeSMTP.error, FakeSMTP.refused = error, {}
        with mock.patch.object(relay.smtplib, "SMTP", FakeSMTP):
            return delivery.deliver_pending(
                self.org_id, senders.sender_for(self.org_id), now=self.later, base_url="", **options
            )

    def history(self):
        return list(
            NotificationDeliveryEvent.objects.order_by("attempt_number").values_list(
                "attempt_number", "outcome", "error_code"
            )
        )

    def test_a_relay_that_refuses_this_server_gives_up_and_is_listed(self):
        self.assertEqual(self.deliver(REFUSED).gave_up, 1)
        self.assertEqual(self.history(), [(1, "GAVE_UP", "REJECTED")])
        [(row, last)] = delivery.given_up(self.org_id)
        self.assertEqual((row.recipient_user_id, last.error_code), (self.crew.pk, "REJECTED"))
        self.assertEqual(delivery.given_up_count(self.org_id), 1)
        self.assertEqual(self.deliver().sent, 0)  # given up stays given up

    def test_sending_again_after_the_relay_is_fixed_delivers_it(self):
        self.deliver(REFUSED)
        result = requeue_email(**self.context())
        self.assertEqual(result.body, {"code": "REQUEUED", "count": 1})
        self.assertEqual(delivery.given_up(self.org_id), [])
        [requeue] = NotificationRequeue.objects.all()
        self.assertEqual((requeue.after_attempt, requeue.actor_user_id), (1, self.admin.pk))
        self.assertTrue(
            AuditEvent.objects.filter(action="NOTIFICATION_REQUEUE", outcome="SUCCEEDED").exists()
        )
        self.assertEqual(self.deliver().sent, 1)
        self.assertEqual(self.history(), [(1, "GAVE_UP", "REJECTED"), (2, "SENT", None)])

    def test_attempts_count_afresh_after_sending_again(self):
        down = smtplib.SMTPServerDisconnected()
        self.deliver(down, max_attempts=2)
        self.deliver(down, max_attempts=2)
        self.assertEqual(self.history()[-1][1:], ("GAVE_UP", "TRANSIENT"))
        requeue_email(**self.context())
        self.assertEqual(self.deliver(down, max_attempts=2).retrying, 1)
        self.assertEqual(self.deliver(down, max_attempts=2).gave_up, 1)
        self.assertEqual(
            [outcome for _n, outcome, _code in self.history()],
            ["RETRY", "GAVE_UP", "RETRY", "GAVE_UP"],
        )
        # Given up again: listed again, and can be sent again after attempt 4.
        self.assertEqual(requeue_email(**self.context()).body["count"], 1)
        self.assertEqual(
            sorted(NotificationRequeue.objects.values_list("after_attempt", flat=True)), [2, 4]
        )

    def test_nothing_to_send_again_is_a_count_of_zero(self):
        self.assertEqual(requeue_email(**self.context()).body["count"], 0)
        self.assertFalse(NotificationRequeue.objects.exists())

    def test_only_an_administrator_may_send_again(self):
        self.deliver(REFUSED)
        for actor in (self.supervisor, self.crew):
            with self.subTest(role=actor.pk), self.assertRaises(CommandDenied):
                requeue_email(**self.context(actor))
        self.assertEqual(delivery.given_up_count(self.org_id), 1)

    def test_the_database_refuses_reopening_an_email_that_did_not_give_up(self):
        self.deliver(smtplib.SMTPServerDisconnected())  # RETRY, not given up
        context = self.context()
        requeue_email(**context)  # nothing to reopen; a real receipt to reuse
        row = CommandReceipt.objects.get(operation_id=context["operation_id"])
        outbox = NotificationDeliveryEvent.objects.get().outbox
        with self.assertRaises(DatabaseError), transaction.atomic():
            NotificationRequeue.objects.create(
                organization_id=self.org_id,
                command=row,
                outbox=outbox,
                after_attempt=1,
                actor_user=self.admin,
                recorded_at=row.recorded_at,
            )
