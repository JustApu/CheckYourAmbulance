"""Correction and invalidation effects on deficiencies (issue 81 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/81); COR-3, COR-4, INV-3, INV-7)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import TestCase, mock
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from cya.checks.commands import CheckError, correct_check, invalidate_chain
from cya.checks.models import CheckEvent, CheckRecord
from cya.core.models import AuditEvent
from cya.deficiencies import acknowledgement, effects
from cya.deficiencies.closure import close_administrator, reopen_administrator
from cya.deficiencies.commands import (
    assign_deficiency,
    report_deficiency,
    set_deadline,
    start_work,
)
from cya.deficiencies.models import DeficiencyEvent, DeficiencyEvidence, DeficiencyProjection
from cya.deficiencies.tests.support import DeficiencyCase
from cya.notifications.models import NotificationOutbox
from cya.operations.models import VehicleStatusEvent
from cya.testing.notifications import notified


class DecisionTests(TestCase):
    def test_each_branch_of_the_cor_4_conditions(self):
        decide = effects.decide_correction_effect
        self.assertEqual(decide("OPEN", False, False), "ERROR")
        self.assertEqual(decide("IN_PROGRESS", False, False), "ERROR")
        self.assertEqual(decide("PENDING_VERIFICATION", False, False), "ERROR")
        # another effective failed check, or recorded repair/verification work, keeps it open
        self.assertEqual(decide("OPEN", True, False), "NOTICE")
        self.assertEqual(decide("OPEN", False, True), "NOTICE")
        # the table has no Awaiting External Dependency to Reported in Error arrow
        self.assertEqual(decide("AWAITING_EXTERNAL_DEPENDENCY", False, False), "NOTICE")
        # already closed or cancelled: nothing to do
        for status in (
            "CLOSED_VERIFIED",
            "CLOSED_ADMINISTRATOR",
            "CLOSED_UNVERIFIABLE",
            "REPORTED_IN_ERROR",
        ):
            self.assertEqual(decide(status, False, False), "NONE", status)

    def test_only_a_non_neutral_event_type_counts_as_recorded_repair(self):
        self.assertEqual(
            effects.NEUTRAL_EVENT_TYPES,
            {
                "REPORTED",
                "REPORTED_IN_ERROR",
                "CORRECTION_NOTICE",
                "REOPENED",
                "SEVERITY_CHANGED",
                "ASSIGNED",
                "DEADLINE_SET",
                "WORK_STARTED",
                "DEPENDENCY_ENTERED",
                "DEPENDENCY_REVISED",
                "DEPENDENCY_ENDED",
                "EXTENSION_REQUESTED",
                "EXTENSION_DECIDED",
                "ESCALATION_ACKNOWLEDGED",
                "ADMINISTRATOR_CLOSED",
                "ADMINISTRATOR_REOPENED",
                "EMPTY_SCOPE_CLOSED",
                "VERIFICATION_REOPENED",
                "CHECKS_SUSPENDED",
            },
        )
        self.assertNotIn("PROGRESS_NOTE", effects.NEUTRAL_EVENT_TYPES)  # a note may describe repair
        self.assertNotIn("CORRECTION_RECORDED", effects.NEUTRAL_EVENT_TYPES)


class EffectTests(DeficiencyCase):
    def correct(self, check_id, tip=None, *, actor=None, **extra):
        context = self.context(actor or self.crew) | extra.pop("context", {})
        return correct_check(
            check_id=check_id,
            expected_tip_check_id=tip or check_id,
            outcome="PASSED",
            **context,
            **extra,
        )

    def invalidate(self, check_id, *, actor=None, **context):
        args = self.context(actor or self.admin) | context
        return invalidate_chain(check_id=check_id, explanation="Synthetic reason.", **args)

    def events(self, deficiency_id):
        return list(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id)
            .order_by("sequence")
            .values_list("sequence", "event_type", "from_status", "to_status")
        )

    def status(self, deficiency_id):
        return DeficiencyProjection.objects.get(deficiency_id=deficiency_id).status

    def test_assignment_a_deadline_and_starting_work_do_not_stop_a_cancellation_or_its_reopening(
        self,
    ):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        for step in (
            lambda last: assign_deficiency(
                deficiency_id=deficiency_id,
                assignee_id=str(self.crew.pk),
                expected_event_id=last,
                **self.context(self.supervisor),
            ),
            lambda last: set_deadline(
                deficiency_id=deficiency_id,
                deadline=timezone.now() + timedelta(days=3),
                expected_event_id=last,
                **self.context(self.supervisor),
            ),
            lambda last: start_work(
                deficiency_id=deficiency_id,
                expected_event_id=last,
                **self.context(self.supervisor),
            ),
        ):
            last = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
            step(str(last.pk))
        self.assertEqual(self.status(deficiency_id), "IN_PROGRESS")
        root = first.body["check_id"]
        self.correct(root)
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")
        self.assertEqual(
            [row[1] for row in self.events(deficiency_id)],
            ["REPORTED", "ASSIGNED", "DEADLINE_SET", "WORK_STARTED", "REPORTED_IN_ERROR"],
        )
        self.invalidate(root)
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(self.events(deficiency_id)[-1][1], "REOPENED")

    def test_acknowledgement_does_not_block_author_cancellation(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        acknowledgement.set_acknowledgement_maximum(
            maximum_days=3,
            **self.context(self.admin),
        )
        latest = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        acknowledgement.acknowledge_escalation(
            deficiency_id=deficiency_id,
            explanation="The supervisor reviewed the repair plan.",
            action_plan="The assigned mechanic will replace the switch.",
            period_days=2,
            expected_event_id=str(latest.pk),
            **self.context(self.supervisor),
        )

        corrected = self.correct(first.body["check_id"])

        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")
        self.assertEqual(self.events(deficiency_id)[-1][1], "REPORTED_IN_ERROR")
        self.assertEqual(corrected.body["deficiency_effects"][0]["event_type"], "REPORTED_IN_ERROR")

    def test_administrator_close_and_reopen_are_neutral_for_author_correction(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        reported = DeficiencyEvent.objects.get(deficiency_id=deficiency_id, sequence=1)
        closed = close_administrator(
            deficiency_id=str(deficiency_id),
            expected_event_id=str(reported.pk),
            explanation="Administrative closure while the replacement is unavailable.",
            **self.context(self.admin),
        )
        reopen_administrator(
            deficiency_id=str(deficiency_id),
            expected_event_id=closed.body["event_id"],
            explanation="The replacement arrived; work can resume.",
            **self.context(self.admin),
        )

        corrected = self.correct(first.body["check_id"])

        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")
        self.assertEqual(
            [event_type for _, event_type, _, _ in self.events(deficiency_id)],
            ["REPORTED", "ADMINISTRATOR_CLOSED", "ADMINISTRATOR_REOPENED", "REPORTED_IN_ERROR"],
        )
        self.assertEqual(corrected.body["deficiency_effects"][0]["event_type"], "REPORTED_IN_ERROR")

    def failed_pair(self):
        """One deficiency supported by two Failed checks on different items (DEF-3)."""
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        second = self.failed(self.siren, deficiency={"existing_id": deficiency_id})
        return deficiency_id, first.body["check_id"], second.body["check_id"]

    def test_correcting_the_only_failed_check_reports_the_deficiency_in_error(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        result = self.correct(first.body["check_id"])
        self.assertEqual(
            self.events(deficiency_id),
            [(1, "REPORTED", None, "OPEN"), (2, "REPORTED_IN_ERROR", "OPEN", "REPORTED_IN_ERROR")],
        )
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")
        event = DeficiencyEvent.objects.get(event_type="REPORTED_IN_ERROR")
        self.assertEqual(result.body["deficiency_event_ids"], [event.pk])
        self.assertEqual(
            (event.actor_user_id, event.correction_ref, event.predecessor_event.sequence),
            (self.crew.pk, first.body["check_id"], 1),
        )
        self.assertEqual(DeficiencyProjection.objects.get().last_applied_event_id, event.pk)
        # history is retained: the evidence and the original Failed check are untouched
        self.assertEqual(DeficiencyEvidence.objects.get().check_record_id, first.body["check_id"])
        self.assertEqual(CheckRecord.objects.get(pk=first.body["check_id"]).outcome, "FAILED")
        audit = AuditEvent.objects.get(action="CHECK_CORRECTION", outcome="SUCCEEDED")
        self.assertEqual(audit.new_snapshot["values"]["deficiency_event_ids"], [event.pk])

    def test_a_recorded_repair_keeps_the_deficiency_open_after_author_corrects_failed_entry(self):
        from cya.deficiencies.commands import record_correction, start_work

        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        event = DeficiencyEvent.objects.get(deficiency_id=deficiency_id)
        start_work(
            deficiency_id=deficiency_id,
            expected_event_id=str(event.pk),
            **self.context(self.supervisor),
        )
        event = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        record_correction(
            deficiency_id=deficiency_id,
            expected_event_id=str(event.pk),
            **self.context(self.crew),
        )
        self.correct(first.body["check_id"])
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.assertEqual(self.events(deficiency_id)[-1][1], "CORRECTION_NOTICE")

    def test_another_effective_failed_check_keeps_it_open_and_notifies_a_supervisor(self):
        deficiency_id, first, second = self.failed_pair()
        result = self.correct(first)
        self.assertEqual(
            self.events(deficiency_id),
            [(1, "REPORTED", None, "OPEN"), (2, "CORRECTION_NOTICE", "OPEN", "OPEN")],
        )
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(result.body["deficiency_effects"][0]["event_type"], "CORRECTION_NOTICE")
        notice = DeficiencyEvent.objects.get(sequence=2)
        self.assertEqual(notice.correction_ref, first)
        # COR-4: supervisors are notified to resolve it (issue 14), never the correcting author.
        self.assertEqual(notified("deficiency.correction_notice", notice.pk), [self.supervisor.pk])
        # the correction itself is recorded either way
        self.assertEqual(CheckRecord.objects.filter(root_check_id=first).count(), 2)
        # correcting the last failed check now cancels it (no other effective failed check)
        self.correct(second)
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")

    def test_an_unable_check_also_keeps_it_open(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        self.record(
            item=self.siren,
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic reason.",
            deficiency={"existing_id": deficiency_id},
        )
        self.correct(first.body["check_id"])
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(self.events(deficiency_id)[-1][1], "CORRECTION_NOTICE")

    def test_another_chain_that_was_invalidated_does_not_block(self):
        deficiency_id, first, second = self.failed_pair()
        self.invalidate(second)
        self.correct(first)
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")

    def test_a_corrected_other_chain_does_not_block(self):
        deficiency_id, first, second = self.failed_pair()
        self.correct(second)  # the other chain is no longer failing; but this one still is
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.correct(first)
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")

    def test_unable_to_passed_leaves_the_deficiency_alone(self):
        result = self.record(
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic reason.",
            deficiency={"create": self.new_fields()},
        )
        deficiency_id = result.body["deficiency_ids"][0]
        corrected = self.correct(result.body["check_id"])
        self.assertEqual(corrected.body["deficiency_event_ids"], [])
        self.assertEqual(self.events(deficiency_id), [(1, "REPORTED", None, "OPEN")])

    def test_a_passed_check_correction_touches_no_deficiency(self):
        failed = self.failed()
        passed = self.record(item=self.siren, outcome="PASSED")
        self.correct(passed.body["check_id"])
        self.assertEqual(
            self.events(failed.body["deficiency_ids"][0]), [(1, "REPORTED", None, "OPEN")]
        )

    def test_a_non_author_cannot_correct_and_nothing_changes(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        with self.assertRaises(CheckError) as caught:
            self.correct(first.body["check_id"], actor=self.supervisor)
        self.assertEqual(caught.exception.code, "AUTHOR_REQUIRED")
        self.assertEqual(self.events(deficiency_id), [(1, "REPORTED", None, "OPEN")])

    def test_one_check_supporting_two_deficiencies_is_handled_under_both_locks(self):
        first = self.failed()
        d1 = first.body["deficiency_ids"][0]
        d2 = report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str(self.suction.pk)},
            **self.new_fields(),
            **self.context(self.crew),
        ).body["deficiency_id"]
        self.attach(d2, first.body["check_id"])
        other = self.failed(self.siren, deficiency={"existing_id": d2})  # only d2 has more evidence
        result = self.correct(first.body["check_id"])
        self.assertEqual(self.status(d1), "REPORTED_IN_ERROR")
        self.assertEqual(self.status(d2), "OPEN")
        self.assertEqual(
            {e["deficiency_id"]: e["event_type"] for e in result.body["deficiency_effects"]},
            {d1: "REPORTED_IN_ERROR", d2: "CORRECTION_NOTICE"},
        )
        # invalidating the chain reopens only the deficiency it cancelled
        invalidated = self.invalidate(first.body["check_id"])
        self.assertEqual(invalidated.body["reopened_deficiency_ids"], [d1])
        self.assertEqual((self.status(d1), self.status(d2)), ("OPEN", "OPEN"))
        self.assertTrue(other.body["check_id"])

    def attach(self, deficiency_id, check_id):
        """A second deficiency citing the same check, as the evidence guard permits."""
        from django.db import transaction

        from cya.core.audit_repository import append_receipt

        with transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.crew,
                command_type="CHECK_RECORD",
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )
            DeficiencyEvidence.objects.create(
                organization_id=self.org.pk,
                deficiency_id=deficiency_id,
                check_record_id=check_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.crew,
                actor_roles_at_time=["CREW"],
            )

    def test_invalidating_a_failed_chain_never_closes_or_cancels_the_deficiency(self):
        """INV-3 / AC-14: the deficiency stays open, with the chain kept as invalid evidence."""
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        invalidated = self.invalidate(first.body["check_id"])
        self.assertEqual(invalidated.body["reopened_deficiency_ids"], [])
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(self.events(deficiency_id), [(1, "REPORTED", None, "OPEN")])
        self.assertEqual(DeficiencyEvidence.objects.get().check_record_id, first.body["check_id"])

    def test_an_outbox_write_failure_rolls_back_the_invalidation_and_its_reopening(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        corrected = self.correct(first.body["check_id"])
        with (
            mock.patch.object(
                NotificationOutbox.objects, "bulk_create", side_effect=DatabaseError("synthetic")
            ),
            self.assertRaises(DatabaseError),
        ):
            self.invalidate(corrected.body["check_id"])
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")
        self.assertFalse(DeficiencyEvent.objects.filter(event_type="REOPENED").exists())
        self.assertFalse(CheckEvent.objects.filter(event_type="INVALIDATION").exists())

    def test_invalidating_the_chain_that_cancelled_a_deficiency_reopens_it(self):
        """INV-7 / AC-32: reopened atomically, attributed, nothing revived or inferred."""
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        corrected = self.correct(first.body["check_id"])
        statuses = VehicleStatusEvent.objects.count()
        invalidated = self.invalidate(corrected.body["check_id"])  # any row resolves to the root
        self.assertEqual(invalidated.body["reopened_deficiency_ids"], [deficiency_id])
        self.assertEqual(self.status(deficiency_id), "OPEN")
        reopened = DeficiencyEvent.objects.get(event_type="REOPENED")
        self.assertEqual((reopened.actor_user_id, reopened.sequence), (self.admin.pk, 3))
        self.assertEqual(
            notified("deficiency.reopened_reported_in_error", reopened.pk), [self.supervisor.pk]
        )
        self.assertIn("ADMINISTRATOR", reopened.actor_roles_at_time)
        self.assertEqual(
            (reopened.from_status, reopened.to_status, reopened.correction_ref),
            ("REPORTED_IN_ERROR", "OPEN", first.body["check_id"]),
        )
        self.assertEqual(DeficiencyProjection.objects.get().last_applied_event_id, reopened.pk)
        # the evidence stays; no earlier outcome revives; no repair is inferred; status unchanged
        self.assertEqual(DeficiencyEvidence.objects.count(), 1)
        self.assertEqual(
            [e for _, e, _, _ in self.events(deficiency_id)],
            ["REPORTED", "REPORTED_IN_ERROR", "REOPENED"],
        )
        self.assertEqual(VehicleStatusEvent.objects.count(), statuses)
        with self.assertRaises(CheckError) as caught:
            self.correct(first.body["check_id"], corrected.body["check_id"])
        self.assertEqual(caught.exception.code, "CHAIN_INVALIDATED")

    def test_an_independently_closed_deficiency_is_not_reopened(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        corrected = self.correct(first.body["check_id"])
        # administrator closure is an M4 lifecycle step; stand in for its result here
        DeficiencyProjection.objects.filter(deficiency_id=deficiency_id).update(
            status="CLOSED_ADMINISTRATOR"
        )
        invalidated = self.invalidate(corrected.body["check_id"])
        self.assertEqual(invalidated.body["reopened_deficiency_ids"], [])
        self.assertEqual(self.status(deficiency_id), "CLOSED_ADMINISTRATOR")
        self.assertFalse(DeficiencyEvent.objects.filter(event_type="REOPENED").exists())

    def test_a_deficiency_cancelled_by_another_chain_is_not_reopened_by_this_one(self):
        deficiency_id, first, second = self.failed_pair()
        self.correct(first)  # stays open with a notice
        self.correct(second)  # the last failing chain cancels it
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")
        invalidated = self.invalidate(first)  # not the chain whose correction cancelled it
        self.assertEqual(invalidated.body["reopened_deficiency_ids"], [])
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")
        reopened = self.invalidate(second)
        self.assertEqual(reopened.body["reopened_deficiency_ids"], [deficiency_id])

    def test_replay_repeats_the_stored_result_and_adds_nothing(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        operation_id = str(uuid4())
        once = self.correct(first.body["check_id"], context={"operation_id": operation_id})
        events = DeficiencyEvent.objects.count()
        again = self.correct(first.body["check_id"], context={"operation_id": operation_id})
        self.assertEqual((again.body, again.replayed), (once.body, True))
        self.assertEqual(DeficiencyEvent.objects.count(), events)
        operation_id = str(uuid4())
        inv = self.invalidate(first.body["check_id"], operation_id=operation_id)
        count = DeficiencyEvent.objects.count()
        replay = self.invalidate(first.body["check_id"], operation_id=operation_id)
        self.assertEqual((replay.body, replay.replayed), (inv.body, True))
        self.assertEqual(replay.body["reopened_deficiency_ids"], [deficiency_id])
        self.assertEqual(DeficiencyEvent.objects.count(), count)

    def test_a_failure_while_writing_the_effect_rolls_the_correction_back(self):
        first = self.failed()
        before = (CheckRecord.objects.count(), DeficiencyEvent.objects.count())
        with (
            mock.patch("cya.deficiencies.effects.append_effect", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            self.correct(first.body["check_id"])
        self.assertEqual((CheckRecord.objects.count(), DeficiencyEvent.objects.count()), before)
        self.assertEqual(self.status(first.body["deficiency_ids"][0]), "OPEN")

    def run_threads(self, jobs):
        def run(job):
            try:
                return job()
            except Exception as error:  # reported to the test, which asserts on the type
                return error
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            return [f.result(timeout=120) for f in [pool.submit(run, j) for j in jobs]]

    def test_concurrent_correction_and_invalidation_of_one_chain_end_open_and_consistent(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        check = first.body["check_id"]
        outcomes = self.run_threads([lambda: self.correct(check), lambda: self.invalidate(check)])
        self.assertTrue(any(hasattr(o, "body") for o in outcomes), outcomes)
        refused = [o for o in outcomes if isinstance(o, CheckError)]
        self.assertTrue(all(o.code == "CHAIN_INVALIDATED" for o in refused), outcomes)
        # either order leaves the deficiency open, with an unforked event chain
        self.assertEqual(self.status(deficiency_id), "OPEN")
        sequence = self.events(deficiency_id)
        self.assertEqual([row[0] for row in sequence], list(range(1, len(sequence) + 1)))
        kinds = [row[1] for row in sequence]
        self.assertIn(kinds, (["REPORTED"], ["REPORTED", "REPORTED_IN_ERROR", "REOPENED"]))

    def test_concurrent_corrections_of_two_chains_cancel_exactly_once(self):
        deficiency_id, first, second = self.failed_pair()
        outcomes = self.run_threads([lambda: self.correct(first), lambda: self.correct(second)])
        self.assertTrue(all(hasattr(o, "body") for o in outcomes), outcomes)
        kinds = sorted(row[1] for row in self.events(deficiency_id))
        self.assertEqual(kinds, ["CORRECTION_NOTICE", "REPORTED", "REPORTED_IN_ERROR"])
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")


class HardeningTests(DeficiencyCase):
    correct = EffectTests.correct
    invalidate = EffectTests.invalidate
    events = EffectTests.events
    status = EffectTests.status
    failed_pair = EffectTests.failed_pair
    attach = EffectTests.attach
    run_threads = EffectTests.run_threads

    """Review findings on PR 127 (https://github.com/JustApu/CheckYourAmbulance-archive/pull/127): legacy replays, bounded work, and the reopening guard."""

    def test_a_legacy_correction_receipt_still_acknowledges_over_http(self):
        from types import SimpleNamespace

        from cya.checks.api import _correction_acknowledgement

        first = self.failed()
        corrected = self.correct(first.body["check_id"])
        legacy = {k: v for k, v in corrected.body.items() if not k.startswith("deficiency_")}
        user = SimpleNamespace(organization_id=self.org.pk)
        acknowledgement = _correction_acknowledgement(SimpleNamespace(body=legacy), user)
        self.assertEqual(acknowledgement["deficiency_event_ids"], [])
        self.assertEqual(acknowledgement["check_id"], corrected.body["check_id"])

    def test_too_many_other_chains_keeps_the_deficiency_open(self):
        deficiency_id, first, second = self.failed_pair()
        with mock.patch.object(effects, "MAX_EVIDENCE", 0):
            self.correct(first)
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(self.events(deficiency_id)[-1][1], "CORRECTION_NOTICE")

    def test_too_much_history_keeps_the_deficiency_open(self):
        deficiency_id, first, second = self.failed_pair()
        self.invalidate(second)  # without a bound this other chain would not block
        with mock.patch.object(effects, "MAX_CHAIN_ROWS", 0):
            self.correct(first)
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(self.events(deficiency_id)[-1][1], "CORRECTION_NOTICE")

    def test_a_history_too_long_to_inspect_keeps_the_deficiency_open(self):
        deficiency_id, first, second = self.failed_pair()
        self.correct(second)  # a notice: the history is now two events, all neutral
        with mock.patch.object(effects, "MAX_HISTORY_EVENTS", 1):
            self.correct(first)  # nothing else fails, but the history cannot be inspected
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(self.events(deficiency_id)[-1][1], "CORRECTION_NOTICE")
        # within the bound the same situation cancels it (see the earlier tests)

    def test_a_history_within_the_bound_of_neutral_events_does_not_block(self):
        deficiency_id, first, second = self.failed_pair()
        self.correct(second)
        with mock.patch.object(effects, "MAX_HISTORY_EVENTS", 2):
            self.correct(first)
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")

    def test_a_check_supporting_too_many_deficiencies_is_refused_before_any_write(self):
        first = self.failed()
        d2 = report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str(self.suction.pk)},
            **self.new_fields(),
            **self.context(self.crew),
        ).body["deficiency_id"]
        self.attach(d2, first.body["check_id"])
        before = (CheckRecord.objects.count(), DeficiencyEvent.objects.count())
        with (
            mock.patch.object(effects, "MAX_SUPPORTING", 1),
            self.assertRaises(CheckError) as caught,
        ):
            self.correct(first.body["check_id"])
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("CONFIGURATION_UNAVAILABLE", 503)
        )
        self.assertEqual((CheckRecord.objects.count(), DeficiencyEvent.objects.count()), before)

    def test_only_a_deficiency_whose_last_event_is_the_chains_cancellation_reopens(self):
        """A neutral event after the cancellation (status unchanged) blocks the reopening."""
        deficiency_id, first, second = self.failed_pair()
        self.correct(first)  # notice: the other chain still fails
        self.correct(second)  # cancels, citing chain two
        last = (
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id)
            .order_by("-sequence")
            .first()
        )
        self.assertEqual((last.event_type, last.correction_ref), ("REPORTED_IN_ERROR", second))
        # chain one did not cancel it, and is not the last event: invalidating it reopens nothing
        self.assertEqual(self.invalidate(first).body["reopened_deficiency_ids"], [])
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")

    def test_the_database_reopens_only_against_the_immediately_preceding_cancellation(self):
        """A neutral event after the cancellation must stop a direct writer from reopening."""

        deficiency_id, first, second = self.failed_pair()
        other = self.failed(self.special, deficiency={"existing_id": deficiency_id})
        other_root = other.body["check_id"]
        self.correct(second)  # notice: the first chain still fails
        tip = self.correct(other_root).body["check_id"]  # notice too
        self.correct(first)  # nothing else fails now: Reported in Error, citing the first chain
        cancellation = DeficiencyEvent.objects.get(
            deficiency_id=deficiency_id, event_type="REPORTED_IN_ERROR"
        )
        self.assertEqual(cancellation.correction_ref, first)
        # A later correction command that has no effect (the deficiency is already cancelled)
        # supplies a real receipt for a neutral event that leaves the status unchanged.
        later = self.correct(other_root, tip).body["check_id"]
        receipt = CheckEvent.objects.get(replacement_check_id=later).command
        neutral = DeficiencyEvent.objects.create(
            organization_id=self.org.pk,
            deficiency_id=deficiency_id,
            sequence=cancellation.sequence + 1,
            predecessor_event=cancellation,
            event_type="CORRECTION_NOTICE",
            from_status="REPORTED_IN_ERROR",
            to_status="REPORTED_IN_ERROR",
            explanation="Synthetic.",
            correction_ref=other_root,
            command=receipt,
            recorded_at=receipt.recorded_at,
            actor_user=self.crew,
            actor_roles_at_time=["CREW"],
        )
        # The application sees that the last event is not this chain's cancellation: no reopening
        self.assertEqual(self.invalidate(first).body["reopened_deficiency_ids"], [])
        invalidation = CheckEvent.objects.get(root_check_id=first, event_type="INVALIDATION")
        reopening = {
            "organization_id": self.org.pk,
            "deficiency_id": deficiency_id,
            "sequence": neutral.sequence + 1,
            "event_type": "REOPENED",
            "from_status": "REPORTED_IN_ERROR",
            "to_status": "OPEN",
            "explanation": "Synthetic.",
            "correction_ref": first,
            "command": invalidation.command,
            "recorded_at": invalidation.command.recorded_at,
            "actor_user": self.admin,
            "actor_roles_at_time": ["ADMINISTRATOR"],
        }
        # ...and neither can the database: the matching cancellation is in the history, but it is
        # not the immediate predecessor.
        with self.assertRaises(DatabaseError), transaction.atomic():
            DeficiencyEvent.objects.create(predecessor_event=neutral, **reopening)
        self.assertEqual(self.status(deficiency_id), "REPORTED_IN_ERROR")
