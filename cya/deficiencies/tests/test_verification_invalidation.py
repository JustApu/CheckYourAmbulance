"""Invalidating or correcting a verification check reopens the closure it supported (issue 155 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/155))."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from cya.checklists.models import Compartment
from cya.checks.commands import CheckError, correct_check, invalidate_chain
from cya.checks.models import CheckEvent, CheckRecord
from cya.core.models import CommandReceipt
from cya.deficiencies.closure import close_administrator
from cya.deficiencies.effects import decide_verification_reopen, supporting_deficiency_ids
from cya.deficiencies.models import DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.service import dependency_state
from cya.deficiencies.tests.test_verification import VerificationBase

ADMIN_REASON = "Synthetic invalidation reason"


class DecisionTests(SimpleTestCase):
    def test_only_a_lost_pass_of_the_closing_lineage_reopens_a_verified_closure(self):
        self.assertTrue(decide_verification_reopen("CLOSED_VERIFIED", True, False))
        # Another effective Pass still satisfies every row: the closure keeps its support.
        self.assertFalse(decide_verification_reopen("CLOSED_VERIFIED", True, True))
        # A Pass of another correction's revision never counted toward this closure.
        self.assertFalse(decide_verification_reopen("CLOSED_VERIFIED", False, False))
        # Not closed by verification: nothing to reopen (Pending Verification stays as it is).
        for status in ("PENDING_VERIFICATION", "IN_PROGRESS", "OPEN", "CLOSED_ADMINISTRATOR"):
            self.assertFalse(decide_verification_reopen(status, True, False))


class ReopenBase(VerificationBase):
    def invalidate(self, check_id, **context):
        return invalidate_chain(
            check_id=check_id,
            explanation=ADMIN_REASON,
            **{**self.context(self.admin), **context},
        )

    def withdraw(self, check_id, outcome="FAILED", **fields):
        context = self.context(self.crew)
        context.update(fields.pop("context", {}))
        return correct_check(
            check_id=check_id,
            expected_tip_check_id=check_id,
            outcome=outcome,
            **fields,
            **context,
        )

    def compartment(self):
        return Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )

    def closed(self):
        """An item deficiency closed by one verification Pass; returns (deficiency, check)."""
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        result = self.verify(deficiency_id)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        return deficiency_id, result.body["check_id"]

    def assert_reopened(self, deficiency_id, root_id, closed_event):
        reopen = self.last_event(deficiency_id)
        self.assertEqual(reopen.event_type, "VERIFICATION_REOPENED")
        self.assertEqual((reopen.from_status, reopen.to_status), ("CLOSED_VERIFIED", "OPEN"))
        self.assertEqual(str(reopen.verification_check_id), root_id)
        self.assertIsNone(reopen.correction_ref)
        self.assertEqual(reopen.predecessor_event_id, closed_event.pk)
        projection = DeficiencyProjection.objects.get(deficiency_id=deficiency_id)
        self.assertEqual((projection.status, projection.last_applied_event_id), ("OPEN", reopen.pk))
        return reopen


class InvalidationReopensTests(ReopenBase):
    def test_invalidating_the_closing_pass_reopens_in_the_same_command(self):
        deficiency_id, check_id = self.closed()
        closed_event = self.last_event(deficiency_id)
        result = self.invalidate(check_id)
        self.assertEqual(result.body["reopened_deficiency_ids"], [deficiency_id])
        reopen = self.assert_reopened(deficiency_id, check_id, closed_event)
        invalidation = CheckEvent.objects.get(root_check_id=check_id, event_type="INVALIDATION")
        # One command wrote both, at one instant (the same transaction).
        self.assertEqual(reopen.command_id, invalidation.command_id)
        self.assertEqual(reopen.recorded_at, invalidation.recorded_at)
        self.assertEqual(reopen.actor_user_id, self.admin.pk)
        # The invalid check is retained and nothing earlier revives (INV-2a).
        self.assertTrue(CheckRecord.objects.filter(pk=check_id, outcome="PASSED").exists())
        self.assertEqual(CheckRecord.objects.filter(root_check_id=check_id).count(), 1)
        receipt = CommandReceipt.objects.get(pk=invalidation.command_id)
        self.assertEqual(receipt.response_body["reopened_deficiency_ids"], [deficiency_id])

    def test_history_shows_the_closure_effective_before_the_invalidation(self):
        deficiency_id, check_id = self.closed()
        closed_at = self.last_event(deficiency_id).recorded_at
        self.invalidate(check_id)
        reopened_at = self.last_event(deficiency_id).recorded_at
        self.assertGreater(reopened_at, closed_at)
        between = closed_at + (reopened_at - closed_at) / 2
        before = dependency_state(str(self.org.pk), deficiency_id, as_of=between)
        self.assertEqual(before["deficiency_status"], "CLOSED_VERIFIED")
        now = dependency_state(str(self.org.pk), deficiency_id)
        # The projection is a rebuildable cache of the replayed status.
        self.assertEqual(now["deficiency_status"], "OPEN")
        self.assertEqual(now["deficiency_status"], self.status(deficiency_id))
        self.assertEqual(now["last_event_id"], str(self.last_event(deficiency_id).pk))

    def test_replay_returns_the_stored_result_and_writes_nothing(self):
        deficiency_id, check_id = self.closed()
        operation_id = str(uuid4())
        first = self.invalidate(check_id, operation_id=operation_id)
        counts = self.counts()
        again = self.invalidate(check_id, operation_id=operation_id)
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.events(deficiency_id).count("VERIFICATION_REOPENED"), 1)

    def test_an_independent_pass_of_the_same_row_keeps_the_closure(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        first = self.verify(deficiency_id, self.suction).body["check_id"]
        second = self.verify(deficiency_id, self.suction).body["check_id"]
        for item in (self.masks, self.gauge, self.special, self.limited):
            self.verify(deficiency_id, item)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        closed_event = self.last_event(deficiency_id)
        kept = self.invalidate(first)
        self.assertEqual(kept.body["reopened_deficiency_ids"], [])
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.assertEqual(self.last_event(deficiency_id).pk, closed_event.pk)
        # The last qualifying support is gone now: the closure reopens.
        lost = self.invalidate(second)
        self.assertEqual(lost.body["reopened_deficiency_ids"], [deficiency_id])
        self.assert_reopened(deficiency_id, second, closed_event)

    def test_losing_the_only_pass_of_an_earlier_row_also_reopens(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        first = self.verify(deficiency_id, self.suction).body["check_id"]
        for item in (self.masks, self.gauge, self.special, self.limited):
            self.verify(deficiency_id, item)
        closed_event = self.last_event(deficiency_id)
        self.invalidate(first)
        self.assert_reopened(deficiency_id, first, closed_event)

    def test_a_pass_or_failed_attempt_of_an_older_revision_does_not_reopen(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        old_pass = self.verify(deficiency_id, self.suction).body["check_id"]
        old_fail = self.verify(deficiency_id, self.masks, "FAILED").body["check_id"]
        self.assertEqual(self.status(deficiency_id), "IN_PROGRESS")
        self.correct(deficiency_id)  # a new scope revision
        for item in (self.suction, self.masks, self.gauge, self.special, self.limited):
            self.verify(deficiency_id, item)
        closed_event = self.last_event(deficiency_id)
        self.assertEqual(closed_event.event_type, "VERIFICATION_CLOSED")
        for check_id in (old_pass, old_fail):
            self.assertEqual(self.invalidate(check_id).body["reopened_deficiency_ids"], [])
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.assertEqual(self.last_event(deficiency_id).pk, closed_event.pk)

    def test_a_pending_deficiency_stays_pending_and_the_row_stops_counting(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        first = self.verify(deficiency_id, self.suction).body["check_id"]
        events = self.events(deficiency_id)
        result = self.invalidate(first)
        self.assertEqual(result.body["reopened_deficiency_ids"], [])
        self.assertEqual(self.events(deficiency_id), events)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        for item in (self.masks, self.gauge, self.special, self.limited):
            last = self.verify(deficiency_id, item)
        self.assertEqual(last.body["verification"]["remaining_scope_rows"], 1)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")

    def test_an_administrator_closed_deficiency_is_left_alone(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        first = self.verify(deficiency_id, self.suction).body["check_id"]
        close_administrator(
            deficiency_id=deficiency_id,
            explanation="Synthetic explicit closure",
            expected_event_id=str(self.last_event(deficiency_id).pk),
            **self.context(self.admin),
        )
        self.assertEqual(self.invalidate(first).body["reopened_deficiency_ids"], [])
        self.assertEqual(self.status(deficiency_id), "CLOSED_ADMINISTRATOR")


class CorrectionTests(ReopenBase):
    def test_withdrawing_the_closing_pass_reopens_and_replays(self):
        deficiency_id, check_id = self.closed()
        closed_event = self.last_event(deficiency_id)
        operation_id = str(uuid4())
        result = self.withdraw(check_id, context={"operation_id": operation_id})
        reopen = self.assert_reopened(deficiency_id, check_id, closed_event)
        self.assertEqual(result.body["deficiency_event_ids"], [str(reopen.pk)])
        self.assertEqual(
            result.body["deficiency_effects"][0]["event_type"], "VERIFICATION_REOPENED"
        )
        row = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertEqual((row.outcome, row.purpose), ("FAILED", "VERIFICATION"))
        self.assertEqual(str(row.verification_for_deficiency_id), deficiency_id)
        self.assertEqual(reopen.command_id, row.command_id)
        counts = self.counts()
        again = self.withdraw(check_id, context={"operation_id": operation_id})
        self.assertEqual((again.body, again.replayed), (result.body, True))
        self.assertEqual(self.counts(), counts)

    def test_unable_needs_a_reason_and_keeps_no_observation(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        masks = self.verify(deficiency_id, self.masks).body["check_id"]
        for item in (self.suction, self.gauge, self.special, self.limited):
            self.verify(deficiency_id, item)
        closed_event = self.last_event(deficiency_id)
        with self.assertRaises(CheckError) as caught:
            self.withdraw(masks, "UNABLE_TO_INSPECT")
        self.assertEqual(caught.exception.code, "EXPLANATION_REQUIRED")
        with self.assertRaises(CheckError) as caught:
            self.withdraw(masks, "UNABLE_TO_INSPECT", quantity=6, explanation="Synthetic reason")
        self.assertEqual(caught.exception.code, "INVALID_OBSERVATION")
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        result = self.withdraw(masks, "UNABLE_TO_INSPECT", explanation="Synthetic reason")
        row = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertEqual((row.outcome, row.quantity), ("UNABLE_TO_INSPECT", None))
        self.assert_reopened(deficiency_id, masks, closed_event)

    def test_a_failed_correction_keeps_the_observation_of_the_pass(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        gauge = self.verify(deficiency_id, self.gauge).body["check_id"]
        result = self.withdraw(gauge)
        row = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertEqual((row.measurement_value, row.measurement_unit), (40, "psi"))

    def test_a_pending_withdrawal_stays_pending_and_never_closes(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        suction = self.verify(deficiency_id, self.suction).body["check_id"]
        for item in (self.masks, self.gauge, self.special):
            self.verify(deficiency_id, item)
        events = self.events(deficiency_id)
        result = self.withdraw(suction)
        self.assertEqual(result.body["deficiency_event_ids"], [])
        self.assertEqual(self.events(deficiency_id), events)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        # The withdrawn row needs a new Pass: the last other item alone does not close it.
        self.assertEqual(
            self.verify(deficiency_id, self.limited).body["verification"]["remaining_scope_rows"], 1
        )
        self.verify(deficiency_id, self.suction)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_other_verification_corrections_are_still_refused(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        passed = self.verify(deficiency_id, self.suction).body["check_id"]
        failed = self.verify(deficiency_id, self.masks, "FAILED").body["check_id"]
        before = (CheckRecord.objects.count(), CommandReceipt.objects.count())
        for check_id, outcome in ((passed, "PASSED"), (failed, "PASSED"), (failed, "FAILED")):
            with self.assertRaises(CheckError) as caught:
                self.withdraw(check_id, outcome)
            self.assertEqual(caught.exception.code, "NOT_YET_SUPPORTED")
        withdrawn = self.withdraw(passed).body["check_id"]
        with self.assertRaises(CheckError) as caught:  # its tip is no longer a Pass
            correct_check(
                check_id=passed,
                expected_tip_check_id=withdrawn,
                outcome="PASSED",
                **self.context(self.crew),
            )
        self.assertEqual(caught.exception.code, "NOT_YET_SUPPORTED")
        self.assertEqual(
            (CheckRecord.objects.count(), CommandReceipt.objects.count()),
            (before[0] + 1, before[1] + 1),
        )

    def test_an_outcome_the_item_does_not_allow_is_refused(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        limited = self.verify(deficiency_id, self.limited).body["check_id"]
        with self.assertRaises(CheckError) as caught:
            self.withdraw(limited, "UNABLE_TO_INSPECT", explanation="Synthetic reason")
        self.assertEqual(caught.exception.code, "INVALID_OUTCOME")

    def test_only_the_author_may_withdraw_a_pass(self):
        deficiency_id, check_id = self.closed()
        with self.assertRaises(CheckError) as caught:
            correct_check(
                check_id=check_id,
                expected_tip_check_id=check_id,
                outcome="FAILED",
                **self.context(self.supervisor),
            )
        self.assertEqual(caught.exception.code, "AUTHOR_REQUIRED")
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")


class LockingAndAtomicityTests(ReopenBase):
    def test_the_lock_set_is_the_sorted_union_of_support_and_target(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        failed = self.verify(deficiency_id, outcome="FAILED").body["check_id"]
        other = self.report({"item_id": str(self.masks.pk)})
        # A Failed verification is evidence for its own deficiency only: the union has it once.
        self.assertEqual(
            supporting_deficiency_ids(str(self.org.pk), failed, deficiency_id), [deficiency_id]
        )
        self.assertEqual(
            supporting_deficiency_ids(str(self.org.pk), failed, other),
            sorted([deficiency_id, other]),
        )

    def test_a_failure_while_reopening_rolls_back_the_whole_invalidation(self):
        deficiency_id, check_id = self.closed()
        counts = self.counts()
        events = CheckEvent.objects.count()
        operation_id = str(uuid4())
        with (
            # The reopening event is the last write, after the check's INVALIDATION event.
            mock.patch(
                "cya.deficiencies.effects.DeficiencyEvent.objects.create",
                side_effect=DatabaseError("boom"),
            ),
            self.assertRaises(DatabaseError),
        ):
            self.invalidate(check_id, operation_id=operation_id)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(CheckEvent.objects.count(), events)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        retry = self.invalidate(check_id, operation_id=operation_id)
        self.assertFalse(retry.replayed)
        self.assertEqual(self.status(deficiency_id), "OPEN")

    def test_a_failure_while_reopening_rolls_back_the_whole_correction(self):
        deficiency_id, check_id = self.closed()
        counts = self.counts()
        checks = CheckRecord.objects.count()
        with (
            mock.patch("cya.deficiencies.effects.append_effect", side_effect=DatabaseError("boom")),
            self.assertRaises(DatabaseError),
        ):
            self.withdraw(check_id)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(CheckRecord.objects.count(), checks)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def run_together(self, *jobs):
        barrier = Barrier(len(jobs))

        def run(job):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return job()
            except Exception as error:  # returned to the asserting thread
                return error
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            return [f.result(timeout=120) for f in [pool.submit(run, j) for j in jobs]]

    def assert_unforked(self, deficiency_id):
        rows = list(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id)
            .order_by("sequence")
            .values_list("sequence", flat=True)
        )
        self.assertEqual(rows, list(range(1, len(rows) + 1)))

    def test_concurrent_correction_and_invalidation_of_one_chain_reopen_once(self):
        deficiency_id, check_id = self.closed()
        outcomes = self.run_together(
            lambda: self.withdraw(check_id), lambda: self.invalidate(check_id)
        )
        refused = [o for o in outcomes if isinstance(o, Exception)]
        # Correction after invalidation is refused; invalidation after correction succeeds.
        self.assertTrue(all(getattr(o, "code", None) == "CHAIN_INVALIDATED" for o in refused))
        self.assertLessEqual(len(refused), 1, outcomes)
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(self.events(deficiency_id).count("VERIFICATION_REOPENED"), 1)
        self.assert_unforked(deficiency_id)

    def test_a_concurrent_last_pass_and_invalidation_never_leave_it_closed(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        first = self.verify(deficiency_id, self.suction).body["check_id"]
        for item in (self.masks, self.gauge, self.special):
            self.verify(deficiency_id, item)
        outcomes = self.run_together(
            lambda: self.verify(deficiency_id, self.limited), lambda: self.invalidate(first)
        )
        self.assertEqual([o for o in outcomes if isinstance(o, Exception)], [])
        # Either order: the invalidated row is unsatisfied, so the deficiency is not closed.
        self.assertIn(self.status(deficiency_id), ("PENDING_VERIFICATION", "OPEN"))
        kinds = [e for e in self.events(deficiency_id) if e.startswith("VERIFICATION_")]
        self.assertIn(kinds, ([], ["VERIFICATION_CLOSED", "VERIFICATION_REOPENED"]))
        self.assert_unforked(deficiency_id)


class DatabaseGuardTests(ReopenBase):
    def forge(self, deficiency_id, check_id, command, **overrides):
        last = self.last_event(deficiency_id)
        fields = {
            "organization_id": self.org.pk,
            "deficiency_id": deficiency_id,
            "sequence": last.sequence + 1,
            "predecessor_event": last,
            "event_type": "VERIFICATION_REOPENED",
            "from_status": "CLOSED_VERIFIED",
            "to_status": "OPEN",
            "explanation": "Synthetic reopening",
            "verification_check_id": check_id,
            "command": command,
            "recorded_at": command.recorded_at,
            "actor_user_id": command.actor_user_id,
            "actor_roles_at_time": ["ADMINISTRATOR"],
            **overrides,
        }
        return DeficiencyEvent.objects.create(**fields)

    def refused(self, operation, text=None):
        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            operation()
        if text:
            self.assertIn(text, str(caught.exception))

    def test_the_database_refuses_a_reopening_while_the_closure_keeps_its_support(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.verify(deficiency_id)
        # Invalidate a chain that never supported the closure: a routine Pass of the item.
        routine = self.record(item=self.suction).body["check_id"]
        self.invalidate(routine)
        invalidation = CheckEvent.objects.get(root_check_id=routine, event_type="INVALIDATION")
        receipt = CommandReceipt.objects.get(pk=invalidation.command_id)
        verification = CheckRecord.objects.get(purpose="VERIFICATION")
        # The named chain was not invalidated by this command.
        self.refused(
            lambda: self.forge(deficiency_id, verification.pk, receipt),
            "lost Pass of the closing revision",
        )
        # A routine chain is not a verification chain of this deficiency.
        self.refused(
            lambda: self.forge(deficiency_id, routine, receipt),
            "lost Pass of the closing revision",
        )
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_the_database_refuses_a_reopening_when_another_pass_still_satisfies(self):
        deficiency_id = self.pending({"compartment_id": str(self.compartment().pk)})
        first = self.verify(deficiency_id, self.suction).body["check_id"]
        self.verify(deficiency_id, self.suction)
        for item in (self.masks, self.gauge, self.special, self.limited):
            self.verify(deficiency_id, item)
        self.invalidate(first)  # the second Pass keeps the closure
        invalidation = CheckEvent.objects.get(root_check_id=first, event_type="INVALIDATION")
        receipt = CommandReceipt.objects.get(pk=invalidation.command_id)
        self.refused(
            lambda: self.forge(deficiency_id, first, receipt), "lost Pass of the closing revision"
        )
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_the_shape_constraint_refuses_extra_fields_on_a_real_reopening(self):
        deficiency_id, check_id = self.closed()
        create = DeficiencyEvent.objects.create
        cases = (
            ({"correction_ref": check_id}, "defevent_verification_reopen_shape"),
            ({"explanation": None}, "defevent_verification_reopen_shape"),
            ({"to_status": "IN_PROGRESS"}, "lost Pass of the closing revision"),
            ({"verification_check_id": None}, "lost Pass of the closing revision"),
        )
        for overrides, text in cases:

            def tampered(overrides=overrides, **fields):
                return create(**{**fields, **overrides})

            with (
                self.subTest(overrides=overrides),
                mock.patch(
                    "cya.deficiencies.effects.DeficiencyEvent.objects.create", side_effect=tampered
                ),
                self.assertRaises(DatabaseError) as caught,
            ):
                self.invalidate(check_id)
            self.assertIn(text, str(caught.exception))
            self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.invalidate(check_id)  # untampered, the same reopening is accepted
        self.assertEqual(self.status(deficiency_id), "OPEN")


class MigrationTests(TransactionTestCase):
    def trigger(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='cya_deficiency_event_bi'"
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_reversal_restores_the_previous_trigger_and_forward_reinstalls_the_guard(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        before = self.trigger()
        self.assertIn("VERIFICATION_REOPENED", before)
        target = [
            ("cya_deficiencies", "0014_verification_events") if app == "cya_deficiencies" else leaf
            for leaf in leaves
            for app in [leaf[0]]
        ]
        try:
            executor.migrate(target)
            self.assertNotIn("VERIFICATION_REOPENED", self.trigger())
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(before, self.trigger())
        finally:
            MigrationExecutor(connection).migrate(leaves)
