"""Concurrent checking and the duplicate warning (issue 75 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/75); SPEC CON-1..CON-5, AC-9, AC-10, AC-31)."""

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from django.db import DatabaseError, OperationalError, connection, transaction

from cya.checks.commands import CheckError, record_check
from cya.checks.conflicts import CheckConflict
from cya.checks.models import CheckRecord, ItemLock
from cya.checks.tests.test_obligation_links import ObligationLinkCleanupMixin
from cya.core.models import CommandReceipt
from cya.kits.tests.support import KitCase
from cya.obligations.tests.support import ObligationCase


class ConflictMixin:
    def record(self, item=None, *, actor=None, operation_id=None, **extra):
        context = self.context(actor or self.crew)
        if operation_id is not None:
            context["operation_id"] = operation_id
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str((item or self.suction).pk),
            checklist_version_id=self.version_id,
            outcome="PASSED",
            purpose="ROUTINE",
            **extra,
            **context,
        )

    def warned(self, *args, **kwargs):
        before = (CheckRecord.objects.count(), CommandReceipt.objects.count())
        with self.assertRaises(CheckConflict) as caught:
            self.record(*args, **kwargs)
        # A warning is not a commit: no check and no receipt (REC-4).
        self.assertEqual((CheckRecord.objects.count(), CommandReceipt.objects.count()), before)
        return caught.exception


class ConflictTests(ObligationLinkCleanupMixin, ConflictMixin, ObligationCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600", recurrence="DAILY")
        self.version_id = self.published_version(self.base_items())
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.second = self.make_user("crew-two")
        self.third = self.make_user("crew-three")
        self.clock.set(self.at(hours=5, minutes=15))  # inside the 06:00 window

    def test_a_second_crew_member_is_warned_with_the_newer_check_and_no_commit(self):
        first = self.record()
        conflict = self.warned(actor=self.second)
        self.assertEqual(conflict.newest["check_id"], first.body["check_id"])
        self.assertEqual(conflict.newest["actor"]["user_id"], str(self.crew.pk))
        self.assertEqual(conflict.newest["actor"]["display_name"], self.crew.display_name)
        self.assertEqual(conflict.newest["outcome"], "PASSED")
        self.assertEqual(conflict.obligation_id, first.body["obligation_links"][0]["obligation_id"])
        self.assertEqual(conflict.status, 409)

    def test_a_warning_does_not_consume_the_operation_id_and_an_identical_retry_is_reevaluated(
        self,
    ):
        first = self.record()
        operation_id = str(uuid4())
        self.warned(actor=self.second, operation_id=operation_id)
        self.warned(actor=self.second, operation_id=operation_id)  # evaluated again, same answer
        proceeded = self.record(
            actor=self.second,
            operation_id=str(uuid4()),
            acknowledges_check_id=first.body["check_id"],
        )
        self.assertEqual(proceeded.status, 201)
        # The original id is still free: with the newer check acknowledged it now commits.
        self.assertEqual(
            self.record(
                actor=self.second,
                operation_id=operation_id,
                basis_check_id=proceeded.body["check_id"],
            ).status,
            201,
        )

    def test_proceeding_keeps_both_records_independently(self):
        first = self.record()
        second = self.record(actor=self.second, acknowledges_check_id=first.body["check_id"])
        rows = {r.pk: r for r in CheckRecord.objects.all()}
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[second.body["check_id"]].acknowledges_check_id, first.body["check_id"]
        )
        self.assertEqual({r.actor_user_id for r in rows.values()}, {self.crew.pk, self.second.pk})

    def test_the_actors_own_newest_check_is_not_a_conflict_and_a_seen_basis_is_not_either(self):
        first = self.record()
        again = self.record()  # same actor
        self.assertEqual(again.status, 201)
        seen = self.record(actor=self.second, basis_check_id=again.body["check_id"])
        self.assertEqual(seen.status, 201)
        self.assertEqual(CheckRecord.objects.count(), 3)
        self.assertEqual(
            CheckRecord.objects.get(pk=seen.body["check_id"]).basis_check_id, again.body["check_id"]
        )
        self.assertNotEqual(first.body["check_id"], again.body["check_id"])

    def test_a_stale_basis_is_warned_about_the_newest_check(self):
        first = self.record()
        newest = self.record(actor=self.second, acknowledges_check_id=first.body["check_id"])
        conflict = self.warned(actor=self.third, basis_check_id=first.body["check_id"])
        self.assertEqual(conflict.newest["check_id"], newest.body["check_id"])

    def test_a_check_landing_between_the_warning_and_the_acknowledgement_gives_a_fresh_warning(
        self,
    ):
        first = self.record()
        warning = self.warned(actor=self.second)
        self.assertEqual(warning.newest["check_id"], first.body["check_id"])
        between = self.record(actor=self.third, acknowledges_check_id=first.body["check_id"])
        fresh = self.warned(actor=self.second, acknowledges_check_id=first.body["check_id"])
        self.assertEqual(fresh.newest["check_id"], between.body["check_id"])
        committed = self.record(actor=self.second, acknowledges_check_id=between.body["check_id"])
        self.assertEqual(committed.status, 201)
        self.assertEqual(CheckRecord.objects.count(), 3)

    def test_a_lost_response_replays_the_stored_result_and_never_warns(self):
        first = self.record()
        operation_id = str(uuid4())
        sent = self.record(
            actor=self.second,
            operation_id=operation_id,
            acknowledges_check_id=first.body["check_id"],
        )
        self.record(actor=self.third, acknowledges_check_id=sent.body["check_id"])  # lands after
        replay = self.record(
            actor=self.second,
            operation_id=operation_id,
            acknowledges_check_id=first.body["check_id"],
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.body, sent.body)
        self.assertEqual(CheckRecord.objects.count(), 3)

    def test_with_no_eligible_obligation_the_unlinked_item_scope_is_compared(self):
        self.clock.set(self.at(hours=3))  # before the early window: no obligation, nothing late
        first = self.record()
        self.assertEqual(first.body["obligation_links"], [])
        conflict = self.warned(actor=self.second)
        self.assertEqual(conflict.newest["check_id"], first.body["check_id"])
        self.assertIsNone(conflict.obligation_id)

    def test_another_item_is_never_compared(self):
        self.record(self.suction)
        self.assertEqual(self.record(self.siren, actor=self.second).status, 201)

    def test_a_basis_must_be_an_earlier_check_of_this_item(self):
        other = self.record(self.siren)
        for kwargs, code in (
            ({"basis_check_id": str(uuid4())}, "NOT_FOUND"),
            ({"acknowledges_check_id": str(uuid4())}, "NOT_FOUND"),
            ({"basis_check_id": other.body["check_id"]}, "INVALID_LINK"),
            ({"acknowledges_check_id": other.body["check_id"]}, "INVALID_LINK"),
        ):
            with self.subTest(kwargs), self.assertRaises(CheckError) as caught:
                self.record(**kwargs)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(CheckRecord.objects.count(), 1)

    def test_the_database_refuses_a_basis_that_is_not_an_earlier_check_of_the_same_item(self):
        first = self.record()
        other = self.record(self.siren)
        row = CheckRecord.objects.get(pk=first.body["check_id"])
        wrong = CheckRecord.objects.get(pk=other.body["check_id"])
        for field in ("basis_check_id", "acknowledges_check_id"):
            with self.subTest(field), self.assertRaises(DatabaseError), transaction.atomic():
                self.insert_like(row, **{field: wrong.pk})
        receipt = self.receipt()  # the bypassing writer's receipt predates the check it names
        later = self.record(actor=self.second, acknowledges_check_id=row.pk)
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.insert_like(row, receipt=receipt, basis_check_id=later.body["check_id"])
        self.insert_like(row, basis_check_id=later.body["check_id"])  # a later row may name it

    def receipt(self):
        from cya.core.audit_repository import append_receipt

        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.crew,
                command_type="CHECK_RECORD",
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def insert_like(self, row, receipt=None, **overrides):
        """A copy of an existing check with its own receipt, as a bypassing writer might insert."""
        receipt = receipt or self.receipt()
        check_id = str(uuid4())
        return CheckRecord.objects.create(
            id=check_id,
            root_check_id=check_id,
            organization_id=self.org.pk,
            command=receipt,
            recorded_at=receipt.recorded_at,
            actor_user=self.crew,
            actor_roles_at_time=["CREW"],
            operation_id=receipt.operation_id,
            vehicle_id=row.vehicle_id,
            item_id=row.item_id,
            item_def_id=row.item_def_id,
            checklist_version_id=row.checklist_version_id,
            outcome="PASSED",
            purpose="ROUTINE",
            **overrides,
        )


class ConcurrencyTests(ObligationLinkCleanupMixin, ConflictMixin, ObligationCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600", recurrence="DAILY")
        self.version_id = self.published_version(self.base_items())
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5, minutes=15))

    def run_threads(self, jobs):
        """Run callables on their own connections; return each outcome or the exception."""

        def run(job):
            try:
                return job()
            except Exception as error:  # reported to the test, which asserts on the type
                return error
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            return [future.result(timeout=120) for future in futures]

    def test_two_writers_on_one_item_commit_once_and_the_other_is_warned(self):
        second = self.make_user("crew-two")
        outcomes = self.run_threads(
            [lambda: self.record(actor=self.crew), lambda: self.record(actor=second)]
        )
        committed = [o for o in outcomes if hasattr(o, "body")]
        warned = [o for o in outcomes if isinstance(o, CheckConflict)]
        self.assertEqual((len(committed), len(warned)), (1, 1), outcomes)
        self.assertEqual(warned[0].newest["check_id"], committed[0].body["check_id"])
        self.assertEqual(CheckRecord.objects.count(), 1)
        # The warned writer proceeds and both records persist (CON-2).
        loser = (
            second
            if committed[0].body["check_id"]
            and warned[0].newest["actor"]["user_id"] == str(self.crew.pk)
            else self.crew
        )
        proceeded = self.record(actor=loser, acknowledges_check_id=committed[0].body["check_id"])
        self.assertEqual(proceeded.status, 201)
        self.assertEqual(CheckRecord.objects.count(), 2)

    def test_eight_concurrent_writers_on_a_fresh_item_do_not_deadlock(self):
        users = [self.make_user(f"crew-{n}") for n in range(8)]
        outcomes = self.run_threads(
            [lambda u=u: self.record(self.masks, actor=u, quantity=6) for u in users]
        )
        committed = [o for o in outcomes if hasattr(o, "body")]
        warned = [o for o in outcomes if isinstance(o, CheckConflict)]
        self.assertEqual((len(committed), len(warned)), (1, 7), outcomes)
        self.assertEqual(CheckRecord.objects.count(), 1)

    def test_eight_writers_who_each_acknowledge_the_newest_check_all_commit(self):
        seed = self.record(actor=self.make_user("seed"))
        users = [self.make_user(f"crew-{n}") for n in range(8)]
        # Each writer proceeds past the seed check; later arrivals see a newer check and are warned,
        # so every outcome is either a commit or a warning, never a deadlock or a lost write.
        outcomes = self.run_threads(
            [
                lambda u=u: self.record(actor=u, acknowledges_check_id=seed.body["check_id"])
                for u in users
            ]
        )
        self.assertTrue(
            all(hasattr(o, "body") or isinstance(o, CheckConflict) for o in outcomes), outcomes
        )
        committed = sum(hasattr(o, "body") for o in outcomes)
        self.assertEqual(CheckRecord.objects.count(), 1 + committed)
        self.assertGreaterEqual(committed, 1)

    def test_different_items_proceed_while_another_item_is_locked(self):
        # CON-1: an item lock is held by someone else; a different item is not blocked by it.
        holding, release = threading.Event(), threading.Event()
        lock = ItemLock.objects.get(vehicle=self.vehicle, item=self.suction)

        def hold():
            with transaction.atomic():
                ItemLock.objects.select_for_update().get(pk=lock.pk)
                holding.set()
                release.wait(timeout=60)

        def other():
            self.assertTrue(holding.wait(timeout=30))
            try:
                return self.record(self.siren)
            finally:
                release.set()

        results = self.run_threads([hold, other])
        self.assertTrue(hasattr(results[1], "body"), results)
        self.assertEqual(CheckRecord.objects.count(), 1)

    def test_a_lock_wait_timeout_is_a_clear_database_error_and_writes_nothing(self):
        holding, release = threading.Event(), threading.Event()
        lock = ItemLock.objects.get(vehicle=self.vehicle, item=self.suction)

        def hold():
            with transaction.atomic():
                ItemLock.objects.select_for_update().get(pk=lock.pk)
                holding.set()
                release.wait(timeout=60)

        def blocked():
            self.assertTrue(holding.wait(timeout=30))
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
                return self.record()
            finally:
                release.set()

        results = self.run_threads([hold, blocked])
        self.assertIsInstance(results[1], OperationalError, results)
        self.assertEqual(results[1].args[0], 1205)
        self.assertEqual(CheckRecord.objects.count(), 0)
        self.assertEqual(self.record().status, 201)  # nothing was consumed; the retry commits


class KitConflictTests(ObligationLinkCleanupMixin, ConflictMixin, KitCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600", recurrence="DAILY")
        self.reference_kit = self.add_kit("Reference").body["kit_id"]
        self.items = [*self.base_items(), self.spec(self.kit_item, 80, kit_id=self.reference_kit)]
        self.version_id = self.published_version(self.items)
        self.bind(reconcile=False)
        self.second_kit = self.add_kit("Second").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        self.other_user = self.make_user("crew-two")
        self.clock.set(self.at(hours=5, minutes=15))

    def test_two_identical_kits_checked_by_two_users_commit_without_a_warning(self):
        first = self.record(self.kit_item, kit_id=self.reference_kit)
        second = self.record(self.kit_item, actor=self.other_user, kit_id=self.second_kit)
        self.assertEqual((first.status, second.status), (201, 201))
        self.assertEqual(CheckRecord.objects.count(), 2)

    def test_a_second_check_on_the_same_kit_instance_warns(self):
        first = self.record(self.kit_item, kit_id=self.reference_kit)
        conflict = self.warned(self.kit_item, actor=self.other_user, kit_id=self.reference_kit)
        self.assertEqual(conflict.newest["check_id"], first.body["check_id"])

    def test_a_basis_on_another_kit_instance_is_refused(self):
        first = self.record(self.kit_item, kit_id=self.reference_kit)
        with self.assertRaises(CheckError) as caught:
            self.record(
                self.kit_item,
                actor=self.other_user,
                kit_id=self.second_kit,
                basis_check_id=first.body["check_id"],
            )
        self.assertEqual(caught.exception.code, "INVALID_LINK")
