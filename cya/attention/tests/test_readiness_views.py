"""Readiness endpoint coordination for overlapping current reads."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest import mock

from django.test import SimpleTestCase

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
