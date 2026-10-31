"""In-app inbox: per-user scope, mark read, bounds."""

from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.core.audit_commands import CommandDenied
from cya.notifications import commands, inbox
from cya.notifications.models import NotificationRead
from cya.notifications.rules import NotificationError
from cya.notifications.tests.support import NotificationCase


class InboxTests(NotificationCase):
    def mark(self, actor, outbox_id):
        return commands.mark_read(outbox_id=str(outbox_id), **self.context(actor))

    def test_each_user_sees_only_their_own_in_app_items_newest_first(self):
        self.emit(user_ids=[str(self.crew.pk)])
        self.emit(user_ids=[str(self.crew.pk), str(self.supervisor.pk)])
        mine = inbox.inbox(self.crew)["items"]
        theirs = inbox.inbox(self.supervisor)["items"]
        self.assertEqual((len(mine), len(theirs)), (2, 1))
        self.assertGreaterEqual(mine[0]["recorded_at"], mine[1]["recorded_at"])
        self.assertEqual(mine[0]["deep_link"], "/status/vehicles/synthetic/")
        self.assertEqual(inbox.inbox(self.admin)["items"], [])

    def test_mark_read_is_own_only_and_repeatable(self):
        self.emit(user_ids=[str(self.crew.pk)])
        item = inbox.inbox(self.crew)["items"][0]
        for actor in (self.supervisor, self.admin):
            with self.subTest(actor=actor.pk), self.assertRaises(NotificationError) as caught:
                self.mark(actor, item["id"])
            self.assertEqual(caught.exception.status, 404)
        self.assertEqual(inbox.unread_count(self.crew), 1)
        self.assertEqual(self.mark(self.crew, item["id"]).body["code"], "SAVED")
        self.assertEqual(self.mark(self.crew, item["id"]).body["code"], "ALREADY_READ")
        self.assertEqual(NotificationRead.objects.count(), 1)
        self.assertEqual(inbox.unread_count(self.crew), 0)
        self.assertTrue(inbox.inbox(self.crew)["items"][0]["read"])
        self.assertEqual(inbox.inbox(self.crew, unread_only=True)["items"], [])

    def test_email_rows_and_unknown_ids_cannot_be_marked(self):
        _, rows = self.emit(user_ids=[str(self.crew.pk)])
        email = next(r for r in rows if r.channel == "EMAIL")
        for value in (email.pk, uuid4()):
            with self.assertRaises(NotificationError):
                self.mark(self.crew, value)
        with self.assertRaises(ValueError):
            self.mark(self.crew, "not-an-id")

    def test_deactivated_user_cannot_mark_read(self):
        self.emit(user_ids=[str(self.crew.pk)])
        item = inbox.inbox(self.crew)["items"][0]
        self.crew.active = False
        self.crew.save()
        with self.assertRaises(CommandDenied):
            self.mark(self.crew, item["id"])

    def test_limit_validation_and_truncation(self):
        for _ in range(3):
            self.emit(user_ids=[str(self.crew.pk)])
        page = inbox.inbox(self.crew, limit=2)
        self.assertEqual((len(page["items"]), page["truncated"]), (2, True))
        for bad in (0, 201, True, "5"):
            with self.subTest(bad), self.assertRaises(ValueError):
                inbox.inbox(self.crew, limit=bad)

    def test_read_marks_are_append_only(self):
        self.emit(user_ids=[str(self.crew.pk)])
        self.mark(self.crew, inbox.inbox(self.crew)["items"][0]["id"])
        with self.assertRaises(DatabaseError), transaction.atomic():
            NotificationRead.objects.all().delete()
