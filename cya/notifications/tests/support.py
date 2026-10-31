"""Fixtures for notification tests: a throwaway command that enqueues, like a real feature would."""

from uuid import uuid4

from cya.checklists.tests.support import ChecklistCase, ids
from cya.core.audit_commands import Plan
from cya.notifications import commands as notification_commands
from cya.notifications import hooks
from cya.notifications.enqueue import enqueue
from cya.notifications.models import NotificationOutbox


class NotificationCase(ChecklistCase):
    def tearDown(self):
        hooks.clear_dispatchers()
        super().tearDown()

    def emit(
        self,
        *,
        actor=None,
        roles=(),
        user_ids=(),
        event_type="status.changed",
        source_id=None,
        fail_after=False,
        **extra,
    ):
        """Run a command whose Plan.apply enqueues, as issue 14 (https://github.com/JustApu/CheckYourAmbulance/issues/14)'s event commands will."""
        source_id = source_id or str(uuid4())
        outcome = {}
        actor = actor or self.supervisor

        def build(current):
            def apply(receipt, current_actor):
                outcome["rows"] = enqueue(
                    receipt,
                    source_event_type="TEST_EVENT",
                    source_event_id=source_id,
                    event_type=event_type,
                    template_key="test.template",
                    payload={"title": "Synthetic update", "vehicle": "Synthetic 1"},
                    deep_link="/status/vehicles/synthetic/",
                    roles=roles,
                    user_ids=user_ids,
                    **extra,
                )
                if fail_after:
                    raise RuntimeError("synthetic failure after enqueue")

            return Plan(response_body={"code": "SAVED"}, apply=apply)

        notification_commands._execute(
            command_type="NOTIFICATION_TEST_EMIT",
            request={"source": source_id},
            build=build,
            **self.context(actor),
        )
        return source_id, outcome.get("rows", [])

    def outbox(self, **filters):
        return NotificationOutbox.objects.filter(**filters).order_by("recorded_at", "id")

    def ids(self):
        return ids()
