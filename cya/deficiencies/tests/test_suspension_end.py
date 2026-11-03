"""Ending a check suspension early while the deficiency is open (SPEC SUS-5, issue 32
(https://github.com/JustApu/CheckYourAmbulance/issues/32))."""

from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor

from cya.attention.tests.support import AttentionCase
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.deficiencies.models import CheckSuspension, DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.suspension import end_suspension, suspension_state
from cya.deficiencies.tests import test_suspension
from cya.deficiencies.tests.test_suspension import SuspensionMixin
from cya.deficiencies.tests.test_verification import VerificationBase
from cya.notifications.models import NotificationOutbox
from cya.obligations.state import State
from cya.operations.models import VehicleStatusEvent

WHY = "Synthetic: the replacement arrived; check it again."


class EndMixin(SuspensionMixin):
    def end(self, deficiency_id, suspension_id, actor=None, **fields):
        context = {**self.context(actor or self.supervisor), **fields.pop("context", {})}
        return end_suspension(
            deficiency_id=deficiency_id,
            expected_event_id=fields.pop(
                "expected_event_id", str(self.last_event(deficiency_id).pk)
            ),
            suspension_id=suspension_id,
            **fields,
            **context,
        )

    def suspended(self, target=None, items=None):
        """A deficiency with suction (and the named items) suspended; returns (id, start ids)."""
        deficiency_id = self.report(target or {"item_id": str(self.suction.pk)})
        result = self.suspend(deficiency_id, items)
        return deficiency_id, result.body["suspension_ids"]


class EarlyEndTests(EndMixin, VerificationBase):
    def test_an_early_end_is_one_event_and_one_end_row_and_changes_nothing_else(self):
        deficiency_id, ids = self.suspended(
            self.cabinet_target(),
            [{"item_id": str(self.suction.pk)}, {"item_id": str(self.masks.pk)}],
        )
        suction = next(s for s in self.starts(deficiency_id) if s.item_id == self.suction.pk)
        masks = next(s for s in self.starts(deficiency_id) if s.item_id == self.masks.pk)
        before = DeficiencyProjection.objects.get(deficiency_id=deficiency_id)
        statuses = VehicleStatusEvent.objects.count()
        outbox = NotificationOutbox.objects.count()
        result = self.end(deficiency_id, str(suction.pk), explanation=WHY)
        event = self.last_event(deficiency_id)
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status, event.explanation),
            ("SUSPENSION_ENDED", "OPEN", "OPEN", WHY),
        )
        self.assertEqual(
            (event.actor_user_id, event.suspensions_ended), (self.supervisor.pk, [str(suction.pk)])
        )
        (end,) = self.ends(deficiency_id)
        self.assertEqual(
            (end.prior_suspension_id, end.deficiency_event_id, end.item_id, end.command_id),
            (suction.pk, event.pk, self.suction.pk, event.command_id),
        )
        # The other suspension stays in force; status, assignee and deadline are unchanged, the
        # vehicle's status is untouched and nobody is notified (SUS-5).
        self.assertEqual(self.in_force(deficiency_id), [str(masks.pk)])
        after = DeficiencyProjection.objects.get(deficiency_id=deficiency_id)
        self.assertEqual(
            (after.status, after.assignee_id, after.deadline, after.last_applied_event_id),
            (before.status, before.assignee_id, before.deadline, event.pk),
        )
        self.assertEqual(VehicleStatusEvent.objects.count(), statuses)
        self.assertEqual(NotificationOutbox.objects.count(), outbox)
        self.assertEqual(
            result.body,
            {
                "code": "DEFICIENCY_SUSPENSION_ENDED",
                "deficiency_id": deficiency_id,
                "event_id": str(event.pk),
                "status": "OPEN",
                "suspensions_ended": [str(suction.pk)],
                "items": [{"item_id": str(self.suction.pk), "kit_instance_id": None}],
            },
        )
        audit = AuditEvent.objects.get(
            action="DEFICIENCY_CHECK_SUSPENSION_END", outcome="SUCCEEDED"
        )
        self.assertNotIn("replacement arrived", str(audit.new_snapshot))  # identifiers only
        self.assertEqual(audit.new_snapshot["values"]["suspensions_ended"], [str(suction.pk)])
        self.assertEqual(sorted(ids), sorted([str(suction.pk), str(masks.pk)]))

    def test_every_active_status_may_end_one_and_the_explanation_is_optional(self):
        pending = self.pending({"item_id": str(self.suction.pk)})
        (pending_id,) = self.suspend(pending).body["suspension_ids"]
        self.end(pending, pending_id)
        self.assertEqual(
            (self.last_event(pending).to_status, self.last_event(pending).explanation),
            ("PENDING_VERIFICATION", None),
        )
        working = self.report({"item_id": str(self.masks.pk)})
        self.work(working)
        (working_id,) = self.suspend(working, [{"item_id": str(self.masks.pk)}]).body[
            "suspension_ids"
        ]
        self.end(working, working_id, actor=self.admin)  # Administrators hold it too
        self.assertEqual(self.last_event(working).to_status, "IN_PROGRESS")
        self.assertEqual((self.in_force(pending), self.in_force(working)), ([], []))

    def test_refusals_write_nothing(self):
        deficiency_id, (own,) = self.suspended()
        other_id, (foreign,) = self.suspended()
        cases = (
            ("SUSPENSION_NOT_FOUND", foreign, {}),  # another deficiency's suspension
            ("SUSPENSION_NOT_FOUND", str(uuid4()), {}),
            ("DEFICIENCY_CHANGED", own, {"expected_event_id": str(uuid4())}),
        )
        for code, suspension_id, fields in cases:
            with self.subTest(code=code, suspension_id=suspension_id):
                self.refused_without_writes(
                    code,
                    lambda s=suspension_id, f=fields: self.end(deficiency_id, s, **dict(f)),
                )
        for suspension_id, fields in (("not-an-id", {}), (own, {"explanation": " "})):
            with self.subTest(fields=fields), self.assertRaises(DeficiencyError) as caught:
                self.end(deficiency_id, suspension_id, **fields)
            self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        self.end(deficiency_id, own)
        error = self.refused_without_writes(
            "SUSPENSION_ALREADY_ENDED", lambda: self.end(deficiency_id, own)
        )
        self.assertEqual(error.path, "suspension_id")
        self.close(other_id)  # the closure ended the other one; its deficiency is not active
        self.refused_without_writes("DEFICIENCY_NOT_ACTIVE", lambda: self.end(other_id, foreign))

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        deficiency_id, (own,) = self.suspended()
        for actor in (self.crew, self.supply):
            before = self.suspension_counts()
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.end(deficiency_id, own, actor=actor)
            self.assertEqual(self.suspension_counts(), before)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="DEFICIENCY_CHECK_SUSPENSION_END", outcome="DENIED"
            ).count(),
            2,
        )
        self.assertEqual(self.in_force(deficiency_id), [own])

    def test_replay_returns_the_stored_result_and_writes_nothing(self):
        deficiency_id, (own,) = self.suspended()
        operation_id = str(uuid4())
        first = self.end(deficiency_id, own, context={"operation_id": operation_id})
        counts = self.suspension_counts()
        again = self.end(
            deficiency_id,
            own,
            context={"operation_id": operation_id},
            expected_event_id=str(self.last_event(deficiency_id).predecessor_event_id),
        )
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.suspension_counts(), counts)

    def test_a_closure_after_an_early_end_lists_only_the_rest_and_the_check_can_be_resuspended(
        self,
    ):
        deficiency_id, _ids = self.suspended(
            self.cabinet_target(),
            [{"item_id": str(self.suction.pk)}, {"item_id": str(self.masks.pk)}],
        )
        suction = next(s for s in self.starts(deficiency_id) if s.item_id == self.suction.pk)
        masks = next(s for s in self.starts(deficiency_id) if s.item_id == self.masks.pk)
        self.end(deficiency_id, str(suction.pk))
        # Ending does not resume anything later: suspending the check again is a new START.
        (again,) = self.suspend(deficiency_id).body["suspension_ids"]
        self.assertNotEqual(again, str(suction.pk))
        self.close(deficiency_id)
        closing = self.last_event(deficiency_id)
        self.assertEqual(closing.suspensions_ended, sorted([str(masks.pk), again]))
        self.assertEqual(self.in_force(deficiency_id), [])
        ends = {
            str(e.prior_suspension_id): e.deficiency_event.event_type
            for e in self.ends(deficiency_id)
        }
        self.assertEqual(
            ends,
            {
                str(suction.pk): "SUSPENSION_ENDED",
                str(masks.pk): "ADMINISTRATOR_CLOSED",
                again: "ADMINISTRATOR_CLOSED",
            },
        )

    def test_the_history_reader_answers_before_and_after_the_end(self):
        deficiency_id, (own,) = self.suspended()
        before_end = self.last_event(deficiency_id).recorded_at
        self.end(deficiency_id, own, explanation=WHY)
        event = self.last_event(deficiency_id)
        now = suspension_state(str(self.org.pk), deficiency_id)
        (row,) = now["suspensions"]
        self.assertEqual(
            (row["ended_at"], row["end_event_id"], row["end_event_type"]),
            (event.recorded_at, str(event.pk), "SUSPENSION_ENDED"),
        )
        self.assertEqual((now["in_force"], now["last_event_id"]), ([], str(event.pk)))
        earlier = suspension_state(str(self.org.pk), deficiency_id, before_end)
        self.assertEqual(earlier["in_force"], [own])
        self.assertIsNone(earlier["suspensions"][0]["ended_at"])


class EarlyEndRaceTests(EndMixin, VerificationBase):
    hold = test_suspension.RaceTests.hold
    run_ordered = test_suspension.RaceTests.run_ordered

    def test_an_end_committed_first_leaves_the_waiting_closure_the_rest(self):
        deficiency_id, (own,) = self.suspended()
        with self.hold("cya.deficiencies.service.end_suspensions"):
            ended, closed = self.run_ordered(
                lambda: self.end(deficiency_id, own),
                lambda: self.close(deficiency_id),
            )
        self.assertFalse(isinstance(ended, Exception), ended)
        # The closure named the event it saw before the end, so it is refused and retried.
        self.assertIsInstance(closed, DeficiencyError)
        self.assertEqual((closed.code, closed.status), ("DEFICIENCY_CHANGED", 409))
        self.close(deficiency_id)
        self.assertEqual(self.last_event(deficiency_id).suspensions_ended, [])
        self.assertEqual(len(self.ends(deficiency_id)), 1)

    def test_a_closure_committed_first_refuses_the_stale_end(self):
        deficiency_id, (own,) = self.suspended()
        seen = str(self.last_event(deficiency_id).pk)
        with self.hold("cya.deficiencies.service.end_suspensions"):
            closed, ended = self.run_ordered(
                lambda: self.close(deficiency_id),
                lambda: self.end(deficiency_id, own, expected_event_id=seen),
            )
        self.assertFalse(isinstance(closed, Exception), closed)
        self.assertIsInstance(ended, DeficiencyError)
        self.assertEqual((ended.code, ended.status), ("DEFICIENCY_CHANGED", 409))
        (end,) = self.ends(deficiency_id)
        self.assertEqual(end.deficiency_event.event_type, "ADMINISTRATOR_CLOSED")


class EarlyEndDatabaseGuardTests(EndMixin, VerificationBase):
    def end_listing(self, deficiency_id, suspension_id, **changes):
        """An early end whose event is written with `changes` instead of what the command chose."""
        original = DeficiencyEvent.objects.create

        def create(**fields):
            return original(**{**fields, **changes})

        with (
            mock.patch(
                "cya.deficiencies.suspension.DeficiencyEvent.objects.create", side_effect=create
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.end(deficiency_id, suspension_id)
        return str(caught.exception)

    def test_the_event_lists_exactly_one_suspension_in_force(self):
        deficiency_id, ids = self.suspended(
            self.cabinet_target(),
            [{"item_id": str(self.suction.pk)}, {"item_id": str(self.masks.pk)}],
        )
        other_id, (foreign,) = self.suspended()
        own = ids[0]
        for listed in ([], ids, [foreign], [str(uuid4())], [1]):
            with self.subTest(listed=listed):
                self.end_listing(deficiency_id, own, suspensions_ended=listed)
        self.assertEqual(sorted(self.in_force(deficiency_id)), sorted(ids))
        self.assertEqual(self.in_force(other_id), [foreign])

    def test_the_event_needs_a_supervisor_or_administrator_and_keeps_the_status(self):
        deficiency_id, (own,) = self.suspended()
        for changes in ({"actor_roles_at_time": ["CREW"]}, {"to_status": "IN_PROGRESS"}):
            with self.subTest(changes=changes):
                self.end_listing(deficiency_id, own, **changes)
        self.assertEqual(self.in_force(deficiency_id), [own])

    def test_only_its_own_command_writes_the_event(self):
        deficiency_id, (own,) = self.suspended()
        start_event = self.last_event(deficiency_id)
        with self.assertRaises(DatabaseError) as caught:
            DeficiencyEvent.objects.create(
                organization_id=self.org.pk,
                deficiency_id=deficiency_id,
                sequence=start_event.sequence + 1,
                predecessor_event=start_event,
                event_type="SUSPENSION_ENDED",
                from_status=start_event.to_status,
                to_status=start_event.to_status,
                suspensions_ended=[own],
                command_id=start_event.command_id,  # the suspension's own receipt
                actor_user_id=start_event.actor_user_id,
                actor_roles_at_time=start_event.actor_roles_at_time,
                recorded_at=start_event.recorded_at,
            )
        self.assertIn("early suspension end needs its own command", str(caught.exception))
        self.assertEqual(self.in_force(deficiency_id), [own])

    def test_an_end_row_names_the_start_its_event_lists(self):
        deficiency_id, ids = self.suspended(
            self.cabinet_target(),
            [{"item_id": str(self.suction.pk)}, {"item_id": str(self.masks.pk)}],
        )
        starts = {str(s.pk): s for s in self.starts(deficiency_id)}
        listed, other = ids[0], ids[1]
        original = CheckSuspension.objects.bulk_create

        def elsewhere(rows, *rest, **kwargs):
            rows = list(rows)
            rows[0].prior_suspension = starts[other]
            rows[0].item_id = starts[other].item_id
            rows[0].definition_id = starts[other].definition_id
            return original(rows, *rest, **kwargs)

        with (
            mock.patch(
                "cya.deficiencies.service.CheckSuspension.objects.bulk_create",
                side_effect=elsewhere,
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.end(deficiency_id, listed)
        self.assertIn("lists its start", str(caught.exception))
        self.assertEqual(sorted(self.in_force(deficiency_id)), sorted(ids))

    def test_the_end_row_is_append_only(self):
        deficiency_id, (own,) = self.suspended()
        self.end(deficiency_id, own)
        (end,) = self.ends(deficiency_id)
        with self.assertRaises(DatabaseError):
            CheckSuspension.objects.filter(pk=end.pk).update(recorded_at=end.recorded_at)
        with self.assertRaises(DatabaseError):
            CheckSuspension.objects.filter(pk=end.pk).delete()


class EarlyEndMigrationTests(EndMixin, VerificationBase):
    def trigger(self, name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s",
                [name],
            )
            row = cursor.fetchone()
        return None if row is None else row[0]

    def target(self, leaves):
        return [
            ("cya_deficiencies", "0018_acknowledgement_maximum_cap")
            if leaf[0] == "cya_deficiencies"
            else leaf
            for leaf in leaves
        ]

    def test_reversal_restores_the_previous_triggers_and_forward_reinstalls_them(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        before = (self.trigger("cya_deficiency_event_bi"), self.trigger("cya_check_suspension_bi"))
        self.assertIn("SUSPENSION_ENDED", before[0])
        self.assertIn("SUSPENSION_ENDED", before[1])
        try:
            executor.migrate(self.target(leaves))
            for name in ("cya_deficiency_event_bi", "cya_check_suspension_bi"):
                self.assertNotIn("SUSPENSION_ENDED", self.trigger(name))
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(
                before,
                (self.trigger("cya_deficiency_event_bi"), self.trigger("cya_check_suspension_bi")),
            )
        finally:
            MigrationExecutor(connection).migrate(leaves)

    def test_reversal_is_refused_once_an_early_end_exists(self):
        deficiency_id, (own,) = self.suspended()
        self.end(deficiency_id, own)
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        try:
            with self.assertRaisesRegex(RuntimeError, "early suspension ends"):
                executor.migrate(self.target(leaves))
        finally:
            MigrationExecutor(connection).migrate(leaves)
        self.assertIn("SUSPENSION_ENDED", self.trigger("cya_deficiency_event_bi"))


class EarlyEndObligationTests(EndMixin, AttentionCase, VerificationBase):
    """SUS-5 over the controllable clock: a 06:00 daily inspection (E 05:00, G 07:00)."""

    states = test_suspension.ObligationTests.states

    def test_an_end_during_the_window_makes_the_check_required_again(self):
        self.clock.set(self.at(hours=5, minutes=15))
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        (own,) = self.suspend(deficiency_id).body["suspension_ids"]
        self.clock.set(self.at(hours=5, minutes=45))
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.SUSPENDED)
        self.assertNotIn((str(self.suction.pk), None), self.item_ids(needs_attention_only=True))
        self.end(deficiency_id, own)
        self.clock.set(self.at(hours=5, minutes=50))
        # Required again with its normal due time: the same state as an unsuspended item.
        self.assertEqual(
            self.states(self.suction)[self.at(hours=6)].state,
            self.states(self.masks)[self.at(hours=6)].state,
        )
        self.assertNotEqual(self.states(self.suction)[self.at(hours=6)].state, State.SUSPENDED)
        row = self.row(self.suction)
        self.assertFalse(row.check_suspended)
        self.assertIn((str(self.suction.pk), None), self.item_ids(needs_attention_only=True))
        self.clock.set(self.at(hours=8))
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.OVERDUE)
        self.assertEqual(self.status(deficiency_id), "OPEN")  # the deficiency stays open

    def test_an_obligation_whose_window_closed_while_suspended_stays_suspended(self):
        self.clock.set(self.at(hours=5, minutes=15))
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        (own,) = self.suspend(deficiency_id).body["suspension_ids"]
        self.clock.set(self.at(hours=9))
        self.end(deficiency_id, own)
        # No backfill: day 0 stays Suspended; day 1 follows the normal schedule.
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        states = self.states(self.suction)
        self.assertEqual(states[self.at(hours=6)].state, State.SUSPENDED)
        self.assertIsNotNone(states[self.at(hours=6)].suspension_ended_at)
        self.assertNotEqual(states[self.at(days=1, hours=6)].state, State.SUSPENDED)
        self.record(self.suction)
        self.assertEqual(self.states(self.suction)[self.at(days=1, hours=6)].state, State.SATISFIED)
