"""The shared live readiness picture (SPEC 8.4.0 §17.3, OQ-89; issue #278 Part J)."""

from datetime import UTC, datetime, timedelta

import redis
from django.test import SimpleTestCase, override_settings

from cya.attention import shared_picture
from cya.attention.readiness import FleetReadiness, VehicleReadiness
from cya.attention.shared_picture import (
    POLL_SECONDS,
    WAIT_SECONDS,
    LocalStore,
    SharedStore,
    _micros,
    decode,
    encode,
    live_picture,
)

T0 = datetime(2026, 11, 26, 20, 0, 0, 123456, tzinfo=UTC)
ORG = "00000000-0000-0000-0000-000000000001"


def picture_at(instant, label="Medic 1"):
    vehicle = VehicleReadiness(
        vehicle_id="v1",
        display_name=label,
        active=True,
        status="IN_SERVICE",
        restrictions=None,
        counts={"PASSED": 3, "MISSED": 1},
        progress={"checked": 3, "owed": 5},
        deficiencies=[
            {
                "deficiency_id": "d1",
                "summary": "Suction weak",
                "status": "OPEN",
                "severity": {
                    "code": "HIGH",
                    "revision": 1,
                    "label": "High",
                    "description": "",
                    "rank": 3,
                },
                "governing_deadline": instant + timedelta(hours=4),
                "overdue": False,
                "age_days": 0,
                "age_level": None,
                "long_running": False,
                "extension_count": 0,
                "follow_up_at": None,
                "awaiting_dependency": False,
                "acknowledged_until": None,
            }
        ],
        by_severity={"High": 1},
        by_status={"OPEN": 1},
        suspensions=[
            {
                "suspension_id": "s1",
                "label": "Monitor",
                "deficiency_id": "d1",
                "started_at": instant - timedelta(minutes=5),
            }
        ],
    )
    return FleetReadiness(
        instant,
        instant - timedelta(days=7),
        instant + timedelta(hours=48),
        [vehicle],
        False,
        {"PASSED": 3, "MISSED": 1},
    )


class Clock:
    def __init__(self, instant=T0):
        self.instant = instant

    def __call__(self):
        return self.instant


class Computer:
    """compute(instant) for live_picture that records each computation."""

    def __init__(self, clock=None, duration=timedelta(0)):
        self.instants, self.clock, self.duration = [], clock, duration

    def __call__(self, instant):
        self.instants.append(instant)
        if self.clock is not None:
            self.clock.instant += self.duration
        return picture_at(instant, label=f"computed {len(self.instants)}")


class FailingStore:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.ConnectionError("down")

        return fail


class FakeRedis:
    """hmget, set(nx, ex) and register_script as SharedStore uses them, with the scripts' rules."""

    def __init__(self):
        self.hashes, self.strings = {}, {}

    def hmget(self, key, *fields):
        values = self.hashes.get(key, {})
        return [values.get(f) for f in fields]

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings:
            return None
        self.strings[key] = value.encode() if isinstance(value, str) else value
        return True

    def register_script(self, source):
        if "HSET" in source:

            def put(keys, args):
                current = self.hashes.get(keys[0], {}).get("at")
                if current is not None and not current.isdigit():
                    # Lua's tonumber gives nil, and comparing nil is a script error.
                    raise redis.ResponseError("attempt to compare nil with number")
                if current is not None and int(current) >= int(args[0]):
                    return 0
                self.hashes[keys[0]] = {"at": str(args[0]).encode(), "picture": args[1].encode()}
                return 1

            return put

        def release(keys, args):
            if self.strings.get(keys[0]) == args[0].encode():
                del self.strings[keys[0]]
                return 1
            return 0

        return release


class EncodingTests(SimpleTestCase):
    def test_a_picture_survives_the_json_round_trip_exactly(self):
        picture = picture_at(T0)
        restored = decode(encode(picture))
        self.assertEqual(restored, picture)
        self.assertEqual(restored.as_of, T0)
        self.assertEqual(restored.as_of.utcoffset(), timedelta(0))
        self.assertIsInstance(restored.vehicles[0], VehicleReadiness)
        self.assertEqual(
            restored.vehicles[0].deficiencies[0]["governing_deadline"], T0 + timedelta(hours=4)
        )

    def test_each_decode_is_a_fresh_copy_a_view_may_change(self):
        text = encode(picture_at(T0))
        first = decode(text)
        first.vehicles[0].counts["PASSED"] = 99
        first.vehicles[0].status_label = "changed by a view"
        second = decode(text)
        self.assertEqual(second.vehicles[0].counts["PASSED"], 3)
        self.assertFalse(hasattr(second.vehicles[0], "status_label"))

    def test_a_value_json_cannot_carry_is_refused_not_stored(self):
        picture = picture_at(T0)
        picture.vehicles[0].progress["odd"] = {1, 2}
        with self.assertRaises(TypeError):
            encode(picture)


@override_settings(CYA_READINESS_SHARED_PICTURE_SECONDS=15)
class LivePictureTests(SimpleTestCase):
    def setUp(self):
        self.clock, self.store = Clock(), LocalStore()

    def live(self, compute, share=True, store=None, sleep=None):
        return live_picture(
            ORG,
            compute,
            self.clock,
            share=share,
            store=store or self.store,
            sleep=sleep or (lambda seconds: None),
        )

    def test_a_first_load_always_computes_and_stores(self):
        compute = Computer()
        self.live(compute, share=False)
        self.clock.instant += timedelta(seconds=1)
        self.live(compute, share=False)
        self.assertEqual(compute.instants, [T0, T0 + timedelta(seconds=1)])
        self.assertEqual(self.store.get(ORG)[0], _micros(T0 + timedelta(seconds=1)))

    def test_a_re_read_uses_a_picture_at_most_15_seconds_old(self):
        compute = Computer()
        self.live(compute, share=False)
        self.clock.instant = T0 + timedelta(seconds=15)
        shared = self.live(compute)
        self.assertEqual(compute.instants, [T0])
        self.assertEqual(shared.as_of, T0)
        self.assertEqual(shared.vehicles[0].display_name, "computed 1")

    def test_a_picture_older_than_15_seconds_is_never_shown(self):
        compute = Computer()
        self.live(compute, share=False)
        self.clock.instant = T0 + timedelta(seconds=15, microseconds=1)
        fresh = self.live(compute)
        self.assertEqual(compute.instants, [T0, self.clock.instant])
        self.assertEqual(fresh.as_of, self.clock.instant)
        self.assertEqual(self.store.get(ORG)[0], _micros(self.clock.instant))

    def test_another_organization_never_gets_the_picture(self):
        compute = Computer()
        self.live(compute, share=False)
        other = live_picture(
            "00000000-0000-0000-0000-000000000002",
            compute,
            self.clock,
            share=True,
            store=self.store,
            sleep=lambda s: None,
        )
        self.assertEqual(len(compute.instants), 2)
        self.assertEqual(other.vehicles[0].display_name, "computed 2")

    @override_settings(CYA_READINESS_SHARED_PICTURE_SECONDS=0)
    def test_zero_turns_sharing_off(self):
        compute = Computer()
        self.live(compute, share=False)
        self.live(compute)
        self.assertEqual(len(compute.instants), 2)
        self.assertIsNone(self.store.get(ORG))

    def test_a_stored_picture_is_replaced_only_by_a_later_one(self):
        later, earlier = picture_at(T0 + timedelta(seconds=5)), picture_at(T0)
        self.store.put(ORG, _micros(later.as_of), encode(later))
        self.store.put(ORG, _micros(earlier.as_of), encode(earlier))  # a slow reader finishing
        self.assertEqual(decode(self.store.get(ORG)[1]).as_of, later.as_of)
        self.store.put(ORG, _micros(later.as_of), encode(picture_at(later.as_of, "same")))
        self.assertEqual(decode(self.store.get(ORG)[1]).vehicles[0].display_name, "Medic 1")

    def test_a_slow_computation_never_moves_the_stored_picture_backwards(self):
        newer = picture_at(T0 + timedelta(seconds=3))

        def slow(instant):
            # Another reader stored a later picture while this one was computing.
            self.store.put(ORG, _micros(newer.as_of), encode(newer))
            return picture_at(instant, "slow")

        mine = self.live(slow)
        self.assertEqual(mine.vehicles[0].display_name, "slow")  # this request still answered
        self.assertEqual(decode(self.store.get(ORG)[1]).as_of, newer.as_of)

    def test_the_lease_holder_computes_and_a_waiter_uses_its_picture(self):
        self.assertTrue(self.store.acquire(ORG, "other reader"))
        produced = picture_at(T0 + timedelta(seconds=1), "from the lease holder")
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 3:
                self.store.put(ORG, _micros(produced.as_of), encode(produced))

        compute = Computer()
        waiter = self.live(compute, sleep=sleep)
        self.assertEqual(compute.instants, [])
        self.assertEqual(waiter.vehicles[0].display_name, "from the lease holder")
        self.assertEqual(polls, [POLL_SECONDS] * 3)

    def test_a_waiter_computes_its_own_after_the_wait_and_ignores_too_old_pictures(self):
        self.assertTrue(self.store.acquire(ORG, "stuck reader"))
        old = picture_at(T0 - timedelta(seconds=16))
        self.store.put(ORG, _micros(old.as_of), encode(old))
        polls, compute = [], Computer()
        waiter = self.live(compute, sleep=polls.append)
        self.assertAlmostEqual(sum(polls), WAIT_SECONDS)
        self.assertEqual(compute.instants, [T0])
        self.assertEqual(waiter.as_of, T0)
        self.assertEqual(self.store.get(ORG)[0], _micros(T0))  # it stores its newer one

    def test_the_lease_is_released_after_computing_even_on_failure(self):
        def broken(instant):
            raise ValueError("read failed")

        with self.assertRaises(ValueError):
            self.live(broken)
        self.assertTrue(self.store.acquire(ORG, "next reader"))

    def test_an_expired_lease_is_taken_over(self):
        ticks = [0.0]
        store = LocalStore(clock=lambda: ticks[0])
        self.assertTrue(store.acquire(ORG, "abandoned"))
        self.assertFalse(store.acquire(ORG, "too soon"))
        ticks[0] = shared_picture.LEASE_SECONDS + 0.1
        self.assertTrue(store.acquire(ORG, "after expiry"))
        store.release(ORG, "abandoned")  # not its lease any more: nothing happens
        self.assertFalse(store.acquire(ORG, "still held"))

    def test_a_store_failure_falls_back_to_computing(self):
        compute = Computer()
        with self.assertLogs("cya.attention.shared_picture", "ERROR"):
            first = self.live(compute, store=FailingStore())
            second = self.live(compute, share=False, store=FailingStore())
        self.assertEqual(len(compute.instants), 2)
        self.assertEqual((first.as_of, second.as_of), (T0, T0))

    def test_an_unreadable_stored_picture_is_computed_again(self):
        self.store.put(ORG, _micros(T0), "{not json")
        compute = Computer()
        with self.assertLogs("cya.attention.shared_picture", "ERROR"):
            picture = self.live(compute)
        self.assertEqual(compute.instants, [T0])
        self.assertEqual(picture.as_of, T0)


@override_settings(CYA_READINESS_SHARED_PICTURE_SECONDS=15)
class SharedStoreTests(SimpleTestCase):
    """The shared store keeps the same rules for several processes."""

    def setUp(self):
        self.client = FakeRedis()
        self.store = SharedStore(self.client)
        self.clock = Clock()

    def test_two_processes_share_one_picture_and_never_go_backwards(self):
        compute = Computer()
        process_a, process_b = SharedStore(self.client), SharedStore(self.client)
        live_picture(ORG, compute, self.clock, share=False, store=process_a)
        self.clock.instant += timedelta(seconds=10)
        shared = live_picture(
            ORG, compute, self.clock, share=True, store=process_b, sleep=lambda s: None
        )
        self.assertEqual(compute.instants, [T0])
        self.assertEqual(shared.as_of, T0)
        process_b.put(ORG, _micros(T0 - timedelta(seconds=1)), encode(picture_at(T0)))
        self.assertEqual(process_a.get(ORG)[0], _micros(T0))

    def test_only_the_lease_holder_releases_its_lease(self):
        self.assertTrue(self.store.acquire(ORG, "a"))
        self.assertFalse(self.store.acquire(ORG, "b"))
        self.store.release(ORG, "b")
        self.assertFalse(self.store.acquire(ORG, "c"))
        self.store.release(ORG, "a")
        self.assertTrue(self.store.acquire(ORG, "c"))

    def test_keys_are_scoped_by_organization(self):
        self.store.put(ORG, _micros(T0), encode(picture_at(T0)))
        self.assertEqual(set(self.client.hashes), {f"cya:readiness:picture:{ORG}"})
        self.assertIsNone(self.store.get("00000000-0000-0000-0000-000000000002"))

    def test_a_corrupted_entry_is_computed_again_never_a_failed_request(self):
        good = encode(picture_at(T0)).encode()
        stale = encode(picture_at(T0 - timedelta(seconds=16))).encode()
        cases = {
            "instant is not a number": (b"soon", good),
            "picture is not UTF-8": (str(_micros(T0)).encode(), b"\xff\xfe\x00"),
            "picture is not JSON": (str(_micros(T0)).encode(), b"{not json"),
            "JSON scalar": (str(_micros(T0)).encode(), b"3"),
            "JSON list": (str(_micros(T0)).encode(), b"[]"),
            "vehicles not a list": (str(_micros(T0)).encode(), b'{"vehicles": 3}'),
            "fields missing": (str(_micros(T0)).encode(), b'{"vehicles": []}'),
            "unknown field": (
                str(_micros(T0)).encode(),
                good.replace(b'"truncated"', b'"surprise":1,"truncated"'),
            ),
            "instant not a datetime": (
                str(_micros(T0)).encode(),
                b'{"as_of":"2026","window_start":null,"window_end":null,'
                b'"vehicles":[],"truncated":false,"totals":{}}',
            ),
            "stored instant newer than the picture": (str(_micros(T0)).encode(), stale),
        }
        key = f"cya:readiness:picture:{ORG}"
        for name, (at, text) in cases.items():
            with self.subTest(name):
                self.client.hashes[key] = {"at": at, "picture": text}
                self.client.strings.clear()
                compute = Computer()
                with self.assertLogs("cya.attention.shared_picture", "ERROR") as logs:
                    picture = live_picture(
                        ORG,
                        compute,
                        self.clock,
                        share=True,
                        store=self.store,
                        sleep=lambda s: None,
                    )
                    shared_picture.logger.error("marker")  # assertLogs needs one for the age case
                self.assertEqual(compute.instants, [T0])
                self.assertEqual(picture.vehicles[0].display_name, "computed 1")
                if name != "stored instant newer than the picture":
                    self.assertIn("unreadable", logs.output[0])
