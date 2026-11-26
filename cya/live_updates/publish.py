"""Schedule best-effort, identifier-only vehicle hints after a transaction commits."""

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import connections, transaction

from cya.live_updates.events import EVENT_KINDS

logger = logging.getLogger(__name__)

# Pushed rows are read off the request path (issue #278): a save answers at once, and one
# background thread per process reads the rows, sends them, then sends the hint. At most
# MAX_PENDING such jobs wait; past that, the hint goes out at once without rows and the pages
# re-read, as before SPEC 8.3.0. With a few saves a second this keeps every hint well inside
# SPEC 17.3's 5-second target.
MAX_PENDING = 16
_pending = 0
_pending_lock = threading.Lock()
_executor = None


def _submit(job):
    """Queue job on this process's rows thread; False when MAX_PENDING jobs already wait."""
    global _executor, _pending
    with _pending_lock:
        if _pending >= MAX_PENDING:
            return False
        _pending += 1
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cya-rows")

    def run():
        global _pending
        try:
            job()
        finally:
            connections.close_all()  # this thread's own connections; it may wait a long time
            with _pending_lock:
                _pending -= 1

    try:
        _executor.submit(run)
    except Exception as error:  # a pool that cannot take work: release the slot, send directly
        with _pending_lock:
            _pending -= 1
        logger.error("pushed rows thread unavailable (%s)", type(error).__name__)
        return False
    return True


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
    rows for each open session's group (SPEC 8.3.0, cya.attention.live_rows). Reading them is
    left to this process's rows thread, so the command's answer does not wait for it; the
    thread sends the rows and then the hint, so a page that receives both sees the rows first
    and need not re-read. A failure there only loses the rows: the hint is still sent. When the
    thread is too far behind, the hint goes out at once without rows.
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

    def send(with_rows):
        try:
            channel_layer = get_channel_layer()
            if channel_layer is None:
                raise RuntimeError("channel layer is unavailable")
            if with_rows:
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

    def deliver():
        if rows is not None and _submit(lambda: send(True)):
            return
        send(False)

    transaction.on_commit(deliver, robust=True)
