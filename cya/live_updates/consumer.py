"""Authenticated vehicle update socket: identifier-only hints, and a session's pushed rows.

A page in an inspection session may name it (?session=<id>). If the user is a recorded
participant, the socket also joins that session's group and relays the item rows pushed
after a check (SPEC 8.3.0 §17.3, cya.attention.live_rows); otherwise the name is ignored,
so nothing reveals whether the session exists. Every other frame stays identifier-only.
"""

import asyncio
import json
import logging
import threading
import uuid
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from cya.accounts.middleware import SESSION_AUTH_TIME_KEY
from cya.live_updates.auth import session_participant, socket_access, socket_access_still_valid
from cya.live_updates.events import EVENT_KINDS

logger = logging.getLogger(__name__)

_connection_lock = threading.Lock()
_connections_by_user = {}


def _canonical_uuid(value):
    try:
        return str(uuid.UUID(str(value))) == str(value)
    except (ValueError, TypeError, AttributeError):
        return False


def _reserve_connection(user_id):
    with _connection_lock:
        count = _connections_by_user.get(user_id, 0)
        if count >= settings.CYA_LIVE_MAX_CONNECTIONS_PER_USER:
            return False
        _connections_by_user[user_id] = count + 1
        return True


def _release_connection(user_id):
    with _connection_lock:
        count = _connections_by_user.get(user_id, 0)
        if count <= 1:
            _connections_by_user.pop(user_id, None)
        else:
            _connections_by_user[user_id] = count - 1


class VehicleLiveConsumer(AsyncWebsocketConsumer):
    """Only current agency members with a built-in role may join a vehicle group."""

    async def connect(self):
        self.vehicle_id = str(self.scope["url_route"]["kwargs"]["vehicle_id"])
        session = self.scope.get("session")
        if session is None:
            await self.close(code=4401)
            return
        try:
            access = await socket_access(session, self.vehicle_id)
        except Exception as error:
            logger.error("live connection authorization unavailable (%s)", type(error).__name__)
            await self.close(code=1011)
            return
        if access is None:
            await self.close(code=4401)
            return
        self.user_id, self.organization_id = access
        self.auth_time = session.get(SESSION_AUTH_TIME_KEY)
        self.group_name = f"vehicle.{self.organization_id}.{self.vehicle_id}"
        self.reserved = False
        self.joined = False
        self.auth_task = None
        self.session_id = None
        self.session_group = None
        requested = parse_qs(self.scope.get("query_string", b"").decode("latin-1")).get("session")
        if requested and len(requested) == 1 and _canonical_uuid(requested[0]):
            self.session_id = requested[0]

        if not _reserve_connection(self.user_id):
            await self.close(code=4429)
            return
        self.reserved = True
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            self.joined = True
            if self.session_id is not None and await session_participant(
                self.user_id, self.organization_id, self.vehicle_id, self.session_id
            ):
                self.session_group = f"session.{self.organization_id}.{self.session_id}"
                await self.channel_layer.group_add(self.session_group, self.channel_name)
            await self.accept()
            await self._send_json({"type": "connection.ready", "vehicle_id": self.vehicle_id})
            self.auth_task = asyncio.create_task(self._reauthorize_periodically())
        except Exception as error:
            logger.error("live connection setup failed (%s)", type(error).__name__)
            await self.close(code=1011)
            await self._cleanup()

    async def disconnect(self, close_code):
        await self._cleanup()

    async def _cleanup(self):
        task = getattr(self, "auth_task", None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            self.auth_task = None
        if getattr(self, "joined", False):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
                if getattr(self, "session_group", None) is not None:
                    await self.channel_layer.group_discard(self.session_group, self.channel_name)
            except Exception as error:
                logger.error("live group cleanup failed (%s)", type(error).__name__)
            self.joined = False
        if getattr(self, "reserved", False):
            _release_connection(self.user_id)
            self.reserved = False

    async def _reauthorize_periodically(self):
        while True:
            await asyncio.sleep(settings.CYA_LIVE_AUTH_RECHECK_SECONDS)
            try:
                valid = await socket_access_still_valid(
                    self.user_id, self.organization_id, self.vehicle_id, self.auth_time
                )
            except Exception as error:
                logger.error(
                    "live connection reauthorization unavailable (%s)", type(error).__name__
                )
                await self.close(code=1011)
                return
            if not valid:
                await self.close(code=4403)
                return

    async def receive(self, text_data=None, bytes_data=None):
        if (
            text_data is not None
            and len(text_data.encode("utf-8")) > settings.CYA_LIVE_MAX_MESSAGE_BYTES
        ):
            await self.close(code=1009)
        elif bytes_data is not None and len(bytes_data) > settings.CYA_LIVE_MAX_MESSAGE_BYTES:
            await self.close(code=1009)
        else:
            # This protocol is server-to-client only. Client frames cannot change state.
            await self.close(code=4400)

    async def live_update(self, event):
        vehicle_id = event.get("vehicle_id")
        event_id = event.get("event_id")
        kind = event.get("kind")
        item_id = event.get("item_id")
        if (
            event.get("organization_id") != self.organization_id
            or vehicle_id != self.vehicle_id
            or not _canonical_uuid(event_id)
            or not isinstance(kind, str)
            or kind not in EVENT_KINDS
            or (item_id is not None and not _canonical_uuid(item_id))
        ):
            logger.error("invalid live update rejected")
            return
        frame = {"type": "vehicle.update", "event_id": event_id, "kind": kind}
        if item_id is not None:
            frame["item_id"] = item_id  # issue #278: only this item's rows can have changed
        await self._send_json(frame)

    async def live_rows(self, event):
        """A session's pushed item rows (SPEC 8.3.0), relayed only on a socket that joined it."""
        items = event.get("items")
        if (
            self.session_group is None
            or event.get("organization_id") != self.organization_id
            or event.get("vehicle_id") != self.vehicle_id
            or event.get("session_id") != self.session_id
            or not _canonical_uuid(event.get("event_id"))
            or not _canonical_uuid(event.get("item_id"))
            or not isinstance(event.get("as_of"), str)
            or not isinstance(items, list)
            or not all(
                isinstance(item, dict) and item.get("item_id") == event["item_id"] for item in items
            )
        ):
            logger.error("invalid pushed rows rejected")
            return
        await self._send_json(
            {
                "type": "checklist.rows",
                "event_id": event["event_id"],
                "session_id": self.session_id,
                "item_id": event["item_id"],
                "as_of": event["as_of"],
                "items": items,
            }
        )

    async def _send_json(self, value):
        await self.send(text_data=json.dumps(value, ensure_ascii=True, separators=(",", ":")))
