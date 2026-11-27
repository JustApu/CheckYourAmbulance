"""One live readiness picture shared by an organization's readers (SPEC 8.4.0 §17.3, OQ-89).

Issue #278 Part J. Every Supervisor and Administrator of an organization computes the same
picture: `fleet_readiness` depends only on the organization and the instant. So a live re-read
of the panel may show the organization's most recent picture if its instant is no more than
CYA_READINESS_SHARED_PICTURE_SECONDS (at most 15, 0 turns sharing off) before the re-read began.

- **Who reads and stores:** `live_picture(share=True)` is the panel's live re-read: it may use the
  stored picture. `share=False` is the screen's first load: it always computes, then stores. The
  API and every as-of read call `fleet_readiness` directly and never read or store a picture.
  Authorization runs in the view before either.
- **Newer only:** a picture replaces the stored one only if its instant is later, so the stored
  picture never goes backwards, even when a slow computation finishes after a newer one.
- **One computation at a time, bounded wait:** a reader whose stored picture is too old takes a
  lease (LEASE_SECONDS) and computes. A reader that finds the lease taken waits up to
  WAIT_SECONDS for a picture new enough for its own start; failing that it computes its own. So
  readers compute concurrently only when a computation outlasts WAIT_SECONDS, and then no more
  of them than were waiting: never more computations than without sharing.
- **Where:** with one process, in process memory. With several (DEP-2, amendment 8.2.0), in the
  shared store (CYA_SHARED_STORE_URL), so the organization computes at most about one picture
  per age bound across processes. The picture is stored as JSON, never pickle, so nothing read
  back can run code. Any store error is logged and the reader computes its own picture: a read
  never fails because of the store.
- **Each picture stays exact:** it is the ordinary current answer as of its own instant, and the
  panel shows that instant ("Updated ...").
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime

from django.conf import settings

from cya.attention.readiness import FleetReadiness, VehicleReadiness

logger = logging.getLogger(__name__)
LEASE_SECONDS = 30  # longer than any panel read; an abandoned lease expires by itself
WAIT_SECONDS = 2.0
POLL_SECONDS = 0.1
KEEP_SECONDS = 60  # how long the shared store keeps a picture nobody replaces
DATETIME = "__datetime__"


def _plain(value):
    if isinstance(value, datetime):
        return {DATETIME: value.isoformat()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot share a {type(value).__name__} in a readiness picture")


def _restored(value):
    if isinstance(value, dict):
        if set(value) == {DATETIME}:
            return datetime.fromisoformat(value[DATETIME])
        return {k: _restored(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restored(v) for v in value]
    return value


def encode(picture):
    """The picture as JSON text; refuses any value JSON cannot carry exactly."""
    return json.dumps(_plain(asdict(picture)), separators=(",", ":"), allow_nan=False)


def decode(text):
    """A fresh FleetReadiness from encode's text; the caller may change it freely."""
    data = _restored(json.loads(text))
    vehicles = [VehicleReadiness(**v) for v in data.pop("vehicles")]
    return FleetReadiness(vehicles=vehicles, **data)


def _micros(instant):
    return int(instant.timestamp() * 1_000_000)


class LocalStore:
    """Pictures and leases in this process, keyed by organization."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._pictures = {}  # organization -> (instant micros, text)
        self._leases = {}  # organization -> (token, expiry)

    def get(self, organization_id):
        with self._lock:
            return self._pictures.get(organization_id)

    def put(self, organization_id, micros, text):
        with self._lock:
            current = self._pictures.get(organization_id)
            if current is None or micros > current[0]:
                self._pictures[organization_id] = (micros, text)

    def acquire(self, organization_id, token):
        now = self._clock()
        with self._lock:
            held = self._leases.get(organization_id)
            if held is not None and held[1] > now:
                return False
            self._leases[organization_id] = (token, now + LEASE_SECONDS)
            return True

    def release(self, organization_id, token):
        with self._lock:
            if self._leases.get(organization_id, (None,))[0] == token:
                del self._leases[organization_id]

    def clear(self):
        with self._lock:
            self._pictures.clear()
            self._leases.clear()


# KEYS[1] picture hash; ARGV: instant micros, text, keep seconds. Replaces only a strictly older
# picture, atomically, so concurrent writers can never move the stored instant backwards.
PUT_NEWER = """
local current = redis.call('HGET', KEYS[1], 'at')
if current and tonumber(current) >= tonumber(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[1], 'at', ARGV[1], 'picture', ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""
RELEASE_OWN = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class SharedStore:
    """Pictures and leases in the shared store, so every process shares them."""

    def __init__(self, client):
        self._client = client
        self._put = client.register_script(PUT_NEWER)
        self._release = client.register_script(RELEASE_OWN)

    @staticmethod
    def _key(kind, organization_id):
        return f"cya:readiness:{kind}:{organization_id}"

    def get(self, organization_id):
        at, text = self._client.hmget(self._key("picture", organization_id), "at", "picture")
        if at is None or text is None:
            return None
        return int(at), text.decode()

    def put(self, organization_id, micros, text):
        self._put(keys=[self._key("picture", organization_id)], args=[micros, text, KEEP_SECONDS])

    def acquire(self, organization_id, token):
        key = self._key("lease", organization_id)
        return bool(self._client.set(key, token, nx=True, ex=LEASE_SECONDS))

    def release(self, organization_id, token):
        self._release(keys=[self._key("lease", organization_id)], args=[token])

    def clear(self):
        pass


def make_store():
    """The store for this deployment: shared with a configured store, else process memory."""
    if not settings.CYA_SHARED_STORE_URL:
        return LocalStore()
    import redis

    client = redis.Redis.from_url(
        settings.CYA_SHARED_STORE_URL,
        password=settings.CYA_SHARED_STORE_PASSWORD,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
    )
    return SharedStore(client)


STORE = make_store()


def _store_errors():
    try:
        import redis
    except ImportError:  # pragma: no cover - redis is a declared dependency
        return (OSError,)
    return (redis.RedisError, OSError)


def live_picture(organization_id, compute, now, *, share, store=None, sleep=time.sleep):
    """The organization's live picture: compute(instant) or, if share, a recent stored one.

    `now()` gives the current instant; `compute(instant)` is fleet_readiness for it.
    """
    store = STORE if store is None else store
    organization_id = str(organization_id)
    age = settings.CYA_READINESS_SHARED_PICTURE_SECONDS
    if age <= 0:
        return compute(now())
    errors = _store_errors()
    start = now()
    oldest = _micros(start) - age * 1_000_000

    def recent():
        try:
            found = store.get(organization_id)
        except errors:
            logger.error("shared readiness picture unavailable; computing in this request")
            return None
        if found is None or found[0] < oldest:
            return None
        try:
            return decode(found[1])
        except (ValueError, TypeError, KeyError):
            logger.error("shared readiness picture unreadable; computing in this request")
            return None

    def computed():
        picture = compute(now())
        try:
            store.put(organization_id, _micros(picture.as_of), encode(picture))
        except errors:
            logger.error("shared readiness picture not stored")
        return picture

    if not share:
        return computed()
    picture = recent()
    if picture is not None:
        return picture
    token = uuid.uuid4().hex
    try:
        leased = store.acquire(organization_id, token)
    except errors:
        logger.error("shared readiness lease unavailable; computing in this request")
        return compute(now())
    if leased:
        try:
            return computed()
        finally:
            try:
                store.release(organization_id, token)
            except errors:
                logger.error("shared readiness lease not released; it expires by itself")
    for _poll in range(round(WAIT_SECONDS / POLL_SECONDS)):
        sleep(POLL_SECONDS)
        picture = recent()
        if picture is not None:
            return picture
    return computed()
