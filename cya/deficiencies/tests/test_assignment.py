"""Assignment, deadlines and starting work (issue 146 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/146); SPEC DEF-4, section 3.3 and 8.2)."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from cya.accounts.models import Organization, User
from cya.authorization.commands import change_role
from cya.checklists.tests.support import ids
from cya.checks.commands import correct_check
from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies import service
from cya.deficiencies.commands import assign_deficiency, report_deficiency, set_deadline, start_work
from cya.deficiencies.models import DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.tests.support import DeficiencyCase
from cya.operations.models import VehicleStatusEvent
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


class LifecycleCase(DeficiencyCase):
    def setUp(self):
        super().setUp()
        self.deficiency_id = self.failed().body["deficiency_ids"][0]  # a mechanical category
        self.supply_id = self.failed(self.siren, create=self.new_fields(self.stock)).body[
            "deficiency_ids"
        ][0]

    def newest(self, deficiency_id=None):
        return str(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id or self.deficiency_id)
            .latest("sequence")
            .pk
        )

    def projection(self, deficiency_id=None):
        return DeficiencyProjection.objects.get(deficiency_id=deficiency_id or self.deficiency_id)

    def args(self, actor=None, deficiency_id=None, expected=None, **extra):
        deficiency_id = deficiency_id or self.deficiency_id
        return {
            "deficiency_id": deficiency_id,
            "expected_event_id": expected or self.newest(deficiency_id),
            **self.context(actor or self.supervisor),
            **extra,
        }

    def assign(self, assignee=None, **kw):
        return assign_deficiency(assignee_id=str((assignee or self.crew).pk), **self.args(**kw))

    def deadline(self, when=None, **kw):
        return set_deadline(deadline=when or timezone.now() + timedelta(days=2), **self.args(**kw))

    def start(self, **kw):
        return start_work(**self.args(**kw))

    def refused(self, code, status, call, *args, **kw):
        with self.assertRaises(DeficiencyError) as caught:
            call(*args, **kw)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        return caught.exception

    def snapshot(self):
        return (
            self.counts(),
            VehicleStatusEvent.objects.count(),
            list(DeficiencyProjection.objects.order_by("pk").values()),
        )

    def administrator_only(self):
        """An active user who holds Administrator and no working role (initial Crew revoked)."""
        user = self.make_user("admin-only")
        self.grant(user, "ADMINISTRATOR")
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(user.pk),
            role="CREW",
            action="REVOKE",
            **ids(),
        )
        return user


class AssignmentCommandTests(LifecycleCase):
    def test_a_supervisor_assigns_and_the_event_the_projection_and_audit_agree(self):
        before = self.snapshot()
        reported = DeficiencyEvent.objects.get(deficiency_id=self.deficiency_id, sequence=1)
        result = self.assign()
        self.assertEqual(result.status, 201)
        event = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest("sequence")
        self.assertEqual(result.body["event_id"], event.pk)
        self.assertEqual(result.body["code"], "DEFICIENCY_ASSIGNED")
        self.assertEqual(
            (
                event.sequence,
                event.event_type,
                event.predecessor_event_id,
                event.from_status,
                event.to_status,
                event.assignee_id,
                event.deadline,
            ),
            (2, "ASSIGNED", reported.pk, "OPEN", "OPEN", self.crew.pk, None),
        )
        self.assertEqual(event.actor_user_id, self.supervisor.pk)
        receipt = CommandReceipt.objects.get(pk=event.command_id)
        self.assertEqual(
            (receipt.command_type, event.recorded_at), ("DEFICIENCY_ASSIGN", receipt.recorded_at)
        )
        projection = self.projection()
        self.assertEqual(
            (projection.assignee_id, projection.status, projection.last_applied_event_id),
            (self.crew.pk, "OPEN", event.pk),
        )
        audit = AuditEvent.objects.get(action="DEFICIENCY_ASSIGN")
        self.assertEqual(audit.outcome, "SUCCEEDED")
        self.assertEqual(audit.new_snapshot["values"]["assignee_user_id"], str(self.crew.pk))
        # nothing but the assignee, the event pointer and the event itself changed
        after = self.snapshot()
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[0]["DeficiencyEvent"], before[0]["DeficiencyEvent"] + 1)

    def test_a_reassignment_keeps_every_earlier_assignee_in_the_history(self):
        self.assign(self.crew)
        self.assign(self.supply)
        self.assertEqual(self.projection().assignee_id, self.supply.pk)
        assigned = list(
            DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id, event_type="ASSIGNED")
            .order_by("sequence")
            .values_list("assignee_id", flat=True)
        )
        self.assertEqual(assigned, [self.crew.pk, self.supply.pk])
        self.refused("ASSIGNEE_UNCHANGED", 422, self.assign, self.supply)

    def test_every_role_against_the_section_3_3_cells(self):
        before = self.snapshot()
        with self.assertRaises(CommandDenied):
            self.assign(actor=self.crew)
        with self.assertRaises(CommandDenied):
            self.assign(actor=self.crew, deficiency_id=self.supply_id)
        with self.assertRaises(CommandDenied):
            self.assign(actor=self.supply)  # a Supply Officer on the mechanical deficiency
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            AuditEvent.objects.filter(action="DEFICIENCY_ASSIGN", outcome="DENIED").count(), 3
        )
        # allowed cells
        self.assign(actor=self.supervisor)
        self.assign(self.supply, actor=self.admin)
        self.assign(self.crew, actor=self.supervisor, deficiency_id=self.supply_id)
        self.assign(self.supply, actor=self.supply, deficiency_id=self.supply_id)
        self.assign(self.crew, actor=self.admin, deficiency_id=self.supply_id)

    def test_every_lifecycle_command_follows_the_same_permission(self):
        for call in (self.deadline, self.start):
            with self.assertRaises(CommandDenied):
                call(actor=self.crew)
            with self.assertRaises(CommandDenied):
                call(actor=self.supply)  # not a supply-related deficiency
        self.deadline(actor=self.supply, deficiency_id=self.supply_id)
        self.start(actor=self.supply, deficiency_id=self.supply_id)
        self.deadline(actor=self.admin)
        self.start(actor=self.supervisor)

    def test_the_supply_fact_comes_from_the_category_revision_not_the_request(self):
        # a client-supplied supply flag is not a parameter, and a later category revision does
        # not change the verdict for a deficiency reported under the earlier one
        self.category(
            "Synthetic reclassified",
            supply_related=True,
            revision=2,
            stable=self.plain.stable_category_id,
            when=self.now - timedelta(hours=1),
        )
        with self.assertRaises(CommandDenied):
            self.assign(actor=self.supply)

    def test_a_denial_reveals_nothing_about_the_deficiency(self):
        unknown = str(uuid4())
        with self.assertRaises(CommandDenied):
            self.assign(actor=self.supply, deficiency_id=unknown, expected=str(uuid4()))
        with self.assertRaises(CommandDenied):
            self.assign(actor=self.crew, deficiency_id=unknown, expected=str(uuid4()))
        error = self.refused(
            "NOT_FOUND",
            404,
            self.assign,
            actor=self.supervisor,
            deficiency_id=unknown,
            expected=str(uuid4()),
        )
        self.assertEqual(error.path, None)

    def test_the_assignee_must_be_an_active_crew_supervisor_or_supply_officer(self):
        admin_only = self.administrator_only()
        other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        stranger = User.objects.create(
            organization=other_org,
            idp_issuer="https://accounts.google.com",
            idp_subject="stranger",
            email="stranger@example.test",
            display_name="Stranger",
        )
        disabled = self.make_user("disabled")
        User.objects.filter(pk=disabled.pk).update(active=False)
        before = self.snapshot()
        for candidate in (str(uuid4()), str(stranger.pk), str(disabled.pk), str(admin_only.pk)):
            error = self.refused(
                "INVALID_ASSIGNEE",
                422,
                assign_deficiency,
                **self.args(assignee_id=candidate),
            )
            self.assertEqual(error.path, "assignee_id")
        self.assertEqual(self.snapshot(), before)
        # an administrator who also holds Crew is a valid assignee; so is each working role
        self.assign(self.admin)
        self.assign(self.supervisor)
        self.assign(self.supply)
        # the same rule applies when a deficiency is reported with an assignee
        self.refused(
            "INVALID_ASSIGNEE",
            422,
            report_deficiency,
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str(self.suction.pk)},
            category_id=self.plain.stable_category_id,
            severity="HIGH",
            summary="Synthetic",
            description="Synthetic description.",
            assignee_id=str(admin_only.pk),
            **self.context(self.supervisor),
        )

    def test_a_closed_or_cancelled_deficiency_cannot_be_assigned_or_given_a_deadline(self):
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
        for call in (self.assign, self.deadline, self.start):
            error = self.refused("DEFICIENCY_NOT_ACTIVE", 422, call, deficiency_id=deficiency_id)
            self.assertEqual(error.path, "deficiency")
        self.assertEqual(self.snapshot(), before)

    def test_a_stale_expected_event_is_refused_for_every_command(self):
        seen = self.newest()
        self.assign()
        for call in (self.assign, self.deadline, self.start):
            kwargs = {"assignee": self.supply} if call == self.assign else {}
            error = self.refused("DEFICIENCY_CHANGED", 409, call, expected=seen, **kwargs)
            self.assertEqual(error.path, "expected_event_id")
        self.refused("DEFICIENCY_CHANGED", 409, self.deadline, expected=str(uuid4()))
        self.deadline()  # naming the newest event works

    def test_a_replay_returns_the_stored_answer_and_a_changed_request_is_reused(self):
        operation_id, expected = str(uuid4()), self.newest()
        first = assign_deficiency(
            assignee_id=str(self.crew.pk), **self.args(expected=expected, operation_id=operation_id)
        )
        counts = self.counts()
        again = assign_deficiency(
            assignee_id=str(self.crew.pk), **self.args(expected=expected, operation_id=operation_id)
        )
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.counts(), counts)
        different = assign_deficiency(
            assignee_id=str(self.supply.pk),
            **self.args(expected=expected, operation_id=operation_id),
        )
        self.assertTrue(different.operation_reused)
        self.assertEqual(self.counts(), counts)

    def test_malformed_requests_are_refused_without_a_write(self):
        before = self.snapshot()
        for bad in (None, 5, "not-a-uuid", str(self.crew.pk).upper()):
            self.refused(
                "MALFORMED_REQUEST",
                400,
                assign_deficiency,
                **{**self.args(), "assignee_id": bad},
            )
        for bad in (None, "tomorrow", 5):
            self.refused("MALFORMED_REQUEST", 400, set_deadline, **{**self.args(), "deadline": bad})
        naive = (timezone.now() + timedelta(days=1)).replace(tzinfo=None)
        self.refused("MALFORMED_REQUEST", 400, set_deadline, **{**self.args(), "deadline": naive})
        self.refused(
            "MALFORMED_REQUEST", 400, start_work, **{**self.args(), "expected_event_id": "x"}
        )
        self.assertEqual(self.snapshot(), before)

    def test_no_command_changes_the_vehicle_status_or_the_evidence(self):
        before = self.snapshot()
        self.assign()
        self.deadline()
        self.start()
        after = self.snapshot()
        self.assertEqual(after[1], before[1])
        for name in (
            "DeficiencyEvidence",
            "DeficiencyLink",
            "Deficiency",
            "DeficiencyAffectedItem",
        ):
            self.assertEqual(after[0][name], before[0][name])

    def test_a_failure_while_writing_rolls_everything_back_and_the_operation_id_is_reusable(self):
        operation_id, expected = str(uuid4()), self.newest()
        before = self.snapshot()
        with (
            mock.patch(
                "cya.live_updates.publish.publish_vehicle_update", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            assign_deficiency(
                assignee_id=str(self.crew.pk),
                **self.args(expected=expected, operation_id=operation_id),
            )
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(
            AuditEvent.objects.filter(action="DEFICIENCY_ASSIGN", outcome="SUCCEEDED").exists()
        )
        assign_deficiency(
            assignee_id=str(self.crew.pk), **self.args(expected=expected, operation_id=operation_id)
        )
        self.assertEqual(self.projection().assignee_id, self.crew.pk)

    def test_each_command_publishes_the_identifier_only_live_hint_after_commit(self):
        with mock.patch("cya.live_updates.publish.publish_vehicle_update") as publish:
            self.assign()
            self.deadline()
            self.start()
        self.assertEqual(publish.call_count, 3)
        for call in publish.call_args_list:
            self.assertEqual(call.kwargs["kind"], "deficiency.changed")
            self.assertEqual(call.kwargs["vehicle_id"], str(self.vehicle.pk))
            self.assertEqual(
                set(call.kwargs), {"organization_id", "vehicle_id", "event_id", "kind"}
            )


class DeadlineAndStartTests(LifecycleCase):
    def test_a_deadline_is_set_and_revised_and_every_revision_is_retained(self):
        first, second = timezone.now() + timedelta(days=2), timezone.now() + timedelta(days=5)
        self.deadline(first)
        self.deadline(second)
        self.assertEqual(self.projection().deadline, second)
        events = DeficiencyEvent.objects.filter(
            deficiency_id=self.deficiency_id, event_type="DEADLINE_SET"
        ).order_by("sequence")
        self.assertEqual([e.deadline for e in events], [first, second])
        self.assertTrue(all(e.assignee_id is None for e in events))
        self.assertEqual(
            CommandReceipt.objects.filter(command_type="DEFICIENCY_DEADLINE").count(), 2
        )
        self.refused("DEADLINE_UNCHANGED", 422, self.deadline, second)

    def test_a_deadline_must_be_in_the_future(self):
        error = self.refused(
            "INVALID_DEADLINE", 422, self.deadline, timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(error.path, "deadline")
        self.assertIsNone(self.projection().deadline)

    def test_starting_work_moves_open_to_in_progress_once(self):
        result = self.start()
        self.assertEqual(
            (result.body["code"], result.body["status"]), ("DEFICIENCY_WORK_STARTED", "IN_PROGRESS")
        )
        event = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest("sequence")
        self.assertEqual(
            (
                event.event_type,
                event.from_status,
                event.to_status,
                event.assignee_id,
                event.deadline,
            ),
            ("WORK_STARTED", "OPEN", "IN_PROGRESS", None, None),
        )
        self.assertEqual(self.projection().status, "IN_PROGRESS")
        error = self.refused("INVALID_TRANSITION", 422, self.start)
        self.assertEqual(error.path, "deficiency")
        # an In Progress deficiency can still be assigned and given a deadline
        self.assign()
        self.deadline()
        self.assertEqual(self.projection().status, "IN_PROGRESS")

    def test_work_cannot_start_from_a_status_that_is_not_open(self):
        # resuming from Awaiting External Dependency is another event; the status is set directly
        for status in ("IN_PROGRESS", "AWAITING_EXTERNAL_DEPENDENCY", "PENDING_VERIFICATION"):
            with self.subTest(status=status):
                DeficiencyProjection.objects.filter(deficiency_id=self.supply_id).update(
                    status=status
                )
                self.refused(
                    "INVALID_TRANSITION",
                    422,
                    self.start,
                    deficiency_id=self.supply_id,
                    expected=self.newest(self.supply_id),
                )


class AssignmentReaderTests(LifecycleCase):
    def test_an_unassigned_deficiency_reads_as_unassigned_and_without_a_deadline(self):
        view = service.assignment_state(str(self.org.pk), self.deficiency_id)
        self.assertEqual(
            (view["assignee_id"], view["deadline"], view["deficiency_status"], view["history"]),
            (None, None, "OPEN", []),
        )
        self.assertEqual(view["last_event_id"], self.newest())

    def test_the_reader_replays_the_events_and_the_projection_is_its_cache(self):
        when = timezone.now() + timedelta(days=3)
        self.assign(self.crew)
        self.deadline(when)
        self.assign(self.supply)
        self.start()
        view = service.assignment_state(str(self.org.pk), self.deficiency_id)
        projection = self.projection()
        self.assertEqual(
            (view["assignee_id"], view["deadline"], view["deficiency_status"]),
            (str(projection.assignee_id), projection.deadline, projection.status),
        )
        self.assertEqual(
            [(h["event_type"], h["assignee_id"]) for h in view["history"]],
            [
                ("ASSIGNED", str(self.crew.pk)),
                ("DEADLINE_SET", None),
                ("ASSIGNED", str(self.supply.pk)),
            ],
        )
        # a stale cache is caught by comparing it with the replay
        DeficiencyProjection.objects.filter(deficiency_id=self.deficiency_id).update(
            assignee=self.crew
        )
        stale = service.assignment_state(str(self.org.pk), self.deficiency_id)
        self.assertNotEqual(stale["assignee_id"], str(self.projection().assignee_id))

    def test_the_creation_event_assignee_and_deadline_are_part_of_the_replay(self):
        when = timezone.now() + timedelta(days=4)
        reported = report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str(self.suction.pk)},
            category_id=self.plain.stable_category_id,
            severity="HIGH",
            summary="Synthetic",
            description="Synthetic description.",
            assignee_id=str(self.crew.pk),
            deadline=when,
            **self.context(self.supervisor),
        )
        view = service.assignment_state(str(self.org.pk), reported.body["deficiency_id"])
        self.assertEqual((view["assignee_id"], view["deadline"]), (str(self.crew.pk), when))
        self.assertEqual([h["event_type"] for h in view["history"]], ["REPORTED"])

    def test_as_of_answers_for_that_instant_whatever_happened_later(self):
        first = self.assign(self.crew)
        t1 = DeficiencyEvent.objects.get(pk=first.body["event_id"]).recorded_at
        self.deadline(timezone.now() + timedelta(days=2))
        second = self.assign(self.supply)
        t2 = DeficiencyEvent.objects.get(pk=second.body["event_id"]).recorded_at
        self.start()
        org = str(self.org.pk)
        at_t1 = service.assignment_state(org, self.deficiency_id, t1)
        self.assertEqual(
            (at_t1["assignee_id"], at_t1["deadline"], at_t1["deficiency_status"]),
            (str(self.crew.pk), None, "OPEN"),
        )
        at_t2 = service.assignment_state(org, self.deficiency_id, t2)
        self.assertEqual(at_t2["assignee_id"], str(self.supply.pk))
        self.assertIsNotNone(at_t2["deadline"])
        self.assertEqual(at_t2["deficiency_status"], "OPEN")
        reported_at = DeficiencyEvent.objects.get(
            deficiency_id=self.deficiency_id, sequence=1
        ).recorded_at
        before_any = service.assignment_state(org, self.deficiency_id, reported_at)
        self.assertEqual((before_any["assignee_id"], before_any["deadline"]), (None, None))
        self.refused(
            "NOT_FOUND",
            404,
            service.assignment_state,
            org,
            self.deficiency_id,
            reported_at - timedelta(seconds=1),
        )
        self.refused("NOT_FOUND", 404, service.assignment_state, org, str(uuid4()))
        other = Organization.objects.create(name="Other Agency", station_zone="UTC")
        self.refused("NOT_FOUND", 404, service.assignment_state, str(other.pk), self.deficiency_id)

    def test_an_oversized_history_is_refused_never_truncated(self):
        self.assign()
        with mock.patch.object(service, "MAX_ASSIGNMENT_EVENTS", 1):
            self.refused(
                "CONFIGURATION_UNAVAILABLE",
                503,
                service.assignment_state,
                str(self.org.pk),
                self.deficiency_id,
            )


class LifecycleRaceTests(LifecycleCase):
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

    def test_concurrent_assignments_from_one_view_commit_exactly_one(self):
        expected = self.newest()
        outcomes = self.run_threads(
            [
                lambda: self.assign(self.crew, expected=expected),
                lambda: self.assign(self.supply, expected=expected, actor=self.admin),
            ]
        )
        done = [o for o in outcomes if hasattr(o, "body")]
        stale = [o for o in outcomes if isinstance(o, DeficiencyError)]
        self.assertEqual((len(done), len(stale)), (1, 1), outcomes)
        self.assertEqual(stale[0].code, "DEFICIENCY_CHANGED")
        self.assertEqual(
            DeficiencyEvent.objects.filter(
                deficiency_id=self.deficiency_id, event_type="ASSIGNED"
            ).count(),
            1,
        )

    def test_a_concurrent_assignment_and_deadline_revision_keep_an_unforked_chain(self):
        expected = self.newest()
        outcomes = self.run_threads(
            [
                lambda: self.assign(self.crew, expected=expected),
                lambda: self.deadline(expected=expected),
                lambda: self.start(expected=expected),
            ]
        )
        self.assertEqual(sum(hasattr(o, "body") for o in outcomes), 1, outcomes)
        sequence = list(
            DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id)
            .order_by("sequence")
            .values_list("sequence", flat=True)
        )
        self.assertEqual(sequence, [1, 2])
        projection = self.projection()
        self.assertEqual(str(projection.last_applied_event_id), self.newest())

    def test_sequential_revisions_each_naming_the_newest_event_all_commit(self):
        for _ in range(3):
            self.deadline(timezone.now() + timedelta(days=1, minutes=_))
        self.assertEqual(
            DeficiencyEvent.objects.filter(
                deficiency_id=self.deficiency_id, event_type="DEADLINE_SET"
            ).count(),
            3,
        )


class LifecycleGuardTests(LifecycleCase):
    """The database refuses what the commands would not write (migration 0006)."""

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
                actor_user=actor or self.supervisor,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def event(self, command_type="DEFICIENCY_ASSIGN", deficiency_id=None, **overrides):
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
            "event_type": "ASSIGNED",
            "from_status": "OPEN",
            "to_status": "OPEN",
            "assignee": self.crew,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.supervisor,
            "actor_roles_at_time": ["SUPERVISOR"],
            **overrides,
        }
        return DeficiencyEvent.objects.create(**fields)

    def refused_event(self, *args, **kwargs):
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.event(*args, **kwargs)

    def test_valid_events_are_accepted_so_the_refusals_below_are_meaningful(self):
        with transaction.atomic():
            self.event()
        with transaction.atomic():
            DeficiencyEvent.objects.filter(pk=self.last.pk)  # a read; the chain moved on
        self.last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest(
            "sequence"
        )
        with transaction.atomic():
            self.event(
                "DEFICIENCY_DEADLINE",
                event_type="DEADLINE_SET",
                assignee=None,
                deadline=timezone.now() + timedelta(days=1),
            )

    def test_the_event_needs_its_own_command_type(self):
        self.refused_event("CHECK_CORRECTION")
        self.refused_event("DEFICIENCY_REGRADE")
        self.refused_event("DEFICIENCY_DEADLINE")  # an assignment written by the deadline command
        self.refused_event(
            "DEFICIENCY_ASSIGN", event_type="WORK_STARTED", assignee=None, to_status="IN_PROGRESS"
        )

    def test_the_actor_must_hold_the_permission_in_the_role_snapshot(self):
        self.refused_event(actor_roles_at_time=["CREW"])
        self.refused_event(actor_roles_at_time=[])
        self.refused_event(actor_roles_at_time=["SUPPLY_OFFICER"])  # the mechanical category
        with transaction.atomic():
            self.event(actor_roles_at_time=["ADMINISTRATOR"])

    def test_a_supply_officer_snapshot_is_accepted_only_for_a_supply_related_category(self):
        with transaction.atomic():
            self.event(deficiency_id=self.supply_id, actor_roles_at_time=["SUPPLY_OFFICER"])

    def test_the_shape_is_closed(self):
        later = timezone.now() + timedelta(days=1)
        self.refused_event(assignee=None)
        self.refused_event(deadline=later)
        self.refused_event(explanation="")
        self.refused_event(correction_ref=str(uuid4()))
        self.refused_event(action_plan="Synthetic plan.")
        self.refused_event(to_status="IN_PROGRESS")
        self.refused_event(from_status="CLOSED_VERIFIED", to_status="CLOSED_VERIFIED")
        self.refused_event(from_status="REPORTED_IN_ERROR", to_status="REPORTED_IN_ERROR")
        self.refused_event(
            "DEFICIENCY_DEADLINE", event_type="DEADLINE_SET", assignee=self.crew, deadline=later
        )
        self.refused_event("DEFICIENCY_DEADLINE", event_type="DEADLINE_SET", assignee=None)
        self.refused_event(
            "DEFICIENCY_START_WORK", event_type="WORK_STARTED", assignee=None, to_status="OPEN"
        )
        self.refused_event(
            "DEFICIENCY_START_WORK",
            event_type="WORK_STARTED",
            assignee=self.crew,
            to_status="IN_PROGRESS",
        )

    def test_a_deadline_must_lie_after_the_instant_it_is_set(self):
        past = timezone.now() - timedelta(days=1)
        self.refused_event(
            "DEFICIENCY_DEADLINE", event_type="DEADLINE_SET", assignee=None, deadline=past
        )

    def test_the_assignee_must_be_an_active_working_user_of_the_organization(self):
        admin_only = self.administrator_only()
        self.refused_event(assignee=admin_only)
        disabled = self.make_user("disabled-forge")
        User.objects.filter(pk=disabled.pk).update(active=False)
        self.refused_event(assignee=disabled)
        other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        stranger = User.objects.create(
            organization=other_org,
            idp_issuer="https://accounts.google.com",
            idp_subject="stranger-forge",
            email="stranger-forge@example.test",
            display_name="Stranger",
        )
        self.refused_event(assignee=stranger)

    def test_work_cannot_start_unless_the_predecessor_left_the_deficiency_open(self):
        with transaction.atomic():
            self.event(
                "DEFICIENCY_START_WORK",
                event_type="WORK_STARTED",
                assignee=None,
                to_status="IN_PROGRESS",
            )
        self.last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest(
            "sequence"
        )
        self.refused_event(
            "DEFICIENCY_START_WORK",
            event_type="WORK_STARTED",
            assignee=None,
            from_status="IN_PROGRESS",
            to_status="IN_PROGRESS",
        )
        self.refused_event(
            "DEFICIENCY_START_WORK",
            event_type="WORK_STARTED",
            assignee=None,
            from_status="OPEN",  # the predecessor left it In Progress
            to_status="IN_PROGRESS",
        )

    def test_a_forked_or_skipped_chain_is_still_refused(self):
        self.refused_event(sequence=self.last.sequence + 2)
        self.refused_event(
            predecessor_event=DeficiencyEvent.objects.filter(sequence=1).first(), sequence=1
        )


@override_settings(**real_collaborator_settings())
class LifecycleApiTests(LifecycleCase):
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

    def test_a_supervisor_assigns_sets_a_deadline_and_starts_work_and_the_reader_shows_it(self):
        when = (timezone.now() + timedelta(days=2)).isoformat()
        assigned = self.post(
            self.sup_client, "assignments", {"assignee_id": str(self.crew_user.pk)}
        )
        payload = assigned.json()
        self.assertEqual((assigned.status_code, payload["status"]), (201, "COMMITTED"))
        self.assertEqual(payload["result"]["assignee_id"], str(self.crew_user.pk))
        self.assertTrue(payload["result"]["server_recorded_at"].endswith("Z"))
        deadline = self.post(self.sup_client, "deadlines", {"deadline": when})
        self.assertEqual(deadline.status_code, 201)
        started = self.post(self.sup_client, "work-starts")
        self.assertEqual(started.json()["result"]["status"], "IN_PROGRESS")
        view = self.crew_client.get(f"{self.base}/assignment").json()  # any member reads
        self.assertEqual(
            (view["deficiency_status"], view["assignee_id"], view["last_event_id"]),
            ("IN_PROGRESS", str(self.crew_user.pk), started.json()["result"]["event_id"]),
        )
        self.assertTrue(view["deadline"].endswith("Z"))
        self.assertEqual([h["event_type"] for h in view["history"]], ["ASSIGNED", "DEADLINE_SET"])
        self.assertEqual(view["history"][0]["deadline"], None)

    def test_the_reader_answers_as_of_an_instant(self):
        first = self.post(
            self.sup_client, "assignments", {"assignee_id": str(self.crew_user.pk)}
        ).json()["result"]
        self.post(self.sup_client, "assignments", {"assignee_id": str(self.sup_user.pk)})
        at = first["server_recorded_at"]
        view = self.crew_client.get(f"{self.base}/assignment", {"as_of": at}).json()
        self.assertEqual(view["assignee_id"], str(self.crew_user.pk))
        now = self.crew_client.get(f"{self.base}/assignment").json()
        self.assertEqual(now["assignee_id"], str(self.sup_user.pk))
        for query in ({"as_of": "yesterday"}, {"as_of": "2026-01-01T00:00:00"}, {"x": "1"}):
            self.assertEqual(
                self.crew_client.get(f"{self.base}/assignment", query).status_code, 400, query
            )
        early = self.crew_client.get(f"{self.base}/assignment", {"as_of": "2000-01-01T00:00:00Z"})
        self.assertEqual((early.status_code, early.json()["code"]), (404, "NOT_FOUND"))

    def test_a_replay_is_acknowledged_again_without_a_second_event(self):
        body = {
            "operation_id": str(uuid4()),
            "assignee_id": str(self.crew_user.pk),
            "expected_event_id": self.newest(),
        }
        first = self.post(self.sup_client, "assignments", body).json()
        events = DeficiencyEvent.objects.count()
        self.assertEqual(self.post(self.sup_client, "assignments", body).json(), first)
        self.assertEqual(DeficiencyEvent.objects.count(), events)
        body["assignee_id"] = str(self.sup_user.pk)
        self.rejected(
            self.post(self.sup_client, "assignments", body),
            409,
            "OPERATION_REUSED",
            [("operation_id", "REUSED")],
        )

    def test_a_crew_member_is_forbidden_and_signed_out_visitors_are_not_authenticated(self):
        from django.test import Client

        events = DeficiencyEvent.objects.count()
        for path, body in (
            ("assignments", {"assignee_id": str(self.crew_user.pk)}),
            ("deadlines", {"deadline": (timezone.now() + timedelta(days=1)).isoformat()}),
            ("work-starts", {}),
        ):
            self.rejected(self.post(self.crew_client, path, body), 403, "FORBIDDEN")
            fresh = Client()
            self.assertEqual(
                fresh.post(
                    f"{self.base}/{path}", "{}", content_type="application/json"
                ).status_code,
                401,
            )
        self.assertEqual(Client().get(f"{self.base}/assignment").status_code, 401)
        self.assertEqual(DeficiencyEvent.objects.count(), events)

    def test_refusals_carry_the_stable_codes(self):
        crew = str(self.crew_user.pk)
        stale = str(uuid4())
        self.rejected(
            self.post(
                self.sup_client, "assignments", {"assignee_id": crew, "expected_event_id": stale}
            ),
            409,
            "DEFICIENCY_CHANGED",
            [("expected_event_id", "DEFICIENCY_CHANGED")],
        )
        self.rejected(
            self.post(self.sup_client, "assignments", {"assignee_id": str(uuid4())}),
            422,
            "INVALID_ASSIGNEE",
            [("assignee_id", "INVALID_ASSIGNEE")],
        )
        self.rejected(
            self.post(
                self.sup_client,
                "deadlines",
                {"deadline": (timezone.now() - timedelta(hours=1)).isoformat()},
            ),
            422,
            "INVALID_DEADLINE",
            [("deadline", "INVALID_DEADLINE")],
        )
        self.post(self.sup_client, "work-starts")
        self.rejected(
            self.post(self.sup_client, "work-starts"),
            422,
            "INVALID_TRANSITION",
            [("deficiency", "INVALID_TRANSITION")],
        )
        missing = f"/api/v1/deficiencies/{uuid4()}"
        self.base = missing
        self.rejected(self.post(self.sup_client, "work-starts"), 404, "NOT_FOUND")

    def test_malformed_bodies_are_rejected_before_any_write(self):
        events = DeficiencyEvent.objects.count()
        self.rejected(self.post(self.sup_client, "assignments", "[]"), 400, "MALFORMED_REQUEST")
        self.rejected(self.post(self.sup_client, "assignments", "{"), 400, "MALFORMED_REQUEST")
        self.rejected(
            self.post(self.sup_client, "work-starts", {"extra": 1}),
            400,
            "MALFORMED_REQUEST",
            [("$", "UNKNOWN_FIELD")],
        )
        self.rejected(
            self.post(self.sup_client, "deadlines", {"deadline": "tomorrow"}),
            400,
            "MALFORMED_REQUEST",
            [("deadline", "INVALID")],
        )
        self.rejected(
            self.post(self.sup_client, "deadlines", {"deadline": "2030-01-01T00:00:00"}),
            400,
            "MALFORMED_REQUEST",
            [("deadline", "MALFORMED_REQUEST")],
        )
        self.assertEqual(DeficiencyEvent.objects.count(), events)
