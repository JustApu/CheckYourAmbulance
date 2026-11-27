"""Rows pushed after a check (SPEC 8.3.0 §17.3, OQ-88; issue #278).

Each open session gets its item's rows exactly as its participants would read them; only a
participant's socket joins the session's group; the consumer relays only well-formed rows for
the session it joined; and the publisher sends the rows before the hint, which still goes out if
reading the rows fails.
"""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from channels.layers import InMemoryChannelLayer
from django.core.serializers.json import DjangoJSONEncoder
from django.test import SimpleTestCase, TestCase

from cya.attention import live_rows
from cya.attention.api import check_authors, checklist_row
from cya.attention.checklist import vehicle_checklist
from cya.attention.tests.support import AttentionCase
from cya.inspections.tests.test_pinned_rows import PinnedVersionMixin
from cya.live_updates.auth import _session_participant
from cya.live_updates.consumer import VehicleLiveConsumer
from cya.live_updates.publish import publish_vehicle_update


class RowMessageTests(PinnedVersionMixin, AttentionCase):
    def setUp(self):
        super().setUp()
        self.session = self.open_session(self.crew)
        self.org_id, self.vehicle_id = str(self.org.pk), str(self.vehicle.pk)
        self.item_id = str(self.items[0]["item_id"])

    def test_each_open_session_gets_the_rows_its_participants_would_read(self):
        event_id = str(uuid4())
        messages = live_rows.row_messages(self.org_id, self.vehicle_id, self.item_id, event_id)
        self.assertEqual(len(messages), 1)
        group, message = messages[0]
        self.assertEqual(group, f"session.{self.org_id}.{self.session.pk}")
        result = vehicle_checklist(
            self.org_id,
            self.vehicle_id,
            view="NORMAL",
            needs_attention_only=False,
            session=self.session,
            item_ids=[self.item_id],
        )
        authors = check_authors(self.org_id, result.rows)
        expected = json.loads(
            json.dumps(
                [checklist_row(r, authors.get(r.latest_check_id)) for r in result.rows],
                cls=DjangoJSONEncoder,
            )
        )
        self.assertTrue(expected)
        self.assertEqual(message["items"], expected, "exactly the API's one-row read")
        self.assertEqual(
            {
                k: message[k]
                for k in (
                    "type",
                    "organization_id",
                    "vehicle_id",
                    "session_id",
                    "event_id",
                    "item_id",
                )
            },
            {
                "type": "live.rows",
                "organization_id": self.org_id,
                "vehicle_id": self.vehicle_id,
                "session_id": str(self.session.pk),
                "event_id": event_id,
                "item_id": self.item_id,
            },
        )
        self.assertIsInstance(message["as_of"], str)
        json.dumps(message)  # plain types only, as the channel layer needs

    def test_a_vehicle_without_an_open_session_gets_none(self):
        other = str(self.vehicles[1].pk)
        self.assertEqual(live_rows.row_messages(self.org_id, other, self.item_id, str(uuid4())), [])

    def test_a_session_past_its_end_gets_none_even_while_marked_open(self):
        # Issue #278: stale OPEN sessions (their job not yet run) multiplied a save's cost.
        from cya.inspections.models import SessionProjection

        SessionProjection.objects.filter(session=self.session).update(
            ends_at=self.clock.now - timedelta(seconds=1)
        )
        self.assertEqual(SessionProjection.objects.get(session=self.session).state, "OPEN")
        self.assertEqual(
            live_rows.row_messages(self.org_id, self.vehicle_id, self.item_id, str(uuid4())), []
        )

    def test_rows_too_large_for_a_frame_are_left_out(self):
        with patch.object(live_rows, "MAX_BYTES", 10):
            self.assertEqual(
                live_rows.row_messages(self.org_id, self.vehicle_id, self.item_id, str(uuid4())), []
            )

    def test_only_participants_join_the_sessions_group(self):
        session_id = str(self.session.pk)
        self.assertTrue(
            _session_participant(str(self.crew.pk), self.org_id, self.vehicle_id, session_id)
        )
        self.assertFalse(
            _session_participant(str(self.supervisor.pk), self.org_id, self.vehicle_id, session_id),
            "a member who has not joined the session does not get its rows",
        )
        self.assertFalse(
            _session_participant(
                str(self.crew.pk), self.org_id, str(self.vehicles[1].pk), session_id
            )
        )
        self.assertFalse(
            _session_participant(str(self.crew.pk), self.org_id, self.vehicle_id, str(uuid4()))
        )


class ConsumerRowsTests(SimpleTestCase):
    def consumer(self, joined=True):
        session_id = str(uuid4())
        return SimpleNamespace(
            organization_id=str(uuid4()),
            vehicle_id=str(uuid4()),
            session_id=session_id,
            session_group=f"session.x.{session_id}" if joined else None,
            _send_json=AsyncMock(),
        )

    def event(self, consumer, **fields):
        item_id = str(uuid4())
        return {
            "organization_id": consumer.organization_id,
            "vehicle_id": consumer.vehicle_id,
            "session_id": consumer.session_id,
            "event_id": str(uuid4()),
            "item_id": item_id,
            "as_of": "2026-06-01T00:00:00Z",
            "items": [{"item_id": item_id, "obligation_state": "SATISFIED"}],
            **fields,
        }

    def test_well_formed_rows_for_the_joined_session_are_relayed(self):
        consumer = self.consumer()
        event = self.event(consumer)
        async_to_sync(VehicleLiveConsumer.live_rows)(consumer, event)
        consumer._send_json.assert_awaited_once_with(
            {
                "type": "checklist.rows",
                "event_id": event["event_id"],
                "session_id": consumer.session_id,
                "item_id": event["item_id"],
                "as_of": event["as_of"],
                "items": event["items"],
            }
        )

    def test_anything_else_is_dropped(self):
        consumer = self.consumer()
        bad = [
            {"organization_id": str(uuid4())},
            {"vehicle_id": str(uuid4())},
            {"session_id": str(uuid4())},
            {"event_id": "not-a-uuid"},
            {"item_id": "not-a-uuid"},
            {"as_of": None},
            {"items": "rows"},
            {"items": [{"item_id": str(uuid4())}]},  # a row of another item
        ]
        for fields in bad:
            async_to_sync(VehicleLiveConsumer.live_rows)(consumer, self.event(consumer, **fields))
        consumer._send_json.assert_not_awaited()
        outsider = self.consumer(joined=False)
        async_to_sync(VehicleLiveConsumer.live_rows)(outsider, self.event(outsider))
        outsider._send_json.assert_not_awaited()


class RecordingLayer(InMemoryChannelLayer):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def group_send(self, group, message):
        self.sent.append((group, message.copy()))
        await super().group_send(group, message)


def inline(job):
    job()
    return True


class PublishRowsTests(TestCase):
    def publish(self, rows, submit=inline):
        org, vehicle, event = (str(uuid4()) for _ in range(3))
        layer = RecordingLayer()
        with (
            patch("cya.live_updates.publish.get_channel_layer", return_value=layer),
            patch("cya.live_updates.publish._submit", side_effect=submit) as submitted,
            self.captureOnCommitCallbacks(execute=True),
        ):
            publish_vehicle_update(
                organization_id=org,
                vehicle_id=vehicle,
                event_id=event,
                kind="check.recorded",
                rows=rows,
            )
            self.assertEqual(layer.sent, [], "nothing before the commit")
        return [group for group, _m in layer.sent], submitted, f"vehicle.{org}.{vehicle}"

    def test_rows_go_out_before_the_hint_and_a_failure_keeps_the_hint(self):
        sent, _s, hint = self.publish(lambda: [("session.g", {"type": "live.rows"})])
        self.assertEqual(sent, ["session.g", hint])
        sent, _s, hint = self.publish(lambda: 1 / 0)
        self.assertEqual(sent, [hint])

    def test_rows_are_read_off_the_request_path(self):
        # Issue #278: the command's answer does not wait for the rows; the rows thread sends both.
        queued = []
        sent, submitted, _hint = self.publish(
            lambda: [("session.g", {})], submit=lambda job: queued.append(job) or True
        )
        self.assertEqual(sent, [], "neither rows nor hint is sent in the request")
        self.assertEqual(submitted.call_count, 1)

    def test_a_thread_too_far_behind_sends_the_hint_at_once_without_rows(self):
        read = []
        sent, _s, hint = self.publish(lambda: read.append(1) or [], submit=lambda job: False)
        self.assertEqual((sent, read), ([hint], []))

    def test_no_rows_means_no_thread(self):
        sent, submitted, hint = self.publish(None)
        self.assertEqual(sent, [hint])
        submitted.assert_not_called()


def wait_for_idle_rows_thread(test):
    """Wait until no rows job holds a slot, so a test starts from an empty queue.

    A job an earlier test queued may still be finishing on the rows thread (its slot is released
    after the job and the connection cleanup). Failing here means a slot was never released.
    """
    import time

    from cya.live_updates import publish

    deadline = time.monotonic() + 5
    while publish._pending and time.monotonic() < deadline:
        time.sleep(0.01)
    test.assertEqual(publish._pending, 0, "a rows job never released its slot")


class RowsThreadTests(SimpleTestCase):
    def setUp(self):
        wait_for_idle_rows_thread(self)
        self.addCleanup(wait_for_idle_rows_thread, self)

    def test_a_cleanup_failure_still_releases_the_slot(self):
        # Host full run on PR #302: a slot held after a job would end row pushes after 16.
        import threading

        from cya.live_updates import publish

        ran = threading.Event()
        with (
            patch.object(publish.connections, "close_all", side_effect=RuntimeError("closing")),
            self.assertLogs("cya.live_updates.publish", "ERROR") as logs,
        ):
            self.assertTrue(publish._submit(ran.set))
            self.assertTrue(ran.wait(5))
            wait_for_idle_rows_thread(self)
        self.assertIn("could not close its connections (RuntimeError)", logs.output[0])

    def test_jobs_run_in_order_on_one_thread_and_the_queue_is_bounded(self):
        import threading

        from cya.live_updates import publish

        gate, done, seen = threading.Event(), threading.Event(), []

        def job(n):
            def run():
                if n == 0:
                    gate.wait(5)
                seen.append((n, threading.current_thread().name))
                if n == publish.MAX_PENDING - 1:
                    done.set()

            return run

        with patch.object(publish, "connections"):
            accepted = [publish._submit(job(n)) for n in range(publish.MAX_PENDING + 2)]
            self.assertEqual(accepted, [True] * publish.MAX_PENDING + [False, False])
            gate.set()
            self.assertTrue(done.wait(5))
        self.assertEqual([n for n, _t in seen], list(range(publish.MAX_PENDING)))
        self.assertTrue(all(t.startswith("cya-rows") for _n, t in seen))
        self.assertTrue(publish._submit(lambda: None), "the queue drains")

    def test_a_pool_that_cannot_take_work_releases_the_slot(self):
        # Codex review (#296): submit raising must not lose the hint or leak a slot.
        from cya.live_updates import publish

        publish._submit(lambda: None)  # make sure the pool exists
        before = publish._pending
        with (
            patch.object(publish._executor, "submit", side_effect=RuntimeError("shut down")),
            self.assertLogs("cya.live_updates.publish", "ERROR"),
        ):
            self.assertFalse(publish._submit(lambda: None))
        self.assertEqual(publish._pending, before, "the slot is released")


class SubmitFailureFallbackTests(TestCase):
    def setUp(self):
        wait_for_idle_rows_thread(self)
        self.addCleanup(wait_for_idle_rows_thread, self)  # its own queued job, before later tests

    def test_the_hint_goes_out_when_the_rows_thread_cannot_take_the_job(self):
        from cya.live_updates import publish

        publish._submit(lambda: None)
        org, vehicle, event = (str(uuid4()) for _ in range(3))
        layer, read = RecordingLayer(), []
        with (
            patch("cya.live_updates.publish.get_channel_layer", return_value=layer),
            patch.object(publish._executor, "submit", side_effect=RuntimeError("shut down")),
            self.assertLogs("cya.live_updates.publish", "ERROR"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            publish_vehicle_update(
                organization_id=org,
                vehicle_id=vehicle,
                event_id=event,
                kind="check.recorded",
                rows=lambda: read.append(1) or [],
            )
        self.assertEqual([g for g, _m in layer.sent], [f"vehicle.{org}.{vehicle}"])
        self.assertEqual(read, [], "no rows were read")
