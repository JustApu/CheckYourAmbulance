"""Bounded WebSocket inputs and best-effort post-commit hint delivery."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from asgiref.sync import async_to_sync, sync_to_async
from asgiref.testing import ApplicationCommunicator
from channels.layers import InMemoryChannelLayer
from django.conf import settings
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.db import connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from cya.accounts.middleware import SESSION_AUTH_TIME_KEY, SESSION_USER_KEY
from cya.accounts.models import Organization, User
from cya.asgi import _application
from cya.authorization.commands import change_role
from cya.checklists.models import Vehicle
from cya.checklists.tests.support import ChecklistCase, ids
from cya.checks.models import CheckRecord
from cya.checks.tests.support import CheckCase
from cya.live_updates.auth import socket_access
from cya.live_updates.consumer import (
    VehicleLiveConsumer,
    _connections_by_user,
    _release_connection,
    _reserve_connection,
)
from cya.live_updates.publish import publish_vehicle_update


class SocketHandshakeTests(SimpleTestCase):
    @override_settings(ALLOWED_HOSTS=["example.test"])
    def test_origin_and_signed_session_are_required_before_group_join(self):
        async def handshake(headers):
            communicator = ApplicationCommunicator(
                _application(),
                {
                    "type": "websocket",
                    "path": f"/ws/v1/vehicles/{uuid4()}/live/",
                    "headers": headers,
                    "query_string": b"",
                    "subprotocols": [],
                },
            )
            await communicator.send_input({"type": "websocket.connect"})
            result = await communicator.receive_output()
            communicator.stop()
            return result

        host = (b"host", b"example.test")
        self.assertEqual(
            async_to_sync(handshake)([host, (b"origin", b"https://other.example")])["type"],
            "websocket.close",
        )
        self.assertEqual(async_to_sync(handshake)([host])["type"], "websocket.close")
        self.assertEqual(
            async_to_sync(handshake)([host, (b"origin", b"https://example.test")]),
            {"type": "websocket.close", "code": 4401},
        )


class ConsumerBoundsTests(TransactionTestCase):
    def setUp(self):
        _connections_by_user.clear()

    def tearDown(self):
        _connections_by_user.clear()

    @override_settings(CYA_LIVE_MAX_CONNECTIONS_PER_USER=1, CYA_LIVE_MAX_MESSAGE_BYTES=4)
    def test_connection_limit_is_released_and_can_be_reused(self):
        user_id = str(uuid4())
        self.assertTrue(_reserve_connection(user_id))
        self.assertFalse(_reserve_connection(user_id))
        _release_connection(user_id)
        self.assertTrue(_reserve_connection(user_id))
        _release_connection(user_id)

    @override_settings(CYA_LIVE_MAX_MESSAGE_BYTES=4)
    def test_oversize_text_and_binary_frames_close_with_message_too_big(self):
        for kwargs in ({"text_data": "12345"}, {"bytes_data": b"12345"}):
            consumer = SimpleNamespace(close=AsyncMock())
            async_to_sync(VehicleLiveConsumer.receive)(consumer, **kwargs)
            consumer.close.assert_awaited_once_with(code=1009)

    @override_settings(CYA_LIVE_MAX_MESSAGE_BYTES=4)
    def test_malformed_client_frame_is_rejected_by_server_only_protocol(self):
        consumer = SimpleNamespace(close=AsyncMock())
        async_to_sync(VehicleLiveConsumer.receive)(consumer, text_data="{")
        consumer.close.assert_awaited_once_with(code=4400)


class ConsumerEventTests(TransactionTestCase):
    def test_wrong_scope_and_malformed_event_hints_are_discarded(self):
        consumer = SimpleNamespace(
            organization_id=str(uuid4()),
            vehicle_id=str(uuid4()),
            _send_json=AsyncMock(),
        )
        bad_events = (
            {
                "organization_id": consumer.organization_id,
                "vehicle_id": consumer.vehicle_id,
                "event_id": "not-a-uuid",
                "kind": "check.recorded",
            },
            {
                "organization_id": consumer.organization_id,
                "vehicle_id": consumer.vehicle_id,
                "event_id": str(uuid4()),
                "kind": [],
            },
            {
                "organization_id": str(uuid4()),
                "vehicle_id": consumer.vehicle_id,
                "event_id": str(uuid4()),
                "kind": "check.recorded",
            },
        )
        for event in bad_events:
            async_to_sync(VehicleLiveConsumer.live_update)(consumer, event)
        consumer._send_json.assert_not_awaited()

    def test_valid_event_hint_carries_no_business_payload(self):
        consumer = SimpleNamespace(
            organization_id=str(uuid4()),
            vehicle_id=str(uuid4()),
            _send_json=AsyncMock(),
        )
        event_id = str(uuid4())
        event = {
            "organization_id": consumer.organization_id,
            "vehicle_id": consumer.vehicle_id,
            "event_id": event_id,
            "kind": "check.recorded",
            "outcome": "PASSED",
        }
        async_to_sync(VehicleLiveConsumer.live_update)(consumer, event)
        consumer._send_json.assert_awaited_once_with(
            {"type": "vehicle.update", "event_id": event_id, "kind": "check.recorded"}
        )

    def test_status_hints_are_relayed_as_identifier_only_frames(self):
        for kind in ("status.changed", "status.request"):
            consumer = SimpleNamespace(
                organization_id=str(uuid4()),
                vehicle_id=str(uuid4()),
                _send_json=AsyncMock(),
            )
            event_id = str(uuid4())
            event = {
                "organization_id": consumer.organization_id,
                "vehicle_id": consumer.vehicle_id,
                "event_id": event_id,
                "kind": kind,
                "restrictions": "must not travel",
            }
            async_to_sync(VehicleLiveConsumer.live_update)(consumer, event)
            consumer._send_json.assert_awaited_once_with(
                {"type": "vehicle.update", "event_id": event_id, "kind": kind}
            )


class PublishAfterCommitTests(TransactionTestCase):
    class RecordingLayer(InMemoryChannelLayer):
        def __init__(self):
            super().__init__()
            self.sent = []

        async def group_send(self, group, message):
            self.sent.append((group, message.copy()))
            await super().group_send(group, message)

    def publish(self, organization_id, vehicle_id, event_id):
        publish_vehicle_update(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            event_id=event_id,
            kind="check.recorded",
        )

    def test_hint_is_delivered_only_after_outer_transaction_commits(self):
        layer = self.RecordingLayer()
        organization_id, vehicle_id, event_id = (str(uuid4()) for _ in range(3))
        channel = async_to_sync(layer.new_channel)()
        group = f"vehicle.{organization_id}.{vehicle_id}"
        async_to_sync(layer.group_add)(group, channel)
        with patch("cya.live_updates.publish.get_channel_layer", return_value=layer):
            with transaction.atomic():
                self.publish(organization_id, vehicle_id, event_id)
                self.assertEqual(layer.sent, [])
            self.assertEqual(len(layer.sent), 1)
            message = async_to_sync(layer.receive)(channel)
        self.assertEqual(
            message,
            {
                "type": "live.update",
                "organization_id": organization_id,
                "vehicle_id": vehicle_id,
                "event_id": event_id,
                "kind": "check.recorded",
            },
        )

    def test_hint_is_discarded_when_business_transaction_rolls_back(self):
        layer = self.RecordingLayer()
        with patch("cya.live_updates.publish.get_channel_layer", return_value=layer):
            try:
                with transaction.atomic():
                    self.publish(str(uuid4()), str(uuid4()), str(uuid4()))
                    raise RuntimeError("rollback fixture")
            except RuntimeError:
                pass
        self.assertEqual(layer.sent, [])

    def test_odometer_decision_hints_are_supported(self):
        layer = self.RecordingLayer()
        organization_id, vehicle_id = (str(uuid4()) for _ in range(2))
        event_ids = [str(uuid4()), str(uuid4())]
        kinds = ("odometer.corrected", "odometer.invalidated")
        with patch("cya.live_updates.publish.get_channel_layer", return_value=layer):
            with transaction.atomic():
                for event_id, kind in zip(event_ids, kinds, strict=True):
                    publish_vehicle_update(
                        organization_id=organization_id,
                        vehicle_id=vehicle_id,
                        event_id=event_id,
                        kind=kind,
                    )
        self.assertEqual([message[1]["kind"] for message in layer.sent], list(kinds))

    def test_invalid_ids_and_event_kinds_are_rejected(self):
        with self.assertRaises(ValueError):
            publish_vehicle_update(
                organization_id="not-an-id",
                vehicle_id=str(uuid4()),
                event_id=str(uuid4()),
                kind="check.recorded",
            )
        with self.assertRaises(ValueError):
            publish_vehicle_update(
                organization_id=str(uuid4()),
                vehicle_id=str(uuid4()),
                event_id=str(uuid4()),
                kind="check.payload",
            )


class SocketAuthorizationTests(ChecklistCase):
    def session(self, user):
        return {SESSION_USER_KEY: str(user.pk), SESSION_AUTH_TIME_KEY: int(time.time())}

    def test_current_organization_member_with_role_can_join_vehicle(self):
        access = async_to_sync(socket_access)(self.session(self.crew), str(self.vehicles[0].pk))
        self.assertEqual(access, (str(self.crew.pk), str(self.org.pk)))

    @override_settings(ALLOWED_HOSTS=["example.test"])
    def test_signed_cookie_joins_the_requested_vehicle_socket(self):
        store = SessionStore()
        store.update(self.session(self.crew))
        store.save()

        async def connect():
            communicator = ApplicationCommunicator(
                _application(),
                {
                    "type": "websocket",
                    "path": f"/ws/v1/vehicles/{self.vehicles[0].pk}/live/",
                    "headers": [
                        (b"host", b"example.test"),
                        (b"origin", b"https://example.test"),
                        (b"cookie", f"cya_session={store.session_key}".encode()),
                    ],
                    "query_string": b"",
                    "subprotocols": [],
                },
            )
            await communicator.send_input({"type": "websocket.connect"})
            accepted = await communicator.receive_output()
            ready = await communicator.receive_output()
            await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
            await communicator.wait()
            return accepted, ready

        accepted, ready = async_to_sync(connect)()
        self.assertEqual(accepted["type"], "websocket.accept")
        self.assertIn('"type":"connection.ready"', ready["text"])
        self.assertIn(str(self.vehicles[0].pk), ready["text"])

    def test_unauthenticated_disabled_and_stale_sessions_are_denied(self):
        self.assertIsNone(async_to_sync(socket_access)({}, str(self.vehicles[0].pk)))
        self.crew.active = False
        self.crew.save(update_fields=["active"])
        self.assertIsNone(
            async_to_sync(socket_access)(self.session(self.crew), str(self.vehicles[0].pk))
        )
        stale = self.session(self.supervisor)
        stale[SESSION_AUTH_TIME_KEY] = int(time.time()) - settings.CYA_SESSION_MAX_AGE - 1
        self.assertIsNone(async_to_sync(socket_access)(stale, str(self.vehicles[0].pk)))

    def test_inactive_and_cross_organization_vehicles_are_denied(self):
        vehicle = self.vehicles[0]
        vehicle.active = False
        vehicle.save(update_fields=["active"])
        self.assertIsNone(async_to_sync(socket_access)(self.session(self.crew), str(vehicle.pk)))

        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign = Vehicle.objects.create(organization=other, display_name="Foreign Synthetic")
        self.assertIsNone(async_to_sync(socket_access)(self.session(self.crew), str(foreign.pk)))


class SocketReauthorizationTests(ChecklistCase):
    """Connected sockets are closed when any current authorization input changes."""

    @property
    def vehicle(self):
        return self.vehicles[0]

    def session(self, user):
        return {SESSION_USER_KEY: str(user.pk), SESSION_AUTH_TIME_KEY: int(time.time())}

    @override_settings(ALLOWED_HOSTS=["example.test"], CYA_LIVE_AUTH_RECHECK_SECONDS=0.2)
    def assert_socket_rechecks_to_close(self, mutate, *, code=4403, auth_time=None):
        store = SessionStore()
        session = self.session(self.crew)
        if auth_time is not None:
            session[SESSION_AUTH_TIME_KEY] = auth_time
        store.update(session)
        store.save()

        async def exercise():
            communicator = ApplicationCommunicator(
                _application(),
                {
                    "type": "websocket",
                    "path": f"/ws/v1/vehicles/{self.vehicle.pk}/live/",
                    "headers": [
                        (b"host", b"example.test"),
                        (b"origin", b"https://example.test"),
                        (b"cookie", f"cya_session={store.session_key}".encode()),
                    ],
                    "query_string": b"",
                    "subprotocols": [],
                },
            )
            await communicator.send_input({"type": "websocket.connect"})
            accepted = await communicator.receive_output(timeout=5)
            ready = await communicator.receive_output(timeout=5)
            await sync_to_async(mutate, thread_sensitive=True)()
            closed = await communicator.receive_output(timeout=5)
            await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
            await communicator.wait(timeout=5)
            return accepted, ready, closed

        accepted, ready, closed = async_to_sync(exercise)()
        self.assertEqual(accepted["type"], "websocket.accept")
        self.assertIn('"type":"connection.ready"', ready["text"])
        self.assertEqual(closed, {"type": "websocket.close", "code": code})

    def test_role_removal_closes_connected_socket(self):
        def revoke_crew_role():
            change_role(
                organization_id=str(self.org.pk),
                actor_id=str(self.admin.pk),
                target_id=str(self.crew.pk),
                role="CREW",
                action="REVOKE",
                **ids(),
            )

        self.assert_socket_rechecks_to_close(revoke_crew_role)

    def test_user_deactivation_closes_connected_socket(self):
        def deactivate_user():
            User.objects.filter(pk=self.crew.pk).update(active=False)

        self.assert_socket_rechecks_to_close(deactivate_user)

    def test_vehicle_deactivation_closes_connected_socket(self):
        def deactivate_vehicle():
            Vehicle.objects.filter(pk=self.vehicle.pk).update(active=False)

        self.assert_socket_rechecks_to_close(deactivate_vehicle)

    @override_settings(CYA_SESSION_MAX_AGE=300)
    def test_session_expiry_closes_connected_socket(self):
        expiring_auth_time = int(time.time()) - 299
        self.assert_socket_rechecks_to_close(lambda: None, auth_time=expiring_auth_time)

    def test_authorization_check_exception_closes_with_internal_error(self):
        with patch(
            "cya.live_updates.consumer.socket_access_still_valid",
            side_effect=RuntimeError("synthetic authorization outage"),
        ):
            self.assert_socket_rechecks_to_close(lambda: None, code=1011)


class CheckPublicationTests(CheckCase):
    def test_failed_hint_keeps_committed_check_available_for_reconciliation(self):
        layer = PublishAfterCommitTests.RecordingLayer()
        with (
            patch("cya.live_updates.publish.get_channel_layer", return_value=layer),
            patch.object(layer, "group_send", side_effect=RuntimeError("synthetic channel outage")),
        ):
            result = self.record()
        check = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertEqual(str(check.vehicle_id), str(self.vehicle.pk))
        self.assertEqual(check.outcome, "PASSED")

    def test_recorded_check_hint_is_scheduled_in_its_command_transaction(self):
        layer = PublishAfterCommitTests.RecordingLayer()
        group = f"vehicle.{self.org.pk}.{self.vehicle.pk}"
        channel = async_to_sync(layer.new_channel)()
        async_to_sync(layer.group_add)(group, channel)

        def schedule_inside_command(**kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(layer.sent, [])
            return publish_vehicle_update(**kwargs)

        with (
            patch("cya.live_updates.publish.get_channel_layer", return_value=layer),
            patch(
                "cya.live_updates.publish.publish_vehicle_update",
                side_effect=schedule_inside_command,
            ),
        ):
            result = self.record()
            self.assertEqual(len(layer.sent), 1)
            message = async_to_sync(layer.receive)(channel)
        self.assertEqual(message["type"], "live.update")
        self.assertEqual(message["vehicle_id"], str(self.vehicle.pk))
        self.assertEqual(message["event_id"], result.body["check_id"])
        self.assertEqual(message["kind"], "check.recorded")
