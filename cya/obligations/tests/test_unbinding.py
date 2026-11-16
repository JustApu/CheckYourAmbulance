"""Unbinding preview and confirmation, and bindings for inactive vehicles (SPEC CFG-7, CFG-8, AC-37).

AttentionCase binds the template to vehicle A at 2026-06-01 00:00 UTC and generates the 06:00
inspections of 06-01 and 06-02, each with a one-hour early window (05:00).
"""

from datetime import timedelta
from uuid import uuid4

from cya.attention.tests.support import AttentionCase
from cya.checks.models import CheckRecord
from cya.conditions.models import ObligationDetermination
from cya.obligations import commands
from cya.obligations.commands import BindingError, PreviewChanged
from cya.obligations.models import RequirementBinding
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.obligations.unbinding import unbind_preview
from cya.operations.vehicles import record_activity_baseline, set_vehicle_active


class UnbindingCase(AttentionCase):
    def preview(self, at=None):
        return unbind_preview(
            str(self.org.pk),
            str(self.vehicle.pk),
            str(self.template.pk),
            "test-0600",
            at or self.clock.now,
        )

    def unbind(self, token, **kw):
        return self.bind(action="UNBIND", preview_token=token, **kw)

    def dues(self, occurrences):
        return [o.due_at for o in occurrences]

    def revisions(self):
        return list(
            RequirementBinding.objects.filter(vehicle=self.vehicle)
            .order_by("revision")
            .values_list("action", flat=True)
        )

    def states_by_due(self, at=None):
        by_due = {}
        for state in get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            at or self.clock.now,
            organization_id=str(self.org.pk),
            end=self.at(days=3),
        ):
            by_due.setdefault(state.obligation.due_at, set()).add(state.state)
        return by_due


class PreviewTests(UnbindingCase):
    def test_the_preview_splits_obligations_at_the_early_window(self):
        self.clock.set(self.at(hours=5, minutes=30))  # the 06-01 window opened at 05:00
        preview = self.preview()
        self.assertTrue(preview.bound)
        self.assertEqual(self.dues(preview.continuing), [self.at(hours=6)])
        self.assertEqual(self.dues(preview.superseded), [self.at(days=1, hours=6)])
        self.assertEqual(preview.superseded[0].early_at, self.at(days=1, hours=5))
        per_occurrence = preview.continuing[0].count
        self.assertGreater(per_occurrence, 0)
        self.assertEqual(preview.superseded_count, per_occurrence)
        self.assertEqual(preview.continuing_count, per_occurrence)

    def test_before_any_window_opens_everything_is_superseded(self):
        self.clock.set(self.at(hours=4))
        preview = self.preview()
        self.assertEqual(preview.continuing, ())
        self.assertEqual(
            self.dues(preview.superseded), [self.at(hours=6), self.at(days=1, hours=6)]
        )

    def test_an_unbound_binding_has_an_empty_preview(self):
        self.clock.set(self.at(hours=4))
        self.unbind(self.preview().token)
        preview = self.preview()
        self.assertFalse(preview.bound)
        self.assertEqual((preview.superseded, preview.continuing), ((), ()))

    def test_the_token_changes_when_time_crosses_a_window_or_a_check_completes(self):
        before = self.preview(self.at(hours=4, minutes=59)).token
        self.clock.set(self.at(hours=5, minutes=30))
        opened = self.preview().token
        self.assertNotEqual(before, opened)
        self.record(item=self.suction)
        self.assertNotEqual(self.preview().token, opened)


class ConfirmationTests(UnbindingCase):
    def test_unbinding_supersedes_only_prewindow_obligations_and_keeps_history(self):
        self.clock.set(self.at(hours=5, minutes=30))
        check = self.record(item=self.suction).body["check_id"]
        self.unbind(self.preview().token)
        self.assertEqual(self.revisions(), ["BIND", "UNBIND"])
        superseded = ObligationDetermination.objects.filter(
            vehicle_id=str(self.vehicle.pk), determination_type="SUPERSEDED"
        )
        self.assertEqual({d.due_at for d in superseded}, {self.at(days=1, hours=6)})
        self.assertTrue(CheckRecord.objects.filter(pk=check).exists(), "checks are retained")
        # Two days on, the superseded inspection is neither owed nor Overdue; the in-window one
        # keeps its ordinary state.
        self.clock.set(self.at(days=2, hours=12))
        states = self.states_by_due()
        self.assertNotIn(self.at(days=1, hours=6), states)
        self.assertIn(State.OVERDUE, states[self.at(hours=6)])
        self.assertIn(State.SATISFIED, states[self.at(hours=6)])
        self.generate()
        self.assertEqual(sorted(self.states_by_due()), [self.at(hours=6)])

    def test_a_changed_preview_is_refused_and_nothing_changes(self):
        token = self.preview(self.at(hours=4)).token
        self.clock.set(self.at(hours=5, minutes=30))  # the 06-01 window opened meanwhile
        with self.assertRaises(PreviewChanged):
            self.unbind(token)
        token = self.preview().token
        self.record(item=self.suction)  # a completion after the preview
        with self.assertRaises(PreviewChanged):
            self.unbind(token)
        self.assertEqual(self.revisions(), ["BIND"])
        self.unbind(self.preview().token)
        self.assertEqual(self.revisions(), ["BIND", "UNBIND"])

    def test_replaying_the_operation_id_does_not_unbind_twice(self):
        self.clock.set(self.at(hours=4))
        token = self.preview().token
        ids = {
            "operation_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "request_attempt_id": str(uuid4()),
        }
        args = {
            "organization_id": str(self.org.pk),
            "actor_id": str(self.supervisor.pk),
            "vehicle_id": str(self.vehicle.pk),
            "template_id": str(self.template.pk),
            "slot": "test-0600",
            "action": "UNBIND",
            "preview_token": token,
        }
        first = commands.bind_requirement(**args, **ids)
        self.clock.set(self.at(hours=5, minutes=30))  # a fresh preview would differ now
        again = commands.bind_requirement(**args, **{**ids, "request_attempt_id": str(uuid4())})
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(self.revisions(), ["BIND", "UNBIND"])

    def test_a_token_applies_only_to_an_unbinding_that_takes_effect_now(self):
        token = self.preview().token
        with self.assertRaisesRegex(BindingError, "only to an unbinding"):
            self.bind(action="BIND", preview_token=token, reconcile=False)
        with self.assertRaisesRegex(BindingError, "only to an unbinding"):
            self.unbind(token, effective_at=self.at(days=1), reconcile=False)
        with self.assertRaisesRegex(BindingError, "malformed"):
            self.unbind("not-a-token", reconcile=False)
        self.assertEqual(self.revisions(), ["BIND"])


class InactiveVehicleTests(UnbindingCase):
    """CFG-7, AC-37(2) and (3): bindings recorded ahead owe only from activation or start."""

    def test_a_binding_recorded_while_inactive_owes_only_from_activation(self):
        record_activity_baseline(str(self.org.pk))
        other = self.vehicles[1]
        set_vehicle_active(vehicle_id=str(other.pk), active=False, **self.context(self.supervisor))
        self.bind(vehicle=other, actor=self.supervisor)
        self.generate()
        self.assertEqual(self.obligations(vehicle=other), [])
        self.clock.set(self.at(days=1, hours=12))  # past the 06-02 06:00 inspection
        self.generate()
        activated = set_vehicle_active(
            vehicle_id=str(other.pk), active=True, **self.context(self.supervisor)
        )
        self.generate()
        owed = [
            s
            for s in get_vehicle_obligations_as_of(
                str(other.pk), self.clock.now, organization_id=str(self.org.pk)
            )
            if s.state != State.EXEMPT
        ]
        self.assertTrue(owed, "obligations begin after activation")
        self.assertTrue(
            all(s.obligation.due_at >= self.clock.now for s in owed),
            f"nothing owed before activation event {activated.body['event_id']}",
        )

    def test_a_future_binding_owes_only_from_its_start(self):
        other = self.vehicles[1]
        self.bind(vehicle=other, actor=self.supervisor, effective_at=self.at(hours=12))
        self.generate()
        self.assertEqual(
            {o.due_at for o in self.obligations(vehicle=other)}, {self.at(days=1, hours=6)}
        )

    def test_an_inactive_vehicle_may_be_unbound(self):
        record_activity_baseline(str(self.org.pk))
        self.clock.set(self.at(hours=4))
        set_vehicle_active(
            vehicle_id=str(self.vehicle.pk), active=False, **self.context(self.supervisor)
        )
        self.unbind(self.preview().token)
        self.assertEqual(self.revisions(), ["BIND", "UNBIND"])
        self.assertEqual(
            ObligationDetermination.objects.filter(
                vehicle_id=str(self.vehicle.pk), determination_type="SUPERSEDED"
            ).count(),
            0,
            "reconciliation does not run for an inactive vehicle",
        )


class AuthorizationTests(UnbindingCase):
    def test_a_supervisor_may_unbind_through_the_policy_action(self):
        self.clock.set(self.at(hours=4))
        self.unbind(self.preview().token, actor=self.supervisor)
        binding = RequirementBinding.objects.get(action="UNBIND")
        self.assertEqual(binding.actor_user_id, str(self.supervisor.pk))
        self.assertIn("SUPERVISOR", binding.actor_roles_at_time)

    def test_the_continuing_list_is_unchanged_by_superseding(self):
        self.clock.set(self.at(hours=5, minutes=30))
        continuing = self.preview().continuing
        self.unbind(self.preview().token)
        self.assertEqual(self.dues(continuing), [self.at(hours=6)])
        self.assertIn(self.at(hours=6), self.states_by_due(self.clock.now + timedelta(minutes=1)))
