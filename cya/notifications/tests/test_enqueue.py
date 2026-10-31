"""Enqueue: atomic with the causing command, de-duplicated, mandatory over optional."""

from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection, transaction

from cya.accounts.models import User
from cya.core.audit_commands import CommandDenied
from cya.notifications import commands, enqueue, hooks
from cya.notifications.models import NotificationOutbox
from cya.notifications.rules import NotificationError
from cya.notifications.tests.support import NotificationCase


class EnqueueTests(NotificationCase):
    def test_rows_commit_with_the_command_one_per_recipient_and_channel(self):
        source, rows = self.emit(user_ids=[str(self.crew.pk)])
        found = self.outbox(source_event_id=source)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {(r.channel, r.recipient_user_id) for r in found},
            {
                ("IN_APP", self.crew.pk),
                ("EMAIL", self.crew.pk),
            },
        )
        self.assertTrue(all(r.command.command_type == "NOTIFICATION_TEST_EMIT" for r in found))

    def test_rollback_with_the_command_leaves_no_intent(self):
        with self.assertRaises(RuntimeError):
            self.emit(user_ids=[str(self.crew.pk)], fail_after=True)
        self.assertFalse(NotificationOutbox.objects.exists())

    def test_role_and_user_recipients_are_deduplicated(self):
        source, rows = self.emit(
            roles=["SUPERVISOR", "ADMINISTRATOR"],
            user_ids=[str(self.supervisor.pk), str(self.crew.pk)],
        )
        users = sorted(r.recipient_user_id for r in rows if r.channel == "IN_APP")
        self.assertEqual(users, sorted({self.supervisor.pk, self.admin.pk, self.crew.pk}))
        self.assertEqual(len(rows), 6)

    def test_every_user_is_considered_however_large_the_organization(self):
        extra = [self.make_user(f"bulk-{n}") for n in range(12)]
        with mock.patch.object(enqueue, "MAX_RECIPIENTS", 2):
            for user in extra:  # named users anywhere in the organization are found
                _, rows = self.emit(user_ids=[str(user.pk)])
                self.assertEqual({r.recipient_user_id for r in rows}, {user.pk})
            for user in extra:
                self.grant(user, "SUPERVISOR")
            with self.assertRaises(NotificationError) as raised:
                self.emit(roles=["SUPERVISOR"])  # overflow is explicit, never a silent omission
            self.assertEqual(raised.exception.code, "MALFORMED_REQUEST")
        _, rows = self.emit(roles=["SUPERVISOR"])
        holders = {r.recipient_user_id for r in rows if r.channel == "IN_APP"}
        self.assertTrue({u.pk for u in extra} <= holders)

    def test_actor_can_be_excluded_and_inactive_users_are_skipped(self):
        self.crew.active = False
        self.crew.save()
        _, rows = self.emit(roles=["CREW", "SUPERVISOR"], exclude_user_ids=[self.supervisor.pk])
        recipients = {r.recipient_user_id for r in rows}
        self.assertNotIn(self.crew.pk, recipients)
        self.assertNotIn(self.supervisor.pk, recipients)

    def test_the_unique_key_is_the_backstop_for_a_repeated_source_event(self):
        source, _ = self.emit(user_ids=[str(self.crew.pk)])
        with self.assertRaises(DatabaseError):
            self.emit(user_ids=[str(self.crew.pk)], source_id=source)
        self.assertEqual(self.outbox(source_event_id=source).count(), 2)

    def test_invalid_input_writes_nothing(self):
        for label, kwargs in {
            "event": dict(event_type="Bad Event"),
            "unknown role": dict(roles=["PILOT"]),
            "unknown user": dict(user_ids=[str(uuid4())]),
            "channel": dict(channels=("SMS",)),
        }.items():
            with self.subTest(label), self.assertRaises(NotificationError):
                self.emit(**{"user_ids": [str(self.crew.pk)], **kwargs})
        self.assertFalse(NotificationOutbox.objects.exists())

    def test_enqueue_outside_a_transaction_is_refused(self):
        self.assertFalse(connection.in_atomic_block)
        with self.assertRaises(RuntimeError):
            enqueue.enqueue(
                None,
                source_event_type="TEST_EVENT",
                source_event_id=str(uuid4()),
                event_type="status.changed",
                template_key="test.template",
                payload={},
                deep_link="/x",
            )

    def test_foreign_organization_users_are_not_recipients(self):
        from cya.accounts.models import Organization

        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        stranger = User.objects.create(
            organization=other,
            idp_issuer="https://accounts.google.com",
            idp_subject="stranger",
            email="stranger@example.test",
            display_name="User stranger",
        )
        with self.assertRaises(NotificationError):
            self.emit(user_ids=[str(stranger.pk)])
        self.assertFalse(NotificationOutbox.objects.exists())


class PreferenceTests(NotificationCase):
    def pref(self, actor, enabled, channel="EMAIL", event="status.changed"):
        return commands.set_preference(
            event_type=event, channel=channel, enabled=enabled, **self.context(actor)
        )

    def policy(self, mandatory, channel="EMAIL", event="status.changed", actor=None):
        return commands.set_policy(
            event_type=event,
            channel=channel,
            mandatory=mandatory,
            **self.context(actor or self.admin),
        )

    def test_optional_preference_suppresses_only_that_users_channel(self):
        self.pref(self.crew, False)
        self.grant(self.supervisor, "SUPERVISOR")
        _, rows = self.emit(user_ids=[str(self.crew.pk), str(self.supervisor.pk)])
        pairs = {(r.recipient_user_id, r.channel) for r in rows}
        self.assertNotIn((self.crew.pk, "EMAIL"), pairs)
        self.assertIn((self.crew.pk, "IN_APP"), pairs)
        self.assertIn((self.supervisor.pk, "EMAIL"), pairs)

    def test_preferences_are_isolated_between_users_and_events(self):
        self.pref(self.crew, False)
        self.assertEqual(self.pref(self.supervisor, True).body["enabled"], True)
        from cya.notifications.models import NotificationPref

        self.assertEqual(NotificationPref.objects.filter(user=self.supervisor).count(), 1)
        self.assertEqual(NotificationPref.objects.filter(user=self.crew, enabled=False).count(), 1)
        _, rows = self.emit(user_ids=[str(self.crew.pk)], event_type="other.event")
        self.assertIn("EMAIL", {r.channel for r in rows})

    def test_mandatory_policy_cannot_be_overridden_by_a_preference(self):
        self.policy(True)
        with self.assertRaises(NotificationError) as caught:
            self.pref(self.crew, False)
        self.assertEqual(caught.exception.code, "MANDATORY_EVENT")
        self.pref(self.crew, True)

    def test_policy_set_after_a_preference_still_wins_at_enqueue(self):
        self.pref(self.crew, False)
        self.policy(True)
        _, rows = self.emit(user_ids=[str(self.crew.pk)])
        email = [r for r in rows if r.channel == "EMAIL"]
        self.assertEqual(len(email), 1)
        self.assertTrue(email[0].mandatory)

    def test_policy_is_administrator_only_and_audited(self):
        for actor in (self.crew, self.supervisor):
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.policy(True, actor=actor)
        self.policy(True)
        self.policy(False)
        from cya.core.models import AuditEvent

        audits = AuditEvent.objects.filter(action="NOTIFICATION_POLICY_SET")
        self.assertEqual(audits.filter(outcome="SUCCEEDED").count(), 2)
        self.assertEqual(audits.filter(outcome="DENIED").count(), 2)

    def test_malformed_policy_and_preference_input(self):
        for call in (
            lambda: self.policy("yes"),
            lambda: self.policy(True, channel="SMS"),
            lambda: self.policy(True, event="Bad Event"),
            lambda: self.pref(self.crew, "no"),
            lambda: self.pref(self.crew, True, event=""),
        ):
            with self.assertRaises(NotificationError):
                call()

    def test_dispatcher_runs_after_commit_and_never_fails_the_command(self):
        seen = []
        hooks.register_dispatcher(seen.append)

        def boom(_):
            raise RuntimeError("synthetic")

        hooks.register_dispatcher(boom)
        self.emit(user_ids=[str(self.crew.pk)])
        self.assertEqual(seen, [str(self.org.pk)])
        seen.clear()
        with self.assertRaises(RuntimeError):
            self.emit(user_ids=[str(self.crew.pk)], fail_after=True)
        self.assertEqual(seen, [])


class DatabaseGuardTests(NotificationCase):
    def test_outbox_is_append_only_and_receipt_attributed(self):
        _, rows = self.emit(user_ids=[str(self.crew.pk)])
        row = NotificationOutbox.objects.get(pk=rows[0].pk)
        for action in (
            lambda: row.delete(),
            lambda: NotificationOutbox.objects.filter(pk=row.pk).update(mandatory=True),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                action()
        forged = NotificationOutbox(
            organization_id=self.org.pk,
            command=row.command,
            recorded_at=row.recorded_at.replace(year=2000),
            source_event_type="TEST_EVENT",
            source_event_id=str(uuid4()),
            event_type="status.changed",
            channel="IN_APP",
            recipient_user=self.crew,
            template_key="test.template",
            deep_link="/x",
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            forged.save(force_insert=True)
