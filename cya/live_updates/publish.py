"""Schedule best-effort, identifier-only vehicle hints after a transaction commits."""

import logging
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from cya.live_updates.events import EVENT_KINDS

logger = logging.getLogger(__name__)


def _valid_id(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical UUID")
    try:
        valid = str(uuid.UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{field} must be a canonical UUID")


def publish_vehicle_update(*, organization_id, vehicle_id, event_id, kind):
    """Publish a small hint after commit; reconnecting clients re-read authoritative state.

    Delivery is intentionally best-effort. A channel-layer failure cannot roll back an
    already-committed business command; reconnect reconciliation repairs missed hints.
    """
    _valid_id(organization_id, "organization_id")
    _valid_id(vehicle_id, "vehicle_id")
    _valid_id(event_id, "event_id")
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        raise ValueError("kind is not a supported vehicle update")

    def deliver():
        try:
            channel_layer = get_channel_layer()
            if channel_layer is None:
                raise RuntimeError("channel layer is unavailable")
            async_to_sync(channel_layer.group_send)(
                f"vehicle.{organization_id}.{vehicle_id}",
                {
                    "type": "live.update",
                    "organization_id": organization_id,
                    "vehicle_id": vehicle_id,
                    "event_id": event_id,
                    "kind": kind,
                },
            )
        except Exception as error:
            logger.error("live update delivery unavailable (%s)", type(error).__name__)

    transaction.on_commit(deliver, robust=True)
