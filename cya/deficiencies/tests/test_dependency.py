"""Awaiting External Dependency and progress notes (issue 149 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/149); SPEC 8.5, 8.1, 3.3 and 8.2)."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from cya.authorization.commands import change_role
from cya.checklists.tests.support import ids
from cya.checks.commands import correct_check
from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies import service
from cya.deficiencies.commands import assign_deficiency, set_deadline
from cya.deficiencies.dependency import (
    add_progress_note,
    end_dependency,
    enter_dependency,
    revise_dependency,
)
from cya.deficiencies.models import DeficiencyEvent
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.tests.test_assignment import LifecycleCase
from cya.notifications.models import NotificationOutbox
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

WAITING = "Waiting for the vendor to deliver a synthetic part."
NOTE = "Synthetic progress: the part was ordered."


class DependencyCase(LifecycleCase):
    def setUp(self):
        super().setUp()
        # Crew may act on any deficiency (SPEC 3.3), so the supply fact is observable only for
        # a Supply Officer who holds no Crew grant (every user starts with Crew).
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(self.supply.pk),
            role="CREW",
            action="REVOKE",
            **ids(),
        )

    def enter(self, text=WAITING, follow_up=None, **kw):
        return enter_dependency(explanation=text, follow_up_at=follow_up, **self.args(**kw))

    def revise(self, text="Vendor now expects the part next week.", follow_up=None, **kw):
        return revise_dependency(explanation=text, follow_up_at=follow_up, **self.args(**kw))

    def leave(self, return_to=None, **kw):
        return end_dependency(return_to=return_to, **self.args(**kw))

    def note(self, text=NOTE, **kw):
        return add_progress_note(note=text, **self.args(**kw))

    def events(self, event_type, deficiency_id=None):
        return DeficiencyEvent.objects.filter(
            deficiency_id=deficiency_id or self.deficiency_id, event_type=event_type
        ).order_by("sequence")

    def soon(self, days=3):
        return timezone.now() + timedelta(days=days)


class EnterDependencyTests(DependencyCase):
    def test_entering_writes_the_event_the_projection_the_receipt_and_an_ids_only_audit(self):
        before = self.snapshot()
        reported = DeficiencyEvent.objects.get(deficiency_id=self.deficiency_id, sequence=1)
        when = self.soon()
        result = self.enter(follow_up=when, actor=self.crew)
        self.assertEqual(result.status, 201)
        self.assertEqual(result.body["code"], "DEFICIENCY_DEPENDENCY_ENTERED")
        event = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest("sequence")
        self.assertEqual(result.body["event_id"], event.pk)
        self.assertEqual(
            (
                event.sequence,
                event.event_type,
                event.predecessor_event_id,
                event.from_status,
                event.to_status,
                event.explanation,
                event.follow_up_at,
                event.assignee_id,
                event.deadline,
            ),
            (
                2,
                "DEPENDENCY_ENTERED",
                reported.pk,
                "OPEN",
                "AWAITING_EXTERNAL_DEPENDENCY",
                WAITING,
                when,
                None,
                None,
            ),
        )
        self.assertEqual(event.actor_user_id, self.crew.pk)
        receipt = CommandReceipt.objects.get(pk=event.command_id)
        self.assertEqual(
            (receipt.command_type, event.recorded_at),
            ("DEFICIENCY_DEPENDENCY_ENTER", receipt.recorded_at),
        )
        projection = self.projection()
        self.assertEqual(
            (projection.status, projection.last_applied_event_id),
            ("AWAITING_EXTERNAL_DEPENDENCY", event.pk),
        )
        audit = AuditEvent.objects.get(action="DEFICIENCY_DEPENDENCY_ENTER")
        self.assertEqual(audit.outcome, "SUCCEEDED")
        self.assertEqual(
            audit.new_snapshot["values"],
            {
                "deficiency_id": self.deficiency_id,
                "event_id": event.pk,
                "event_type": "DEPENDENCY_ENTERED",
                "to_status": "AWAITING_EXTERNAL_DEPENDENCY",
            },
        )
        self.assertNotIn(WAITING, json.dumps([audit.new_snapshot, audit.prior_snapshot]))
        after = self.snapshot()
        self.assertEqual(after[1], before[1])  # the vehicle's status is untouched (OPS-2)
        self.assertEqual(after[0]["DeficiencyEvent"], before[0]["DeficiencyEvent"] + 1)

    def test_the_explanation_is_mandatory_and_bounded(self):
        before = self.snapshot()
        for bad in (None, "", "   \n", 5, "x" * 4001):
            self.refused("MALFORMED_REQUEST", 400, enter_dependency, **self.dep(bad))
        self.assertEqual(self.snapshot(), before)
        self.enter("x" * 4000)

    def dep(self, explanation):
        return {**self.args(), "explanation": explanation}

    def test_a_follow_up_date_is_optional_future_only_and_never_a_reminder(self):
        outbox = NotificationOutbox.objects.count()
        self.enter()
        self.assertIsNone(self.events("DEPENDENCY_ENTERED").get().follow_up_at)
        self.leave()
        before = self.snapshot()
        error = self.refused(
            "INVALID_FOLLOW_UP",
            422,
            self.enter,
            follow_up=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(error.path, "follow_up_at")
        naive = (timezone.now() + timedelta(days=1)).replace(tzinfo=None)
        for bad in (naive, "tomorrow", 5):
            self.refused(
                "MALFORMED_REQUEST",
                400,
                enter_dependency,
                **{**self.args(), "explanation": WAITING, "follow_up_at": bad},
            )
        self.assertEqual(self.snapshot(), before)
        self.enter(follow_up=self.soon())
        self.assertEqual(NotificationOutbox.objects.count(), outbox)

    def test_it_enters_from_open_and_in_progress_and_records_where_it_came_from(self):
        self.enter()
        self.assertEqual(self.events("DEPENDENCY_ENTERED").get().from_status, "OPEN")
        self.leave()
        self.start()
        self.enter()
        self.assertEqual(self.events("DEPENDENCY_ENTERED").last().from_status, "IN_PROGRESS")

    def test_there_is_one_current_record_so_a_second_entry_is_refused(self):
        self.enter()
        before = self.snapshot()
        error = self.refused("DEPENDENCY_ALREADY_RECORDED", 422, self.enter)
        self.assertEqual(error.path, "deficiency")
        self.assertEqual(self.snapshot(), before)

    def test_starting_work_does_not_resume_a_waiting_deficiency(self):
        self.enter()
        self.refused("INVALID_TRANSITION", 422, self.start)

    def test_a_closed_or_cancelled_deficiency_takes_no_dependency_or_note(self):
        failed = self.failed(self.siren)
        deficiency_id = failed.body["deficiency_ids"][0]
        correct_check(
            check_id=failed.body["check_id"],
            expected_tip_check_id=failed.body["check_id"],
            outcome="PASSED",
            **self.context(self.crew),
        )
        self.assertEqual(self.projection(deficiency_id).status, "REPORTED_IN_ERROR")
        before = self.snapshot()
        for call in (self.enter, self.note, self.revise, self.leave):
            error = self.refused("DEFICIENCY_NOT_ACTIVE", 422, call, deficiency_id=deficiency_id)
            self.assertEqual(error.path, "deficiency")
        self.assertEqual(self.snapshot(), before)


class ReviseAndEndTests(DependencyCase):
    def test_a_revision_is_a_new_event_and_every_record_stays_in_the_history(self):
        first_follow_up = self.soon(2)
        self.enter(follow_up=first_follow_up)
        self.revise("Second wording.", follow_up=self.soon(5), actor=self.crew)
        self.revise("Third wording.", follow_up=None, actor=self.admin)
        events = list(self.events("DEPENDENCY_REVISED"))
        self.assertEqual(
            [(e.explanation, e.from_status, e.to_status) for e in events],
            [
                ("Second wording.", "AWAITING_EXTERNAL_DEPENDENCY", "AWAITING_EXTERNAL_DEPENDENCY"),
                ("Third wording.", "AWAITING_EXTERNAL_DEPENDENCY", "AWAITING_EXTERNAL_DEPENDENCY"),
            ],
        )
        self.assertIsNone(events[1].follow_up_at)  # a revision states the whole record
        state = service.dependency_state(str(self.org.pk), self.deficiency_id)
        self.assertEqual(state["dependency"]["explanation"], "Third wording.")
        self.assertIsNone(state["dependency"]["follow_up_at"])
        self.assertEqual(
            [h["explanation"] for h in state["history"]],
            [WAITING, "Second wording.", "Third wording."],
        )
        self.assertEqual(self.events("DEPENDENCY_ENTERED").get().explanation, WAITING)
        self.assertEqual(self.events("DEPENDENCY_ENTERED").get().follow_up_at, first_follow_up)
        self.assertEqual(self.projection().status, "AWAITING_EXTERNAL_DEPENDENCY")

    def test_a_revision_must_change_something_and_keep_the_explanation(self):
        when = self.soon()
        self.enter(follow_up=when)
        before = self.snapshot()
        error = self.refused("DEPENDENCY_UNCHANGED", 422, self.revise, WAITING, follow_up=when)
        self.assertEqual(error.path, "explanation")
        for bad in (None, "", "  "):
            self.refused(
                "MALFORMED_REQUEST", 400, revise_dependency, **{**self.args(), "explanation": bad}
            )
        self.refused(
            "INVALID_FOLLOW_UP",
            422,
            self.revise,
            "Changed.",
            follow_up=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(self.snapshot(), before)
        self.revise(WAITING, follow_up=self.soon(9))  # only the date changes
        self.revise(WAITING, follow_up=None)  # and the date can be cleared

    def test_revising_or_ending_needs_a_current_record(self):
        before = self.snapshot()
        for call in (self.revise, self.leave):
            error = self.refused("NO_ACTIVE_DEPENDENCY", 422, call)
            self.assertEqual(error.path, "deficiency")
        self.enter()
        self.leave()
        for call in (self.revise, self.leave):
            self.refused("NO_ACTIVE_DEPENDENCY", 422, call)
        self.assertEqual(self.counts()["DeficiencyEvent"], before[0]["DeficiencyEvent"] + 2)

    def test_ending_returns_to_the_status_the_caller_names(self):
        self.enter()
        result = self.leave("IN_PROGRESS")
        self.assertEqual(result.body["status"], "IN_PROGRESS")
        ended = self.events("DEPENDENCY_ENDED").get()
        self.assertEqual(
            (ended.from_status, ended.to_status, ended.explanation, ended.follow_up_at),
            ("AWAITING_EXTERNAL_DEPENDENCY", "IN_PROGRESS", None, None),
        )
        self.assertEqual(self.projection().status, "IN_PROGRESS")
        self.enter()
        self.leave("OPEN")  # a status other than the one it left is the caller's choice
        self.assertEqual(self.projection().status, "OPEN")

    def test_ending_without_a_choice_returns_to_the_status_before_waiting(self):
        self.enter()
        self.assertEqual(self.leave().body["status"], "OPEN")
        self.start()
        self.enter()
        self.revise()  # a revision does not change where it returns to
        self.assertEqual(self.leave().body["status"], "IN_PROGRESS")
        self.assertEqual(self.projection().status, "IN_PROGRESS")

    def test_the_return_status_is_closed_to_open_and_in_progress(self):
        self.enter()
        before = self.snapshot()
        for bad in (
            "PENDING_VERIFICATION",
            "CLOSED_VERIFIED",
            "AWAITING_EXTERNAL_DEPENDENCY",
            "x",
            5,
        ):
            self.refused(
                "MALFORMED_REQUEST", 400, end_dependency, **{**self.args(), "return_to": bad}
            )
        self.assertEqual(self.snapshot(), before)

    def test_a_new_dependency_may_follow_an_ended_one_and_only_it_is_current(self):
        self.enter("First wait.")
        self.leave()
        self.enter("Second wait.")
        state = service.dependency_state(str(self.org.pk), self.deficiency_id)
        self.assertEqual(state["dependency"]["explanation"], "Second wait.")
        self.assertEqual(
            [h["event_type"] for h in state["history"]],
            ["DEPENDENCY_ENTERED", "DEPENDENCY_ENDED", "DEPENDENCY_ENTERED"],
        )


class DependencyPermissionTests(DependencyCase):
    def test_every_role_against_the_section_3_3_cells(self):
        before = self.snapshot()
        with self.assertRaises(CommandDenied):
            self.enter(actor=self.supply)  # a Supply Officer on the mechanical deficiency
        with self.assertRaises(CommandDenied):
            self.note(actor=self.supply)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            AuditEvent.objects.filter(
                action__in=("DEFICIENCY_DEPENDENCY_ENTER", "DEFICIENCY_PROGRESS_NOTE"),
                outcome="DENIED",
            ).count(),
            2,
        )
        # allowed cells: Crew, Supervisor and Administrator anywhere, a Supply Officer on supply
        self.enter(actor=self.crew)
        self.revise(actor=self.supervisor)
        self.leave(actor=self.admin)
        self.note(actor=self.crew)
        self.note(actor=self.admin)
        for actor in (self.supply, self.crew, self.supervisor, self.admin):
            self.note(actor=actor, deficiency_id=self.supply_id)
        self.enter(actor=self.supply, deficiency_id=self.supply_id)
        self.revise(actor=self.supply, deficiency_id=self.supply_id)
        self.leave(actor=self.supply, deficiency_id=self.supply_id)

    def test_the_supply_fact_comes_from_the_category_revision_not_the_request(self):
        self.category(
            "Synthetic reclassified",
            supply_related=True,
            revision=2,
            stable=self.plain.stable_category_id,
            when=self.now - timedelta(hours=1),
        )
        with self.assertRaises(CommandDenied):
            self.enter(actor=self.supply)

    def test_a_denial_reveals_nothing_about_the_deficiency(self):
        unknown, seen = str(uuid4()), str(uuid4())
        for call in (self.enter, self.note):
            with self.assertRaises(CommandDenied):
                call(actor=self.supply, deficiency_id=unknown, expected=seen)
            error = self.refused("NOT_FOUND", 404, call, deficiency_id=unknown, expected=seen)
            self.assertIsNone(error.path)

    def test_crew_cannot_move_the_deadline_the_assignee_or_the_escalation(self):
        set_deadline(deadline=self.soon(2), **self.args())
        assign_deficiency(assignee_id=str(self.supply.pk), **self.args())
        projection = self.projection()
        deadline, assignee = projection.deadline, projection.assignee_id
        self.enter(actor=self.crew)
        self.revise(actor=self.crew)
        self.leave(actor=self.crew)
        self.note(actor=self.crew)
        projection = self.projection()
        self.assertEqual((projection.deadline, projection.assignee_id), (deadline, assignee))
        for event in DeficiencyEvent.objects.filter(
            deficiency_id=self.deficiency_id, event_type__regex="^(DEPENDENCY|PROGRESS)"
        ):
            self.assertEqual(
                (event.deadline, event.assignee_id, event.proposed_deadline), (None,) * 3
            )
        # and the commands take no such argument
        with self.assertRaises(TypeError):
            enter_dependency(explanation=WAITING, deadline=self.soon(), **self.args())


class DependencyReplayTests(DependencyCase):
    def test_a_stale_expected_event_is_refused_for_every_command(self):
        seen = self.newest()
        self.enter()
        for call in (self.enter, self.revise, self.leave, self.note):
            error = self.refused("DEFICIENCY_CHANGED", 409, call, expected=seen)
            self.assertEqual(error.path, "expected_event_id")
        self.refused("DEFICIENCY_CHANGED", 409, self.note, expected=str(uuid4()))
        self.note()  # naming the newest event works

    def test_a_replay_returns_the_stored_answer_and_a_changed_request_is_reused(self):
        operation_id, expected = str(uuid4()), self.newest()
        first = enter_dependency(
            explanation=WAITING, **self.args(expected=expected, operation_id=operation_id)
        )
        counts = self.counts()
        again = enter_dependency(
            explanation=WAITING, **self.args(expected=expected, operation_id=operation_id)
        )
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.counts(), counts)
        different = enter_dependency(
            explanation="A different reason.",
            **self.args(expected=expected, operation_id=operation_id),
        )
        self.assertTrue(different.operation_reused)
        self.assertEqual(self.counts(), counts)
        note_op, seen = str(uuid4()), self.newest()
        note = add_progress_note(note=NOTE, **self.args(operation_id=note_op, expected=seen))
        counts = self.counts()
        replay = add_progress_note(
            note=NOTE, **self.args(operation_id=note_op, expected=seen)
        )  # the event it named is no longer the newest, yet the stored answer is returned
        self.assertEqual((replay.body, replay.replayed), (note.body, True))
        self.assertEqual(self.counts(), counts)

    def test_malformed_identifiers_are_refused_without_a_write(self):
        before = self.snapshot()
        for call, extra in (
            (enter_dependency, {"explanation": WAITING}),
            (revise_dependency, {"explanation": WAITING}),
            (end_dependency, {}),
            (add_progress_note, {"note": NOTE}),
        ):
            self.refused(
                "MALFORMED_REQUEST", 400, call, **{**self.args(), **extra, "expected_event_id": "x"}
            )
            self.refused(
                "MALFORMED_REQUEST", 400, call, **{**self.args(), **extra, "deficiency_id": 5}
            )
            self.refused(
                "MALFORMED_REQUEST", 400, call, **{**self.args(), **extra, "operation_id": "x"}
            )
        self.assertEqual(self.snapshot(), before)

    def test_a_failure_while_writing_rolls_everything_back_and_the_operation_id_is_reusable(self):
        for call, kwargs in (
            (enter_dependency, {"explanation": WAITING}),
            (add_progress_note, {"note": NOTE}),
        ):
            operation_id, expected = str(uuid4()), self.newest()
            before = self.snapshot()
            with (
                mock.patch(
                    "cya.live_updates.publish.publish_vehicle_update",
                    side_effect=RuntimeError("boom"),
                ),
                self.assertRaises(RuntimeError),
            ):
                call(**kwargs, **self.args(expected=expected, operation_id=operation_id))
            self.assertEqual(self.snapshot(), before)
            call(**kwargs, **self.args(expected=expected, operation_id=operation_id))
        self.assertEqual(self.projection().status, "AWAITING_EXTERNAL_DEPENDENCY")
        self.assertEqual(len(self.events("PROGRESS_NOTE")), 1)

    def test_each_command_publishes_the_identifier_only_live_hint_after_commit(self):
        with mock.patch("cya.live_updates.publish.publish_vehicle_update") as publish:
            self.enter()
            self.revise()
            self.note()
            self.leave()
        self.assertEqual(publish.call_count, 4)
        for call in publish.call_args_list:
            self.assertEqual(call.kwargs["kind"], "deficiency.changed")
            self.assertEqual(
                set(call.kwargs), {"organization_id", "vehicle_id", "event_id", "kind"}
            )

    def test_no_command_changes_the_vehicle_status_the_links_or_the_evidence(self):
        before = self.snapshot()
        self.enter()
        self.revise()
        self.note()
        self.leave()
        after = self.snapshot()
        self.assertEqual(after[1], before[1])
        for name in (
            "DeficiencyEvidence",
            "DeficiencyLink",
            "Deficiency",
            "DeficiencyAffectedItem",
        ):
            self.assertEqual(after[0][name], before[0][name])


class ProgressNoteTests(DependencyCase):
    def test_a_note_is_timestamped_attributed_and_changes_nothing_else(self):
        self.assign()
        self.deadline()
        before = self.snapshot()
        projection = self.projection()
        result = self.note(actor=self.crew)
        self.assertEqual(result.body["code"], "DEFICIENCY_PROGRESS_NOTE_ADDED")
        event = self.events("PROGRESS_NOTE").get()
        self.assertEqual(
            (event.explanation, event.from_status, event.to_status, event.follow_up_at),
            (NOTE, "OPEN", "OPEN", None),
        )
        self.assertEqual(event.actor_user_id, self.crew.pk)
        receipt = CommandReceipt.objects.get(pk=event.command_id)
        self.assertEqual(
            (receipt.command_type, event.recorded_at),
            ("DEFICIENCY_PROGRESS_NOTE", receipt.recorded_at),
        )
        after = self.projection()
        self.assertEqual(
            (after.status, after.assignee_id, after.deadline),
            (projection.status, projection.assignee_id, projection.deadline),
        )
        self.assertEqual(after.last_applied_event_id, event.pk)
        audit = AuditEvent.objects.get(action="DEFICIENCY_PROGRESS_NOTE")
        self.assertNotIn(NOTE, json.dumps([audit.new_snapshot, audit.prior_snapshot]))
        self.assertEqual(self.snapshot()[1], before[1])

    def test_notes_accumulate_in_order_and_none_is_overwritten(self):
        for text in ("one", "two", "three"):
            self.note(text)
        view = service.progress_notes(str(self.org.pk), self.deficiency_id)
        self.assertEqual([n["note"] for n in view["notes"]], ["one", "two", "three"])
        self.assertEqual(view["last_event_id"], self.newest())

    def test_the_text_is_mandatory_and_bounded(self):
        before = self.snapshot()
        for bad in (None, "", "  \t", 5, "x" * 4001):
            self.refused(
                "MALFORMED_REQUEST", 400, add_progress_note, **{**self.args(), "note": bad}
            )
        self.assertEqual(self.snapshot(), before)
        self.note("x" * 4000)

    def test_a_note_is_allowed_in_every_working_status(self):
        self.note()
        self.start()
        self.note()
        self.enter()
        self.note()
        self.assertEqual(self.projection().status, "AWAITING_EXTERNAL_DEPENDENCY")
        self.assertEqual(len(self.events("PROGRESS_NOTE")), 3)


class DependencyRaceTests(DependencyCase):
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

    def test_concurrent_notes_and_dependency_changes_from_one_view_commit_exactly_one(self):
        expected = self.newest()
        outcomes = self.run_threads(
            [
                lambda: self.note("a", expected=expected),
                lambda: self.note("b", expected=expected, actor=self.crew),
                lambda: self.enter(expected=expected),
            ]
        )
        done = [o for o in outcomes if hasattr(o, "body")]
        stale = [o for o in outcomes if isinstance(o, DeficiencyError)]
        self.assertEqual((len(done), len(stale)), (1, 2), outcomes)
        self.assertEqual({o.code for o in stale}, {"DEFICIENCY_CHANGED"})
        self.assertEqual(
            list(
                DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id)
                .order_by("sequence")
                .values_list("sequence", flat=True)
            ),
            [1, 2],
        )
        self.assertEqual(str(self.projection().last_applied_event_id), self.newest())

    def test_a_revision_and_an_exit_from_one_view_commit_exactly_one(self):
        self.enter()
        expected = self.newest()
        outcomes = self.run_threads(
            [
                lambda: self.revise("Changed.", expected=expected),
                lambda: self.leave(expected=expected),
            ]
        )
        self.assertEqual(sum(hasattr(o, "body") for o in outcomes), 1, outcomes)
        self.assertEqual(
            [
                e.sequence
                for e in DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).order_by(
                    "sequence"
                )
            ],
            [1, 2, 3],
        )
        self.assertEqual(str(self.projection().last_applied_event_id), self.newest())


class DependencyCorrectionEffectTests(DependencyCase):
    """Author corrections after dependency events and notes (SPEC COR-4, AC-13)."""

    def failed_deficiency(self):
        failed = self.failed(self.siren)
        return failed.body["check_id"], failed.body["deficiency_ids"][0]

    def correct(self, check_id):
        correct_check(
            check_id=check_id,
            expected_tip_check_id=check_id,
            outcome="PASSED",
            **self.context(self.crew),
        )

    def test_a_deficiency_still_awaiting_stays_open_with_a_notice(self):
        check_id, deficiency_id = self.failed_deficiency()
        self.enter(deficiency_id=deficiency_id)
        self.correct(check_id)
        self.assertEqual(self.projection(deficiency_id).status, "AWAITING_EXTERNAL_DEPENDENCY")
        self.assertEqual(self.events("CORRECTION_NOTICE", deficiency_id).count(), 1)
        self.assertEqual(self.events("REPORTED_IN_ERROR", deficiency_id).count(), 0)

    def test_waiting_that_has_ended_records_no_repair_so_the_correction_still_cancels(self):
        check_id, deficiency_id = self.failed_deficiency()
        self.enter(deficiency_id=deficiency_id)
        self.revise(deficiency_id=deficiency_id)
        self.leave(deficiency_id=deficiency_id)
        self.correct(check_id)
        self.assertEqual(self.projection(deficiency_id).status, "REPORTED_IN_ERROR")

    def test_a_progress_note_may_describe_repair_so_it_blocks_the_cancellation(self):
        check_id, deficiency_id = self.failed_deficiency()
        self.note("Synthetic: the hose was replaced.", deficiency_id=deficiency_id)
        self.correct(check_id)
        self.assertEqual(self.projection(deficiency_id).status, "OPEN")
        self.assertEqual(self.events("CORRECTION_NOTICE", deficiency_id).count(), 1)


class DependencyReaderTests(DependencyCase):
    def test_the_record_is_not_current_once_another_event_has_left_the_status(self):
        # No other event type leaves Awaiting External Dependency yet; administrator closure
        # (issue 152 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/152)) will, so the reader is checked with a newest event that does.
        self.enter(actor=self.crew)
        real = service._events
        for status in ("CLOSED_ADMINISTRATIVE", "OPEN"):

            def leaving(*args, status=status, **kw):
                newest, rows = real(*args, **kw)
                return SimpleNamespace(pk=newest.pk, to_status=status), rows

            with mock.patch.object(service, "_events", leaving):
                state = service.dependency_state(str(self.org.pk), self.deficiency_id)
            self.assertEqual(
                (state["awaiting_external_dependency"], state["dependency"]), (False, None)
            )
            self.assertEqual([h["event_type"] for h in state["history"]], ["DEPENDENCY_ENTERED"])

    def test_the_reader_replays_the_events_and_the_status_is_its_cache(self):
        state = service.dependency_state(str(self.org.pk), self.deficiency_id)
        self.assertEqual(
            (state["awaiting_external_dependency"], state["dependency"], state["history"]),
            (False, None, []),
        )
        when = self.soon()
        self.enter(follow_up=when, actor=self.crew)
        self.revise("Revised.", follow_up=when + timedelta(days=1), actor=self.supervisor)
        state = service.dependency_state(str(self.org.pk), self.deficiency_id)
        record = state["dependency"]
        self.assertEqual(
            (
                state["deficiency_status"],
                state["awaiting_external_dependency"],
                record["explanation"],
                record["follow_up_at"],
                record["entered_by"],
                record["revised_by"],
                record["returns_to"],
                state["last_event_id"],
            ),
            (
                self.projection().status,
                True,
                "Revised.",
                when + timedelta(days=1),
                str(self.crew.pk),
                str(self.supervisor.pk),
                "OPEN",
                self.newest(),
            ),
        )
        self.leave()
        state = service.dependency_state(str(self.org.pk), self.deficiency_id)
        self.assertEqual(
            (state["deficiency_status"], state["dependency"], len(state["history"])),
            (self.projection().status, None, 3),
        )

    def test_as_of_answers_for_that_instant_whatever_happened_later(self):
        first = self.enter().body["event_id"]
        at = DeficiencyEvent.objects.get(pk=first).recorded_at
        self.revise("Later wording.")
        self.leave()
        then = service.dependency_state(str(self.org.pk), self.deficiency_id, at)
        self.assertEqual(
            (then["dependency"]["explanation"], then["last_event_id"], len(then["history"])),
            (WAITING, str(first), 1),
        )
        before_report = DeficiencyEvent.objects.get(
            deficiency_id=self.deficiency_id, sequence=1
        ).recorded_at - timedelta(seconds=1)
        with self.assertRaises(DeficiencyError) as caught:
            service.dependency_state(str(self.org.pk), self.deficiency_id, before_report)
        self.assertEqual(caught.exception.status, 404)

    def test_an_unknown_deficiency_is_not_found_and_an_oversized_history_is_refused(self):
        for read in (service.dependency_state, service.progress_notes):
            with self.assertRaises(DeficiencyError) as caught:
                read(str(self.org.pk), str(uuid4()))
            self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))
        self.note()
        self.note()
        self.enter()
        self.revise()
        with mock.patch.object(service, "MAX_PROGRESS_NOTES", 1):
            with self.assertRaises(DeficiencyError) as caught:
                service.progress_notes(str(self.org.pk), self.deficiency_id)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("CONFIGURATION_UNAVAILABLE", 503)
        )
        with mock.patch.object(service, "MAX_DEPENDENCY_EVENTS", 1):
            with self.assertRaises(DeficiencyError) as caught:
                service.dependency_state(str(self.org.pk), self.deficiency_id)
        self.assertEqual(caught.exception.status, 503)


class DependencyGuardTests(DependencyCase):
    """The database refuses what the commands would not write (migration 0007)."""

    def setUp(self):
        super().setUp()
        self.last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest(
            "sequence"
        )

    def receipt(self, command_type, actor=None):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=actor or self.crew,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def event(self, command_type="DEFICIENCY_DEPENDENCY_ENTER", deficiency_id=None, **overrides):
        receipt = self.receipt(command_type)
        last = (
            self.last
            if deficiency_id is None
            else DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        )
        fields = {
            "organization_id": self.org.pk,
            "deficiency_id": deficiency_id or self.deficiency_id,
            "sequence": last.sequence + 1,
            "predecessor_event": last,
            "event_type": "DEPENDENCY_ENTERED",
            "from_status": last.to_status,
            "to_status": "AWAITING_EXTERNAL_DEPENDENCY",
            "explanation": WAITING,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.crew,
            "actor_roles_at_time": ["CREW"],
            **overrides,
        }
        return DeficiencyEvent.objects.create(**fields)

    def refused_event(self, *args, **kwargs):
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.event(*args, **kwargs)

    def waiting(self):
        """Move the chain to Awaiting External Dependency through the command."""
        self.enter()
        self.last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest(
            "sequence"
        )

    def note_fields(self, **overrides):
        return {
            "event_type": "PROGRESS_NOTE",
            "to_status": "OPEN",
            "explanation": NOTE,
            **overrides,
        }

    def test_valid_events_are_accepted_so_the_refusals_below_are_meaningful(self):
        with transaction.atomic():
            self.event(follow_up_at=timezone.now() + timedelta(days=1))
        self.last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest(
            "sequence"
        )
        for command_type, fields in (
            (
                "DEFICIENCY_DEPENDENCY_REVISE",
                {"event_type": "DEPENDENCY_REVISED", "explanation": "Revised."},
            ),
            (
                "DEFICIENCY_PROGRESS_NOTE",
                self.note_fields(
                    from_status="AWAITING_EXTERNAL_DEPENDENCY",
                    to_status="AWAITING_EXTERNAL_DEPENDENCY",
                ),
            ),
            (
                "DEFICIENCY_DEPENDENCY_END",
                {
                    "event_type": "DEPENDENCY_ENDED",
                    "explanation": None,
                    "to_status": "IN_PROGRESS",
                },
            ),
        ):
            with transaction.atomic():
                self.event(command_type, **fields)
            self.last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest(
                "sequence"
            )

    def test_each_event_needs_its_own_command_type(self):
        self.refused_event("CHECK_CORRECTION")
        self.refused_event("DEFICIENCY_ASSIGN")
        self.refused_event("DEFICIENCY_PROGRESS_NOTE")  # a dependency written by the note command
        self.refused_event("DEFICIENCY_DEPENDENCY_ENTER", **self.note_fields())
        self.refused_event("DEFICIENCY_DEPENDENCY_REVISE", **self.note_fields())

    def test_the_actor_must_hold_the_permission_in_the_role_snapshot(self):
        for fields in ({}, self.note_fields()):
            command = "DEFICIENCY_PROGRESS_NOTE" if fields else "DEFICIENCY_DEPENDENCY_ENTER"
            self.refused_event(command, actor_roles_at_time=[], **fields)
            self.refused_event(command, actor_roles_at_time=["SUPPLY_OFFICER"], **fields)
        with transaction.atomic():
            self.event(actor_roles_at_time=["SUPERVISOR"])

    def test_a_supply_officer_and_the_legacy_crew_grant_are_accepted_where_the_matrix_allows(self):
        with transaction.atomic():
            self.event(deficiency_id=self.supply_id, actor_roles_at_time=["SUPPLY_OFFICER"])
        with transaction.atomic():
            self.event(actor_roles_at_time=["EMT"])

    def test_the_shape_is_closed(self):
        later = timezone.now() + timedelta(days=1)
        self.refused_event(explanation=None)
        self.refused_event(explanation="")
        self.refused_event(assignee=self.crew)
        self.refused_event(deadline=later)
        self.refused_event(proposed_deadline=later)
        self.refused_event(correction_ref=str(uuid4()))
        self.refused_event(action_plan="Synthetic plan.")
        self.refused_event(acknowledgement_until=later)
        self.refused_event(to_status="OPEN")
        self.refused_event(to_status="IN_PROGRESS")
        self.refused_event(
            from_status="PENDING_VERIFICATION", to_status="AWAITING_EXTERNAL_DEPENDENCY"
        )
        self.refused_event(from_status="CLOSED_VERIFIED", to_status="AWAITING_EXTERNAL_DEPENDENCY")
        self.refused_event(
            "DEFICIENCY_DEPENDENCY_REVISE", event_type="DEPENDENCY_REVISED", from_status="OPEN"
        )
        self.refused_event(
            "DEFICIENCY_DEPENDENCY_END", event_type="DEPENDENCY_ENDED", explanation=None
        )

    def test_a_follow_up_date_lies_after_the_instant_it_is_set(self):
        self.refused_event(follow_up_at=timezone.now() - timedelta(days=1))
        self.refused_event(follow_up_at=timezone.now() - timedelta(microseconds=1))

    def test_ending_carries_nothing_and_returns_only_to_open_or_in_progress(self):
        self.waiting()
        end = {"event_type": "DEPENDENCY_ENDED", "explanation": None}
        self.refused_event(
            "DEFICIENCY_DEPENDENCY_END", **{**end, "to_status": "PENDING_VERIFICATION"}
        )
        self.refused_event("DEFICIENCY_DEPENDENCY_END", **{**end, "to_status": "CLOSED_VERIFIED"})
        self.refused_event(
            "DEFICIENCY_DEPENDENCY_END", **{**end, "to_status": "AWAITING_EXTERNAL_DEPENDENCY"}
        )
        self.refused_event(
            "DEFICIENCY_DEPENDENCY_END", **{**end, "to_status": "OPEN", "explanation": "Why."}
        )
        self.refused_event(
            "DEFICIENCY_DEPENDENCY_END",
            **{**end, "to_status": "OPEN", "follow_up_at": timezone.now() + timedelta(days=1)},
        )
        with transaction.atomic():
            self.event("DEFICIENCY_DEPENDENCY_END", **{**end, "to_status": "OPEN"})

    def test_a_note_keeps_the_status_and_carries_no_follow_up(self):
        command = "DEFICIENCY_PROGRESS_NOTE"
        self.refused_event(command, **self.note_fields(to_status="IN_PROGRESS"))
        self.refused_event(command, **self.note_fields(explanation=None))
        self.refused_event(command, **self.note_fields(explanation=""))
        self.refused_event(
            command, **self.note_fields(follow_up_at=timezone.now() + timedelta(days=1))
        )
        self.refused_event(command, **self.note_fields(assignee=self.crew))
        self.refused_event(
            command, **self.note_fields(from_status="CLOSED_VERIFIED", to_status="CLOSED_VERIFIED")
        )
        self.refused_event(
            command,
            **self.note_fields(from_status="REPORTED_IN_ERROR", to_status="REPORTED_IN_ERROR"),
        )
        with transaction.atomic():
            self.event(command, **self.note_fields())

    def test_the_other_types_still_carry_no_follow_up(self):
        later = timezone.now() + timedelta(days=1)
        self.refused_event(
            "DEFICIENCY_ASSIGN",
            event_type="ASSIGNED",
            assignee=self.crew,
            to_status="OPEN",
            explanation=None,
            follow_up_at=later,
            actor_roles_at_time=["SUPERVISOR"],
        )

    def test_a_forked_or_skipped_chain_is_still_refused(self):
        self.refused_event(sequence=self.last.sequence + 2)
        self.refused_event(
            predecessor_event=DeficiencyEvent.objects.filter(sequence=1).first(), sequence=1
        )
        self.waiting()
        # the predecessor left the deficiency awaiting, so a note claiming Open does not follow
        self.refused_event("DEFICIENCY_PROGRESS_NOTE", **self.note_fields(from_status="OPEN"))
        # and a second entry cannot follow the first
        self.refused_event(from_status="AWAITING_EXTERNAL_DEPENDENCY")


@override_settings(**real_collaborator_settings())
class DependencyApiTests(DependencyCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.crew_client, self.crew_user = self.harness.sign_in("crew-http")
        self.sup_client, self.sup_user = self.harness.sign_in("supervisor-http")
        self.grant(self.sup_user, "SUPERVISOR")
        self.base = f"/api/v1/deficiencies/{self.deficiency_id}"

    def post(self, client, path, body=None):
        client.get("/")
        token = client.cookies["csrftoken"].value
        if not isinstance(body, (str, bytes)):
            body = {"operation_id": str(uuid4()), "expected_event_id": self.newest()} | (body or {})
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return client.post(
            f"{self.base}/{path}",
            data=data,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    def rejected(self, response, status, code, paths=()):
        self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.assertEqual(
            [(e["path"], e["code"]) for e in response.json()["field_errors"]],
            [tuple(p) for p in paths],
        )

    def test_crew_enters_revises_and_leaves_and_every_reader_shows_it(self):
        when = (timezone.now() + timedelta(days=2)).isoformat()
        entered = self.post(
            self.crew_client, "dependencies", {"explanation": WAITING, "follow_up_at": when}
        )
        payload = entered.json()
        self.assertEqual((entered.status_code, payload["status"]), (201, "COMMITTED"))
        self.assertEqual(payload["result"]["status"], "AWAITING_EXTERNAL_DEPENDENCY")
        self.assertTrue(payload["result"]["follow_up_at"])
        self.assertTrue(payload["result"]["server_recorded_at"].endswith("Z"))
        revised = self.post(self.crew_client, "dependency-revisions", {"explanation": "Revised."})
        self.assertEqual(revised.status_code, 201)
        view = self.sup_client.get(f"{self.base}/dependency").json()  # any member reads
        self.assertEqual(
            (view["deficiency_status"], view["dependency"]["explanation"], view["last_event_id"]),
            (
                "AWAITING_EXTERNAL_DEPENDENCY",
                "Revised.",
                revised.json()["result"]["event_id"],
            ),
        )
        self.assertIsNone(view["dependency"]["follow_up_at"])
        self.assertTrue(view["dependency"]["entered_at"].endswith("Z"))
        self.assertEqual(
            [h["event_type"] for h in view["history"]], ["DEPENDENCY_ENTERED", "DEPENDENCY_REVISED"]
        )
        self.assertTrue(view["history"][0]["follow_up_at"].endswith("Z"))
        left = self.post(self.crew_client, "dependency-exits", {"return_to": "IN_PROGRESS"})
        self.assertEqual(left.json()["result"]["status"], "IN_PROGRESS")
        after = self.crew_client.get(f"{self.base}/dependency").json()
        self.assertEqual(
            (after["dependency"], after["awaiting_external_dependency"]), (None, False)
        )
        default = self.post(self.sup_client, "dependencies", {"explanation": "Again."})
        self.assertEqual(default.status_code, 201)
        self.assertEqual(
            self.post(self.sup_client, "dependency-exits").json()["result"]["status"],
            "IN_PROGRESS",
        )

    def test_notes_are_added_and_listed_with_author_and_time(self):
        added = self.post(self.crew_client, "progress-notes", {"note": NOTE})
        self.assertEqual((added.status_code, added.json()["status"]), (201, "COMMITTED"))
        self.assertEqual(added.json()["result"]["status"], "OPEN")
        self.post(self.sup_client, "progress-notes", {"note": "Second note."})
        view = self.crew_client.get(f"{self.base}/progress-notes").json()
        self.assertEqual([n["note"] for n in view["notes"]], [NOTE, "Second note."])
        self.assertEqual(
            [n["actor_user_id"] for n in view["notes"]],
            [str(self.crew_user.pk), str(self.sup_user.pk)],
        )
        self.assertTrue(view["notes"][0]["recorded_at"].endswith("Z"))
        self.assertEqual(view["last_event_id"], self.newest())

    def test_the_readers_answer_as_of_an_instant(self):
        first = self.post(self.crew_client, "progress-notes", {"note": NOTE}).json()["result"]
        self.post(self.crew_client, "progress-notes", {"note": "Later."})
        then = self.crew_client.get(
            f"{self.base}/progress-notes", {"as_of": first["server_recorded_at"]}
        ).json()
        self.assertEqual([n["note"] for n in then["notes"]], [NOTE])
        for path in ("dependency", "progress-notes"):
            for query in ({"as_of": "yesterday"}, {"as_of": "2026-01-01T00:00:00"}, {"x": "1"}):
                self.assertEqual(
                    self.crew_client.get(f"{self.base}/{path}", query).status_code, 400, query
                )
            early = self.crew_client.get(f"{self.base}/{path}", {"as_of": "2000-01-01T00:00:00Z"})
            self.assertEqual((early.status_code, early.json()["code"]), (404, "NOT_FOUND"))

    def test_a_replay_is_acknowledged_again_without_a_second_event(self):
        body = {
            "operation_id": str(uuid4()),
            "note": NOTE,
            "expected_event_id": self.newest(),
        }
        first = self.post(self.crew_client, "progress-notes", body).json()
        events = DeficiencyEvent.objects.count()
        self.assertEqual(self.post(self.crew_client, "progress-notes", body).json(), first)
        self.assertEqual(DeficiencyEvent.objects.count(), events)
        body["note"] = "Different."
        self.rejected(
            self.post(self.crew_client, "progress-notes", body),
            409,
            "OPERATION_REUSED",
            [("operation_id", "REUSED")],
        )

    def test_signed_out_visitors_are_not_authenticated_and_unknown_ids_are_not_found(self):
        from django.test import Client

        events = DeficiencyEvent.objects.count()
        for path in ("dependencies", "dependency-revisions", "dependency-exits", "progress-notes"):
            response = Client().post(f"{self.base}/{path}", "{}", content_type="application/json")
            self.assertEqual(response.status_code, 401, path)
        for path in ("dependency", "progress-notes"):
            self.assertEqual(Client().get(f"{self.base}/{path}").status_code, 401, path)
        self.base = f"/api/v1/deficiencies/{uuid4()}"
        self.rejected(
            self.post(self.crew_client, "progress-notes", {"note": NOTE}), 404, "NOT_FOUND"
        )
        self.assertEqual(self.crew_client.get(f"{self.base}/dependency").status_code, 404)
        self.assertEqual(DeficiencyEvent.objects.count(), events)

    def test_refusals_carry_the_stable_codes(self):
        self.rejected(
            self.post(
                self.crew_client,
                "dependencies",
                {"explanation": WAITING, "expected_event_id": str(uuid4())},
            ),
            409,
            "DEFICIENCY_CHANGED",
            [("expected_event_id", "DEFICIENCY_CHANGED")],
        )
        self.rejected(
            self.post(self.crew_client, "dependency-revisions", {"explanation": WAITING}),
            422,
            "NO_ACTIVE_DEPENDENCY",
            [("deficiency", "NO_ACTIVE_DEPENDENCY")],
        )
        self.rejected(
            self.post(self.crew_client, "dependency-exits"),
            422,
            "NO_ACTIVE_DEPENDENCY",
            [("deficiency", "NO_ACTIVE_DEPENDENCY")],
        )
        self.post(self.crew_client, "dependencies", {"explanation": WAITING})
        self.rejected(
            self.post(self.crew_client, "dependencies", {"explanation": WAITING}),
            422,
            "DEPENDENCY_ALREADY_RECORDED",
            [("deficiency", "DEPENDENCY_ALREADY_RECORDED")],
        )
        self.rejected(
            self.post(self.crew_client, "dependency-revisions", {"explanation": WAITING}),
            422,
            "DEPENDENCY_UNCHANGED",
            [("explanation", "DEPENDENCY_UNCHANGED")],
        )
        self.rejected(
            self.post(
                self.crew_client,
                "dependency-revisions",
                {
                    "explanation": "New.",
                    "follow_up_at": (timezone.now() - timedelta(hours=1)).isoformat(),
                },
            ),
            422,
            "INVALID_FOLLOW_UP",
            [("follow_up_at", "INVALID_FOLLOW_UP")],
        )

    def test_malformed_bodies_are_rejected_before_any_write(self):
        events = DeficiencyEvent.objects.count()
        self.rejected(self.post(self.crew_client, "progress-notes", "[]"), 400, "MALFORMED_REQUEST")
        self.rejected(self.post(self.crew_client, "dependencies", "{"), 400, "MALFORMED_REQUEST")
        self.rejected(
            self.post(self.crew_client, "dependencies", {"explanation": "  "}),
            400,
            "MALFORMED_REQUEST",
            [("explanation", "MALFORMED_REQUEST")],
        )
        self.rejected(
            self.post(self.crew_client, "dependencies", {"explanation": WAITING, "deadline": "x"}),
            400,
            "MALFORMED_REQUEST",
            [("$", "UNKNOWN_FIELD")],
        )
        self.rejected(
            self.post(
                self.crew_client, "dependencies", {"explanation": WAITING, "assignee_id": "x"}
            ),
            400,
            "MALFORMED_REQUEST",
            [("$", "UNKNOWN_FIELD")],
        )
        self.rejected(
            self.post(
                self.crew_client, "dependencies", {"explanation": WAITING, "follow_up_at": "soon"}
            ),
            400,
            "MALFORMED_REQUEST",
            [("follow_up_at", "INVALID")],
        )
        self.rejected(
            self.post(
                self.crew_client,
                "dependencies",
                {"explanation": WAITING, "follow_up_at": "2030-01-01T00:00:00"},
            ),
            400,
            "MALFORMED_REQUEST",
            [("follow_up_at", "MALFORMED_REQUEST")],
        )
        self.rejected(
            self.post(self.crew_client, "dependency-exits", {"return_to": "CLOSED_VERIFIED"}),
            400,
            "MALFORMED_REQUEST",
            [("return_to", "MALFORMED_REQUEST")],
        )
        self.rejected(
            self.post(self.crew_client, "progress-notes", {"note": "x" * 4001}),
            400,
            "MALFORMED_REQUEST",
            [("note", "MALFORMED_REQUEST")],
        )
        self.assertEqual(DeficiencyEvent.objects.count(), events)
