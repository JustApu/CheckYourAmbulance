"""Readiness endpoint coordination for overlapping current reads."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest import mock

from django.db import OperationalError
from django.test import RequestFactory, SimpleTestCase

from cya.attention import readiness_views


class CurrentReadSingleFlightTests(SimpleTestCase):
    def setUp(self):
        with readiness_views._CURRENT_READS_CHANGED:
            readiness_views._CURRENT_READS.clear()

    def tearDown(self):
        with readiness_views._CURRENT_READS_CHANGED:
            readiness_views._CURRENT_READS.clear()

    def test_overlapping_current_reads_share_a_follow_up_started_after_the_follower(self):
        leader_entered = Event()
        followers_waiting = Event()
        release_leader = Event()
        wait_count_lock = Lock()
        wait_count = 0
        version = {"value": 0}
        calls = 0
        user = mock.Mock(organization_id="org-1")

        def compute(organization_id, *, now):
            nonlocal calls
            calls += 1
            captured = version["value"]
            if calls == 1:
                leader_entered.set()
                self.assertTrue(release_leader.wait(timeout=2))
            return captured

        original_wait = readiness_views._CURRENT_READS_CHANGED.wait

        def wait_for_next_generation(*args, **kwargs):
            nonlocal wait_count
            with wait_count_lock:
                wait_count += 1
                if wait_count == 2:
                    followers_waiting.set()
            return original_wait(*args, **kwargs)

        with (
            mock.patch.object(readiness_views, "fleet_readiness", side_effect=compute) as read,
            mock.patch.object(
                readiness_views._CURRENT_READS_CHANGED, "wait", side_effect=wait_for_next_generation
            ),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            first = pool.submit(readiness_views._picture, user, None, consumer="screen")
            self.assertTrue(leader_entered.wait(timeout=2))
            second = pool.submit(readiness_views._picture, user, None, consumer="screen")
            third = pool.submit(readiness_views._picture, user, None, consumer="screen")
            self.assertTrue(followers_waiting.wait(timeout=2))
            version["value"] = 1  # a save commits after the leader captured its picture
            release_leader.set()
            self.assertEqual(first.result(timeout=2), 0)
            self.assertEqual(second.result(timeout=2), 1)
            self.assertEqual(third.result(timeout=2), 1)
            self.assertEqual(read.call_count, 2)

        version["value"] = 2
        later = 2
        with mock.patch.object(readiness_views, "fleet_readiness", return_value=later) as read:
            self.assertEqual(readiness_views._picture(user, None, consumer="screen"), later)
            read.assert_called_once()

    def test_historical_reads_do_not_join_or_retain_current_work(self):
        user = mock.Mock(organization_id="org-1")
        instant = object()
        picture = object()
        with mock.patch.object(readiness_views, "fleet_readiness", return_value=picture) as read:
            self.assertIs(readiness_views._picture(user, instant, consumer="screen"), picture)
        read.assert_called_once_with("org-1", as_of=instant, now=mock.ANY)

    def test_failed_current_read_is_removed_for_a_later_retry(self):
        user = mock.Mock(organization_id="org-1")
        with mock.patch.object(
            readiness_views, "fleet_readiness", side_effect=RuntimeError("synthetic failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                readiness_views._picture(user, None, consumer="screen")

        picture = object()
        with mock.patch.object(readiness_views, "fleet_readiness", return_value=picture):
            self.assertIs(readiness_views._picture(user, None, consumer="screen"), picture)

    def test_failed_queued_generation_reaches_followers_and_allows_a_retry(self):
        leader_entered = Event()
        followers_waiting = Event()
        release_leader = Event()
        wait_count_lock = Lock()
        wait_count = 0
        calls = 0
        user = mock.Mock(organization_id="org-1")

        def compute(organization_id, *, now):
            nonlocal calls
            calls += 1
            if calls == 1:
                leader_entered.set()
                self.assertTrue(release_leader.wait(timeout=2))
                return "leader"
            if calls == 2:
                raise OperationalError("synthetic queued failure")
            return "retry"

        original_wait = readiness_views._CURRENT_READS_CHANGED.wait

        def wait_for_followers(*args, **kwargs):
            nonlocal wait_count
            with wait_count_lock:
                wait_count += 1
                if wait_count == 2:
                    followers_waiting.set()
            return original_wait(*args, **kwargs)

        with (
            mock.patch.object(readiness_views, "fleet_readiness", side_effect=compute),
            mock.patch.object(
                readiness_views._CURRENT_READS_CHANGED, "wait", side_effect=wait_for_followers
            ),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            leader = pool.submit(readiness_views._picture, user, None, consumer="screen")
            self.assertTrue(leader_entered.wait(timeout=2))
            follower_a = pool.submit(readiness_views._picture, user, None, consumer="screen")
            follower_b = pool.submit(readiness_views._picture, user, None, consumer="screen")
            self.assertTrue(followers_waiting.wait(timeout=2))
            release_leader.set()
            self.assertEqual(leader.result(timeout=2), "leader")
            for follower in (follower_a, follower_b):
                with self.assertRaisesRegex(OperationalError, "synthetic queued failure"):
                    follower.result(timeout=2)

        with mock.patch.object(readiness_views, "fleet_readiness", side_effect=compute) as read:
            self.assertEqual(readiness_views._picture(user, None, consumer="screen"), "retry")
            read.assert_called_once()

    def test_different_organization_and_consumer_keys_run_independently(self):
        first_entered = Event()
        release_first = Event()
        first_user = mock.Mock(organization_id="org-1")
        second_user = mock.Mock(organization_id="org-2")
        calls = []

        def compute(organization_id, *, now):
            calls.append(organization_id)
            if len(calls) == 1:
                first_entered.set()
                self.assertTrue(release_first.wait(timeout=2))
            return organization_id

        with (
            mock.patch.object(readiness_views, "fleet_readiness", side_effect=compute),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            first = pool.submit(readiness_views._picture, first_user, None, consumer="screen")
            self.assertTrue(first_entered.wait(timeout=2))
            other_organization = pool.submit(
                readiness_views._picture, second_user, None, consumer="screen"
            )
            other_consumer = pool.submit(readiness_views._picture, first_user, None, consumer="api")
            self.assertEqual(other_organization.result(timeout=2), "org-2")
            self.assertEqual(other_consumer.result(timeout=2), "org-1")
            self.assertFalse(first.done())
            release_first.set()
            self.assertEqual(first.result(timeout=2), "org-1")

        self.assertCountEqual(calls, ["org-1", "org-2", "org-1"])

    def test_wait_timeout_fails_retryably_and_cleans_the_queued_generation(self):
        leader_entered = Event()
        follower_waiting = Event()
        release_leader = Event()
        user = mock.Mock(organization_id="org-1")

        def compute(organization_id, *, now):
            leader_entered.set()
            self.assertTrue(release_leader.wait(timeout=2))
            return "leader"

        original_wait = readiness_views._CURRENT_READS_CHANGED.wait

        def observe_wait(*args, **kwargs):
            follower_waiting.set()
            return original_wait(*args, **kwargs)

        with (
            mock.patch.object(readiness_views, "COALESCED_READ_WAIT_SECONDS", 0.01),
            mock.patch.object(readiness_views, "fleet_readiness", side_effect=compute),
            mock.patch.object(
                readiness_views._CURRENT_READS_CHANGED, "wait", side_effect=observe_wait
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            leader = pool.submit(readiness_views._picture, user, None, consumer="screen")
            self.assertTrue(leader_entered.wait(timeout=2))
            follower = pool.submit(readiness_views._picture, user, None, consumer="screen")
            self.assertTrue(follower_waiting.wait(timeout=2))
            with self.assertRaises(readiness_views.ReadinessWaitExpired):
                follower.result(timeout=2)
            release_leader.set()
            self.assertEqual(leader.result(timeout=2), "leader")

        self.assertEqual(readiness_views._CURRENT_READS, {})

    def test_wait_timeout_returns_retryable_api_response(self):
        request = RequestFactory().get("/api/v1/fleet-readiness")
        request.cya_user = mock.Mock(organization_id="org-1")
        with (
            mock.patch.object(readiness_views, "roles_for", return_value={"SUPERVISOR"}),
            mock.patch.object(
                readiness_views, "_picture", side_effect=readiness_views.ReadinessWaitExpired
            ),
        ):
            response = readiness_views.api(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["code"], "RETRY_LATER")
