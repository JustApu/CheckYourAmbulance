"""Verification checks count toward scope and close or return a deficiency (issue 154 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/154))."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from cya.checklists.models import Compartment, VehicleSystem
from cya.checks.commands import (
    CheckError,
    _satisfied_scope_rows,
    correct_check,
    invalidate_chain,
)
from cya.checks.models import CheckRecord
from cya.core.audit_repository import append_receipt
from cya.core.models import CommandReceipt
from cya.deficiencies.closure import close_administrator
from cya.deficiencies.commands import record_correction, report_deficiency, start_work
from cya.deficiencies.models import (
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyProjection,
    VerificationScopeRow,
)
from cya.deficiencies.tests.support import DeficiencyCase
from cya.kits.commands import add_kit_instance

NO_KIT = "00000000-0000-0000-0000-000000000000"


class VerificationMigrationTests(TransactionTestCase):
    def trigger(self, name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s",
                [name],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_reversal_restores_the_previous_triggers_and_forward_reinstalls_the_guards(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        before = (
            self.trigger("cya_deficiency_event_bi"),
            self.trigger("cya_check_record_bi"),
        )
        older = {
            "cya_deficiencies": "0013_empty_scope_closure",
            "cya_checks": "0009_unable_check_chain_effects",
        }
        target = [
            (app, older.get(app, name)) if app in older else (app, name) for app, name in leaves
        ]
        try:
            executor.migrate(target)
            self.assertNotIn("VERIFICATION_CLOSED", self.trigger("cya_deficiency_event_bi"))
            self.assertNotIn("verification", self.trigger("cya_check_record_bi"))
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(
                before,
                (self.trigger("cya_deficiency_event_bi"), self.trigger("cya_check_record_bi")),
            )
        finally:
            MigrationExecutor(connection).migrate(leaves)


class VerificationBase(DeficiencyCase):
    def status(self, deficiency_id):
        return DeficiencyProjection.objects.get(deficiency_id=deficiency_id).status

    def events(self, deficiency_id):
        return list(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id)
            .order_by("sequence")
            .values_list("event_type", flat=True)
        )

    def last_event(self, deficiency_id):
        return DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")

    def report(self, target):
        return report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target=target,
            **self.new_fields(),
            **self.context(self.crew),
        ).body["deficiency_id"]

    def work(self, deficiency_id):
        start_work(
            deficiency_id=deficiency_id,
            expected_event_id=str(self.last_event(deficiency_id).pk),
            **self.context(self.supervisor),
        )

    def correct(self, deficiency_id):
        return record_correction(
            deficiency_id=deficiency_id,
            expected_event_id=str(self.last_event(deficiency_id).pk),
            **self.context(self.crew),
        )

    def pending(self, target):
        """A deficiency reported, worked and repaired: Pending Verification with its scope."""
        deficiency_id = self.report(target)
        self.work(deficiency_id)
        self.correct(deficiency_id)
        return deficiency_id

    def observation(self, item, outcome):
        if item == self.masks:
            return {"quantity": 6 if outcome == "PASSED" else 2}
        if item == self.gauge:
            return {"measurement": {"value": "40", "unit": "psi"}}
        return {}

    def verify(self, deficiency_id, item=None, outcome="PASSED", **fields):
        item = item or self.suction
        fields = {**self.observation(item, outcome), **fields}
        return self.record(
            item=item,
            outcome=outcome,
            purpose="VERIFICATION",
            verification_for_deficiency_id=deficiency_id,
            **fields,
        )

    def refused(self, code, deficiency_id, item=None, outcome="PASSED", **fields):
        before = (CheckRecord.objects.count(), CommandReceipt.objects.count())
        with self.assertRaises(CheckError) as caught:
            self.verify(deficiency_id, item, outcome, **fields)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(before, (CheckRecord.objects.count(), CommandReceipt.objects.count()))
        return caught.exception


class ItemVerificationTests(VerificationBase):
    def test_a_pass_after_the_repair_closes_the_item_deficiency_with_its_check(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        result = self.verify(deficiency_id)
        event = self.last_event(deficiency_id)
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status),
            ("VERIFICATION_CLOSED", "PENDING_VERIFICATION", "CLOSED_VERIFIED"),
        )
        self.assertEqual(str(event.verification_check_id), result.body["check_id"])
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.assertEqual(
            DeficiencyProjection.objects.get(deficiency_id=deficiency_id).last_applied_event_id,
            event.pk,
        )
        self.assertEqual(
            result.body["verification"],
            {
                "deficiency_id": deficiency_id,
                "scope_revision": 1,
                "counts_toward_scope": True,
                "remaining_scope_rows": 0,
                "closure_withheld": False,
                "status": "CLOSED_VERIFIED",
                "suspensions_ended": [],
            },
        )
        self.assertEqual(result.body["event_ids"], [str(event.pk)])
        check = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertEqual(
            (check.purpose, str(check.verification_for_deficiency_id)),
            ("VERIFICATION", deficiency_id),
        )
        self.assertGreater(check.recorded_at, event.predecessor_event.recorded_at)

    def test_routine_passes_never_count_before_or_after_the_repair(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.record(item=self.suction)
        self.work(deficiency_id)
        self.correct(deficiency_id)
        self.record(item=self.suction)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.assertEqual(self.events(deficiency_id)[-1], "CORRECTION_RECORDED")
        self.verify(deficiency_id)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_a_routine_response_carries_no_verification_section(self):
        self.assertNotIn("verification", self.record().body)

    def test_the_verifying_crew_member_may_be_the_one_who_repaired(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.verify(deficiency_id)
        check = CheckRecord.objects.get(purpose="VERIFICATION")
        self.assertEqual(check.actor_user_id, self.crew.pk)

    def test_a_replay_returns_the_original_and_writes_nothing(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        operation_id = str(uuid4())
        first = self.verify(deficiency_id, operation_id=operation_id)
        counts = self.counts()
        replay = self.verify(deficiency_id, operation_id=operation_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.body, first.body)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.events(deficiency_id).count("VERIFICATION_CLOSED"), 1)

    def test_a_lost_response_retry_after_closure_is_the_same_replay_not_a_refusal(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        operation_id = str(uuid4())
        self.verify(deficiency_id, operation_id=operation_id)
        # The client never saw the 201; the deficiency is already closed, yet the retry succeeds.
        retry = self.verify(deficiency_id, operation_id=operation_id)
        self.assertTrue(retry.replayed)
        self.assertEqual(CheckRecord.objects.filter(purpose="VERIFICATION").count(), 1)

    def test_a_different_operation_after_closure_is_refused_without_a_write(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.verify(deficiency_id)
        error = self.refused("VERIFICATION_NOT_PENDING", deficiency_id)
        self.assertEqual(error.status, 409)
        self.assertEqual(self.events(deficiency_id).count("VERIFICATION_CLOSED"), 1)


class RefusalTests(VerificationBase):
    def test_only_a_pending_deficiency_can_be_verified(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.refused("VERIFICATION_NOT_PENDING", deficiency_id)  # Open
        self.work(deficiency_id)
        self.refused("VERIFICATION_NOT_PENDING", deficiency_id)  # In Progress
        self.correct(deficiency_id)
        self.verify(deficiency_id)
        self.refused("VERIFICATION_NOT_PENDING", deficiency_id)  # Closed

    def test_an_unknown_deficiency_and_another_vehicles_deficiency_are_refused(self):
        self.refused("NOT_FOUND", str(uuid4()))
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        with self.assertRaises(CheckError) as caught:
            self.record(
                vehicle=self.vehicles[1],
                outcome="PASSED",
                purpose="VERIFICATION",
                verification_for_deficiency_id=deficiency_id,
            )
        self.assertEqual(caught.exception.code, "DEFICIENCY_VEHICLE_MISMATCH")
        self.assertEqual(CheckRecord.objects.count(), 0)

    def test_an_item_outside_the_scope_is_refused(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        error = self.refused("NOT_IN_VERIFICATION_SCOPE", deficiency_id, item=self.masks)
        self.assertEqual(error.path, "item_id")
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")

    def test_the_purpose_and_target_must_agree(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        with self.assertRaises(CheckError) as caught:
            self.record(purpose="VERIFICATION")
        self.assertEqual(caught.exception.code, "VERIFICATION_TARGET_REQUIRED")
        with self.assertRaises(CheckError) as caught:
            self.record(verification_for_deficiency_id=deficiency_id)
        self.assertEqual(caught.exception.code, "VERIFICATION_TARGET_NOT_ALLOWED")
        with self.assertRaises(CheckError) as caught:
            self.record(
                purpose="VERIFICATION", verification_for_deficiency_id=deficiency_id.upper()
            )
        self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        self.assertEqual(CheckRecord.objects.count(), 0)

    def test_only_a_pass_or_a_fail_can_verify(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.refused(
            "INVALID_OUTCOME", deficiency_id, outcome="UNABLE_TO_INSPECT", explanation="Locked"
        )
        self.refused("INVALID_OUTCOME", deficiency_id, outcome="NOT_APPLICABLE")

    def test_a_pass_cannot_carry_a_deficiency_selection(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.refused(
            "DEFICIENCY_NOT_ALLOWED", deficiency_id, deficiency={"existing_id": deficiency_id}
        )

    def test_a_failed_verification_check_cannot_be_corrected_to_passed(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        check_id = self.verify(deficiency_id, outcome="FAILED").body["check_id"]
        with self.assertRaises(CheckError) as caught:
            correct_check(
                check_id=check_id,
                expected_tip_check_id=check_id,
                outcome="PASSED",
                **self.context(self.crew),
            )
        self.assertEqual(caught.exception.code, "NOT_YET_SUPPORTED")


class FailedVerificationTests(VerificationBase):
    def test_a_fail_adds_evidence_and_returns_the_deficiency_to_in_progress(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        result = self.verify(deficiency_id, outcome="FAILED")
        event = self.last_event(deficiency_id)
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status),
            ("VERIFICATION_FAILED", "PENDING_VERIFICATION", "IN_PROGRESS"),
        )
        self.assertEqual(str(event.verification_check_id), result.body["check_id"])
        self.assertEqual(self.status(deficiency_id), "IN_PROGRESS")
        self.assertTrue(
            DeficiencyEvidence.objects.filter(
                deficiency_id=deficiency_id, check_record_id=result.body["check_id"]
            ).exists()
        )
        self.assertEqual(result.body["deficiency_ids"], [deficiency_id])
        self.assertFalse(result.body["verification"]["counts_toward_scope"])
        # The repair and its scope stay in the history.
        self.assertEqual(
            VerificationScopeRow.objects.filter(deficiency_id=deficiency_id).count(), 1
        )
        self.assertIn("CORRECTION_RECORDED", self.events(deficiency_id))

    def test_the_matching_existing_selection_is_accepted_and_a_conflicting_one_is_refused(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        other = self.report({"item_id": str(self.masks.pk)})
        for label, selection in {
            "another deficiency": {"existing_id": other},
            "a new deficiency": {"create": self.new_fields()},
        }.items():
            with self.subTest(label):
                error = self.refused(
                    "DEFICIENCY_SELECTION_CONFLICT",
                    deficiency_id,
                    outcome="FAILED",
                    deficiency=selection,
                )
                self.assertEqual(error.path, "deficiency")
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.verify(deficiency_id, outcome="FAILED", deficiency={"existing_id": deficiency_id})
        self.assertEqual(self.status(deficiency_id), "IN_PROGRESS")

    def test_a_later_repair_gets_a_new_scope_revision_and_old_passes_do_not_carry_over(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        deficiency_id = self.pending({"compartment_id": str(compartment.pk)})
        scope_items = [self.suction, self.masks, self.gauge, self.special, self.limited]
        for item in scope_items[:4]:
            self.verify(deficiency_id, item)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.verify(deficiency_id, self.limited, outcome="FAILED")
        self.assertEqual(self.status(deficiency_id), "IN_PROGRESS")
        self.correct(deficiency_id)
        revisions = set(
            VerificationScopeRow.objects.filter(deficiency_id=deficiency_id).values_list(
                "scope_revision", flat=True
            )
        )
        self.assertEqual(revisions, {1, 2})
        # The four earlier Passes were recorded before the second repair: none counts again.
        result = self.verify(deficiency_id, self.limited)
        self.assertEqual(result.body["verification"]["remaining_scope_rows"], 4)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        for item in scope_items[:4]:
            self.verify(deficiency_id, item)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")


class ScopeTests(VerificationBase):
    def test_compartment_scope_needs_every_item_in_any_order(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        deficiency_id = self.pending({"compartment_id": str(compartment.pk)})
        order = [self.limited, self.special, self.suction, self.gauge, self.masks]
        for position, item in enumerate(order, start=1):
            result = self.verify(deficiency_id, item)
            self.assertEqual(result.body["verification"]["remaining_scope_rows"], 5 - position)
            expected = "CLOSED_VERIFIED" if position == 5 else "PENDING_VERIFICATION"
            self.assertEqual(self.status(deficiency_id), expected)
        self.assertEqual(self.events(deficiency_id).count("VERIFICATION_CLOSED"), 1)

    def test_a_pass_recorded_for_one_revision_never_satisfies_another(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        deficiency_id = self.pending({"compartment_id": str(compartment.pk)})
        check = CheckRecord.objects.get(
            pk=self.verify(deficiency_id, self.suction).body["check_id"]
        )
        self.assertEqual(check.verification_scope_revision, 1)
        rows = list(VerificationScopeRow.objects.filter(deficiency_id=deficiency_id))
        organization_id = str(self.org.pk)
        satisfied = _satisfied_scope_rows(organization_id, deficiency_id, rows)
        self.assertEqual(satisfied, {r.pk for r in rows if r.item_id == str(self.suction.pk)})
        # A later revision of another correction, even one that kept every row's instant, is not
        # satisfied by the earlier revision's Pass (VER-4; only a supervisor expansion of the same
        # correction carries Passes over, SPEC 1.22.0, tested in test_scope_expansion).
        other_correction = str(uuid4())
        later = [
            SimpleNamespace(
                pk=r.pk,
                item_id=r.item_id,
                instance_key=r.instance_key,
                required_after=r.required_after,
                scope_revision=2,
                correction_event_id=other_correction,
            )
            for r in rows
        ]
        self.assertEqual(_satisfied_scope_rows(organization_id, deficiency_id, later), set())

    def test_a_repeated_pass_of_one_row_does_not_stand_in_for_another(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        deficiency_id = self.pending({"compartment_id": str(compartment.pk)})
        for _ in range(3):
            self.verify(deficiency_id, self.suction)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")

    def test_vehicle_scope_stays_open_while_a_reading_item_cannot_be_checked(self):
        system = VehicleSystem.objects.get(vehicle=self.vehicle)
        deficiency_id = self.pending({"vehicle": True, "system_ids": [str(system.pk)]})
        result = self.verify(deficiency_id, self.siren)
        self.assertEqual(result.body["verification"]["remaining_scope_rows"], 1)
        # An odometer reading is recorded as an odometer entry, not a check (fail closed).
        with self.assertRaises(CheckError) as caught:
            self.verify(deficiency_id, self.odometer)
        self.assertEqual(caught.exception.code, "NOT_YET_SUPPORTED")
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")


class KitScopeTests(VerificationBase):
    def setUp(self):
        super().setUp()
        self.kit_item = self.stray

        def add_kit(label):
            return add_kit_instance(
                label=label,
                vehicle_id=str(self.vehicle.pk),
                compartment_id=str(self.cabinet.pk),
                **self.context(self.supervisor),
            ).body["kit_id"]

        self.first_kit = add_kit("First physical kit")
        self.second_kit = add_kit("Second physical kit")
        self.version_id = self.published_version(
            [*self.base_items(), self.spec(self.kit_item, 80, kit_id=self.first_kit)]
        )

    def test_each_physical_kit_instance_needs_its_own_pass(self):
        deficiency_id = self.pending({"item_id": str(self.kit_item.pk)})
        keys = set(
            VerificationScopeRow.objects.filter(deficiency_id=deficiency_id).values_list(
                "instance_key", flat=True
            )
        )
        self.assertEqual(keys, {self.first_kit, self.second_kit})
        first = self.verify(deficiency_id, self.kit_item, kit_id=self.first_kit)
        self.assertEqual(first.body["verification"]["remaining_scope_rows"], 1)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        # The identical item in the same kit again does not satisfy the other kit's row.
        self.verify(deficiency_id, self.kit_item, kit_id=self.first_kit)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.verify(deficiency_id, self.kit_item, kit_id=self.second_kit)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_a_kit_deficiency_covers_only_its_own_instance(self):
        deficiency_id = self.pending({"kit_id": self.second_kit})
        keys = set(
            VerificationScopeRow.objects.filter(deficiency_id=deficiency_id).values_list(
                "instance_key", flat=True
            )
        )
        self.assertEqual(keys, {self.second_kit})
        with self.assertRaises(CheckError) as caught:
            self.verify(deficiency_id, self.kit_item, kit_id=self.first_kit)
        self.assertEqual(caught.exception.code, "NOT_IN_VERIFICATION_SCOPE")
        self.verify(deficiency_id, self.kit_item, kit_id=self.second_kit)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")


class InvalidationTests(VerificationBase):
    def invalidate(self, check_id):
        return invalidate_chain(
            check_id=check_id,
            explanation="Synthetic invalidation reason",
            **self.context(self.admin),
        )

    def test_an_invalidated_pass_no_longer_counts(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        deficiency_id = self.pending({"compartment_id": str(compartment.pk)})
        first = self.verify(deficiency_id, self.suction).body["check_id"]
        for item in (self.masks, self.gauge, self.special):
            self.verify(deficiency_id, item)
        self.invalidate(first)
        self.verify(deficiency_id, self.limited)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.verify(deficiency_id, self.suction)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_invalidated_original_support_keeps_it_open_until_an_explicit_closure(self):
        original = self.failed(item=self.suction)
        deficiency_id = original.body["deficiency_ids"][0]
        self.invalidate(original.body["check_id"])
        self.assertEqual(self.status(deficiency_id), "OPEN")  # INV-3: invalidation closes nothing
        self.record(item=self.suction)  # a routine pass is not verification
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.work(deficiency_id)
        self.correct(deficiency_id)
        result = self.verify(deficiency_id)
        # The Pass is recorded and counts, every row is satisfied, yet INV-3 withholds the closure.
        self.assertTrue(result.body["verification"]["counts_toward_scope"])
        self.assertEqual(result.body["verification"]["remaining_scope_rows"], 0)
        self.assertTrue(result.body["verification"]["closure_withheld"])
        self.assertEqual(result.body["verification"]["status"], "PENDING_VERIFICATION")
        self.assertEqual(result.body["event_ids"], [])
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.assertNotIn("VERIFICATION_CLOSED", self.events(deficiency_id))
        # Only an explicit closure under 8.8 (an Administrator here) closes it.
        close_administrator(
            deficiency_id=deficiency_id,
            explanation="Synthetic explicit closure",
            expected_event_id=str(self.last_event(deficiency_id).pk),
            **self.context(self.admin),
        )
        self.assertEqual(self.status(deficiency_id), "CLOSED_ADMINISTRATOR")

    def test_a_deficiency_without_invalidated_support_still_closes_automatically(self):
        original = self.failed(item=self.suction)
        deficiency_id = original.body["deficiency_ids"][0]
        self.work(deficiency_id)
        self.correct(deficiency_id)
        result = self.verify(deficiency_id)
        self.assertFalse(result.body["verification"]["closure_withheld"])
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")


class ConcurrencyAndAtomicityTests(VerificationBase):
    def scope_deficiency(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        return self.pending({"compartment_id": str(compartment.pk)})

    def test_two_concurrent_last_passes_close_exactly_once(self):
        deficiency_id = self.scope_deficiency()
        for item in (self.suction, self.masks, self.gauge):
            self.verify(deficiency_id, item)
        barrier = Barrier(2)

        def submit(item):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.verify(deficiency_id, item)
            except Exception as error:  # returned to the assertion thread
                return error
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, (self.special, self.limited)))
        errors = [o for o in outcomes if isinstance(o, Exception)]
        self.assertEqual(errors, [])
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.assertEqual(self.events(deficiency_id).count("VERIFICATION_CLOSED"), 1)
        closing = [o for o in outcomes if o.body["verification"]["status"] == "CLOSED_VERIFIED"]
        self.assertEqual(len(closing), 1)
        self.assertEqual(CheckRecord.objects.filter(purpose="VERIFICATION").count(), 5)

    def test_concurrent_pass_and_fail_leave_one_consistent_outcome(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        barrier = Barrier(2)

        def submit(outcome):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.verify(deficiency_id, self.suction, outcome)
            except Exception as error:
                return error
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, ("PASSED", "FAILED")))
        successes = [o for o in outcomes if not isinstance(o, Exception)]
        failures = [o for o in outcomes if isinstance(o, Exception)]
        # Whichever took the deficiency lock second saw a deficiency that is no longer pending.
        self.assertEqual((len(successes), len(failures)), (1, 1), outcomes)
        self.assertEqual(failures[0].code, "VERIFICATION_NOT_PENDING")
        self.assertIn(self.status(deficiency_id), ("CLOSED_VERIFIED", "IN_PROGRESS"))
        self.assertEqual(
            len([e for e in self.events(deficiency_id) if e.startswith("VERIFICATION_")]), 1
        )

    def test_a_late_write_failure_leaves_nothing_and_the_operation_id_reusable(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        counts = self.counts()
        checks = CheckRecord.objects.count()
        operation_id = str(uuid4())
        with mock.patch(
            "cya.checks.commands.DeficiencyEvent.objects.create", side_effect=DatabaseError("boom")
        ):
            with self.assertRaises(DatabaseError):
                self.verify(deficiency_id, operation_id=operation_id)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(CheckRecord.objects.count(), checks)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        retry = self.verify(deficiency_id, operation_id=operation_id)
        self.assertFalse(retry.replayed)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")


class DatabaseGuardTests(VerificationBase):
    def receipt(self):
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

    def insert_check(self, receipt, deficiency_id, item=None, **overrides):
        item = item or self.suction
        base = CheckRecord.objects.filter(item=item).first() or CheckRecord.objects.first()
        check_id = str(uuid4())
        fields = {
            "id": check_id,
            "root_check_id": check_id,
            "organization_id": self.org.pk,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.crew,
            "actor_roles_at_time": ["CREW"],
            "operation_id": receipt.operation_id,
            "vehicle": self.vehicle,
            "item": item,
            "item_def_id": base.item_def_id,
            "checklist_version_id": self.version_id,
            "outcome": "PASSED",
            "purpose": "VERIFICATION",
            "verification_for_deficiency_id": deficiency_id,
            "verification_scope_revision": 1 if deficiency_id else None,
            **overrides,
        }
        return CheckRecord.objects.create(**fields)

    def refused(self, operation, text=None):
        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            operation()
        if text:
            self.assertIn(text, str(caught.exception))

    def test_the_database_pairs_purpose_with_target_and_requires_a_pending_scope_item(self):
        self.record()  # a routine row to copy the definition from
        pending = self.pending({"item_id": str(self.suction.pk)})
        open_one = self.report({"item_id": str(self.suction.pk)})
        self.refused(lambda: self.insert_check(self.receipt(), None))
        self.refused(lambda: self.insert_check(self.receipt(), pending, purpose="ROUTINE"))
        self.refused(
            lambda: self.insert_check(self.receipt(), open_one),
            "not for a pending scope item",
        )
        self.refused(
            lambda: self.insert_check(self.receipt(), pending, item=self.masks, quantity=6),
            "not for a pending scope item",
        )
        self.refused(
            lambda: self.insert_check(self.receipt(), pending, outcome="NOT_APPLICABLE"),
            "not for a pending scope item",
        )
        for revision in (2, None):  # not the newest revision, or none: refused
            self.refused(
                lambda revision=revision: self.insert_check(
                    self.receipt(), pending, verification_scope_revision=revision
                )
            )
        self.insert_check(self.receipt(), pending)  # the valid row is accepted

    def test_the_database_refuses_a_closure_while_a_scope_row_is_unsatisfied(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        self.record()
        deficiency_id = self.pending({"compartment_id": str(compartment.pk)})
        receipt = self.receipt()
        check = self.insert_check(receipt, deficiency_id)
        last = self.last_event(deficiency_id)

        def forged(**overrides):
            fields = {
                "organization_id": self.org.pk,
                "deficiency_id": deficiency_id,
                "sequence": last.sequence + 1,
                "predecessor_event": last,
                "event_type": "VERIFICATION_CLOSED",
                "from_status": "PENDING_VERIFICATION",
                "to_status": "CLOSED_VERIFIED",
                "verification_check": check,
                "command": receipt,
                "recorded_at": receipt.recorded_at,
                "actor_user": self.crew,
                "actor_roles_at_time": ["CREW"],
                **overrides,
            }
            return DeficiencyEvent.objects.create(**fields)

        self.refused(forged, "every scope row satisfied")
        self.refused(lambda: forged(explanation="free text"))
        self.refused(lambda: forged(event_type="VERIFICATION_FAILED", to_status="IN_PROGRESS"))
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")

    def test_the_database_refuses_a_closure_while_the_original_support_is_invalidated(self):
        original = self.failed(item=self.suction)
        deficiency_id = original.body["deficiency_ids"][0]
        invalidate_chain(
            check_id=original.body["check_id"],
            explanation="Synthetic invalidation reason",
            **self.context(self.admin),
        )
        self.work(deficiency_id)
        self.correct(deficiency_id)
        receipt = self.receipt()
        check = self.insert_check(receipt, deficiency_id)  # a valid Pass satisfying the one row
        last = self.last_event(deficiency_id)

        def forged():
            return DeficiencyEvent.objects.create(
                organization_id=self.org.pk,
                deficiency_id=deficiency_id,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type="VERIFICATION_CLOSED",
                from_status="PENDING_VERIFICATION",
                to_status="CLOSED_VERIFIED",
                verification_check=check,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.crew,
                actor_roles_at_time=["CREW"],
            )

        self.refused(forged, "no invalidated support")
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
