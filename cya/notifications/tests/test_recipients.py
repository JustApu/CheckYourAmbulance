"""The documented default recipients and notify() (issue 14; SPEC 13 NOT-1, NOT-3, NOT-5)."""

import json
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from cya.core.audit_commands import Plan
from cya.notifications import commands as notification_commands
from cya.notifications import recipients
from cya.notifications.models import RECIPIENT_KINDS, NotificationOutbox
from cya.notifications.rules import NotificationError
from cya.notifications.tests.support import NotificationCase


class DefaultsTests(NotificationCase):
    def test_the_shipped_file_is_valid_and_matches_the_owner_decisions(self):
        defaults = recipients.defaults()
        expected = {
            "deficiency.assigned": ["ASSIGNEE"],
            "deficiency.reopened_verification_added": ["ASSIGNEE"],
            "deficiency.reopened_verification_invalidated": ["ASSIGNEE", "SUPERVISORS"],
            "deficiency.reopened_by_administrator": ["ASSIGNEE", "REPORTER"],
            "deficiency.reopened_reported_in_error": ["SUPERVISORS"],
            "deficiency.correction_notice": ["SUPERVISORS"],
            "deficiency.extension_requested": ["SUPERVISORS"],
            "deficiency.extension_decided": ["REQUESTER", "ASSIGNEE"],
            "deficiency.deadline_overdue": ["ASSIGNEE", "SUPERVISORS"],
            "deficiency.administrator_closed": ["ASSIGNEE", "REPORTER"],
            "check.supervisor_completion_voided": ["SUPERVISORS"],
            "vehicle.status_changed": ["SUPERVISORS", "ADMINISTRATORS"],
            "vehicle.status_change_requested": ["SUPERVISORS"],
            "vehicle.status_request_decided": ["REQUESTER"],
            "session.reopened": ["PARTICIPANTS"],
        }
        self.assertEqual({event: list(d) for event, (_a, d) in defaults.items()}, expected)
        for available, _default in defaults.values():
            self.assertLessEqual(set(available), set(RECIPIENT_KINDS))

    def test_a_malformed_defaults_file_is_refused(self):
        good = json.loads(recipients.DEFAULTS_FILE.read_text(encoding="utf-8"))
        bad_files = [
            {**good, "schema_version": 2},
            {"schema_version": 1, "events": []},
            {"schema_version": 1, "events": {"BAD": {"available": [], "default": []}}},
            {"schema_version": 1, "events": {"a.b": {"available": ["X"], "default": []}}},
            {
                "schema_version": 1,
                "events": {"a.b": {"available": ["ASSIGNEE"], "default": ["REPORTER"]}},
            },
        ]
        for data in bad_files:
            recipients.defaults.cache_clear()
            try:
                with (
                    self.subTest(data=data),
                    mock.patch.object(
                        type(recipients.DEFAULTS_FILE), "read_text", return_value=json.dumps(data)
                    ),
                    self.assertRaises(ValueError),
                ):
                    recipients.defaults()
            finally:
                recipients.defaults.cache_clear()


class NotifyTests(NotificationCase):
    def run_notify(self, **people):
        outcome = {}

        def build(current):
            def apply(receipt, current_actor):
                outcome["rows"] = recipients.notify(
                    receipt,
                    event_type="vehicle.status_request_decided",
                    source_event_type="status_request",
                    source_event_id=str(uuid4()),
                    deep_link="/status/",
                    payload={"vehicle_id": "synthetic"},
                    actor_user_id=str(self.supervisor.pk),
                    **people,
                )

            return Plan(response_body={"code": "SAVED"}, apply=apply)

        notification_commands._execute(
            command_type="NOTIFICATION_TEST_EMIT",
            request={"people": sorted(people)},
            build=build,
            **self.context(self.supervisor),
        )
        return outcome["rows"]

    def test_named_people_are_resolved_skipping_the_actor_and_nobody_writes_nothing(self):
        self.assertEqual(self.run_notify(), [])
        self.assertEqual(self.run_notify(requester_id=str(self.supervisor.pk)), [])
        rows = self.run_notify(requester_id=str(self.crew.pk))
        self.assertEqual(
            {(r.recipient_user_id, r.channel) for r in rows},
            {
                (self.crew.pk, "IN_APP"),
                (self.crew.pk, "EMAIL"),
            },
        )
        self.assertEqual(NotificationOutbox.objects.count(), 2)

    def test_notify_outside_a_command_transaction_is_refused(self):
        receipt = SimpleNamespace(organization_id=str(self.org.pk))
        with self.assertRaises(RuntimeError):
            recipients.notify(
                receipt,
                event_type="vehicle.status_request_decided",
                source_event_type="status_request",
                source_event_id=str(uuid4()),
                deep_link="/status/",
                payload={},
                requester_id=str(self.crew.pk),
            )
        with self.assertRaises(NotificationError):
            recipients.notify(
                receipt,
                event_type="vehicle.unknown",
                source_event_type="status_request",
                source_event_id=str(uuid4()),
                deep_link="/status/",
                payload={},
            )
