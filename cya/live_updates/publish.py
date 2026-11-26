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


def publish_vehicle_update(*, organization_id, vehicle_id, event_id, kind, item_id=None, rows=None):
    """Publish a small hint after commit; reconnecting clients re-read authoritative state.

    Delivery is intentionally best-effort. A channel-layer failure cannot roll back an
    already-committed business command; reconnect reconciliation repairs missed hints.

    item_id, when given, promises that the change can alter only that item's checklist rows, so
    a page may re-read just those (issue #278). Leave it out whenever the change may reach
    other rows; a page then re-reads the whole checklist.

    rows, when given, is called after the commit and returns [(group, message)]: the changed
    rows for each open session's group (SPEC 8.3.0, cya.attention.live_rows). They are sent
    before the hint, so a page that receives both sees the rows first and need not re-read. A
    failure there only loses the rows: the hint is still sent.
    """
    _valid_id(organization_id, "organization_id")
    _valid_id(vehicle_id, "vehicle_id")
    _valid_id(event_id, "event_id")
    if item_id is not None:
        _valid_id(item_id, "item_id")
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        raise ValueError("kind is not a supported vehicle update")
    message = {
        "type": "live.update",
        "organization_id": organization_id,
        "vehicle_id": vehicle_id,
        "event_id": event_id,
        "kind": kind,
    }
    if item_id is not None:
        message["item_id"] = item_id

    def deliver():
        try:
            channel_layer = get_channel_layer()
            if channel_layer is None:
                raise RuntimeError("channel layer is unavailable")
            if rows is not None:
                try:
                    for group, rows_message in rows():
                        async_to_sync(channel_layer.group_send)(group, rows_message)
                except Exception as error:
                    logger.error("pushed rows unavailable (%s)", type(error).__name__)
            async_to_sync(channel_layer.group_send)(
                f"vehicle.{organization_id}.{vehicle_id}", message
            )
        except Exception as error:
            logger.error("live update delivery unavailable (%s)", type(error).__name__)

    transaction.on_commit(deliver, robust=True)
