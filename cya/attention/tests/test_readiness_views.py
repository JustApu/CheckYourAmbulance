"""Readiness endpoint coordination for overlapping current reads."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from unittest import mock

from django.test import SimpleTestCase

from cya.attention import readiness_views


class CurrentReadSingleFlightTests(SimpleTestCase):
    def setUp(self):
        with readiness_views._CURRENT_READS_LOCK:
            readiness_views._CURRENT_READS.clear()

    def tearDown(self):
        with readiness_views._CURRENT_READS_LOCK:
            readiness_views._CURRENT_READS.clear()

    def test_overlapping_current_reads_share_only_the_in_flight_picture(self):
        entered = Event()
        release = Event()
        waiter = Event()
        picture = object()
        user = mock.Mock(organization_id="org-1")

        def compute(organization_id, *, now):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return picture

        original_result = Future.result

        def wait_for_result(future, *args, **kwargs):
            waiter.set()
            return original_result(future, *args, **kwargs)

        with (
            mock.patch.object(readiness_views, "fleet_readiness", side_effect=compute) as read,
            mock.patch.object(Future, "result", wait_for_result),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(readiness_views._picture, user, None, consumer="screen")
            self.assertTrue(entered.wait(timeout=2))
            second = pool.submit(readiness_views._picture, user, None, consumer="screen")
            self.assertTrue(waiter.wait(timeout=2))
            release.set()
            self.assertIs(first.result(timeout=2), picture)
            self.assertIs(second.result(timeout=2), picture)
            read.assert_called_once()

        later = object()
        with mock.patch.object(readiness_views, "fleet_readiness", return_value=later) as read:
            self.assertIs(readiness_views._picture(user, None, consumer="screen"), later)
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
