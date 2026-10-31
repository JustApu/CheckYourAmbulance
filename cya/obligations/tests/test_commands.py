"""The binding command and its operator CLI: authorization, validation, history and replay."""

from datetime import timedelta
from io import StringIO
from unittest import mock
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError

from cya.accounts.fields import new_id
from cya.accounts.models import Organization
from cya.checklists.models import ChecklistTemplate, Vehicle
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.obligations.commands import BindingError, bind_requirement
from cya.obligations.models import Obligation, RequirementBinding
from cya.obligations.tests.support import ObligationCase


class BindingCase(ObligationCase):
    def setUp(self):
        super().setUp()
        self.published_version()
        self.publish_schedule("test-0600")

    def bindings(self):
        return list(RequirementBinding.objects.order_by("revision"))


class AuthorizationTests(BindingCase):
    def test_only_an_administrator_may_bind(self):
        for actor in (self.supervisor, self.crew):
            with self.subTest(actor=actor.idp_subject), self.assertRaises(CommandDenied):
                self.bind(actor=actor)
        self.assertEqual(self.bindings(), [])
        self.assertEqual(
            AuditEvent.objects.filter(action="REQUIREMENT_BIND", outcome="DENIED").count(), 2
        )

    def test_an_inactive_administrator_is_denied(self):
        self.admin.active = False
        self.admin.save(update_fields=["active"])
        with self.assertRaises(CommandDenied):
            self.bind()
        self.assertEqual(self.bindings(), [])


class HistoryTests(BindingCase):
    def test_bind_and_unbind_append_revisions_and_never_edit(self):
        self.clock.set(self.at(hours=1))
        self.bind(reconcile=False)
        self.clock.set(self.at(hours=2))
        self.bind(action="UNBIND", reconcile=False)
        self.clock.set(self.at(hours=3))
        self.bind(reconcile=False)
        rows = self.bindings()
        self.assertEqual(
            [(r.revision, r.action) for r in rows], [(1, "BIND"), (2, "UNBIND"), (3, "BIND")]
        )
        self.assertEqual(rows[0].effective_at, rows[0].recorded_at)
        self.assertEqual(rows[0].actor_user_id, str(self.admin.pk))
        self.assertIn("ADMINISTRATOR", rows[0].actor_roles_at_time)
        self.assertIsNone(rows[0].service_identity)

    def test_a_future_effective_binding_is_recorded_with_its_effective_instant(self):
        self.bind(effective_at=self.at(days=2), reconcile=False)
        self.assertEqual(self.bindings()[0].effective_at, self.at(days=2))

    def test_binding_twice_or_unbinding_when_unbound_is_refused(self):
        with self.assertRaisesRegex(BindingError, "already unbound"):
            self.bind(action="UNBIND", reconcile=False)
        self.bind(reconcile=False)
        with self.assertRaisesRegex(BindingError, "already bound"):
            self.bind(reconcile=False)
        self.assertEqual(len(self.bindings()), 1)

    def test_a_binding_cannot_take_effect_in_the_past(self):
        with self.assertRaisesRegex(BindingError, "before it is recorded"):
            self.bind(effective_at=self.at(hours=-1), reconcile=False)
        self.assertEqual(self.bindings(), [])

    def test_a_revision_must_take_effect_after_the_previous_one(self):
        self.bind(effective_at=self.at(days=2), reconcile=False)
        with self.assertRaisesRegex(BindingError, "after the previous revision"):
            self.bind(action="UNBIND", effective_at=self.at(days=1), reconcile=False)

    def test_a_naive_effective_instant_is_refused_before_anything_runs(self):
        with self.assertRaisesRegex(BindingError, "timezone-aware"):
            self.bind(effective_at=self.at().replace(tzinfo=None), reconcile=False)


class ValidationTests(BindingCase):
    def test_the_slot_must_be_a_published_schedule(self):
        with self.assertRaisesRegex(BindingError, "no schedule has been published"):
            self.bind(slot="no-such-slot", reconcile=False)

    def test_the_slot_name_is_validated(self):
        for slot in ("", "Has Caps", "../x", "a" * 101):
            with self.subTest(slot=slot), self.assertRaisesRegex(BindingError, "slot must be"):
                self.bind(slot=slot, reconcile=False)

    def test_the_action_is_validated(self):
        with self.assertRaisesRegex(BindingError, "BIND or UNBIND"):
            self.bind(action="TOGGLE", reconcile=False)

    def test_ids_must_be_canonical_uuids(self):
        with self.assertRaises(ValueError):
            bind_requirement(
                organization_id=str(self.org.pk),
                actor_id=str(self.admin.pk),
                vehicle_id="not-a-uuid",
                template_id=str(self.template.pk),
                slot="test-0600",
                operation_id=str(uuid4()),
                correlation_id=str(uuid4()),
                request_attempt_id=str(uuid4()),
            )

    def test_inactive_unknown_and_foreign_vehicles_and_templates_are_refused(self):
        other_org = Organization.objects.create(name="Other", station_zone="UTC")
        foreign_vehicle = Vehicle.objects.create(organization=other_org, display_name="Elsewhere")
        foreign_template = ChecklistTemplate.objects.create(organization=other_org, label="Other")
        inactive = self.new_vehicle("inactive")
        inactive.active = False
        inactive.save(update_fields=["active"])
        retired = ChecklistTemplate.objects.create(
            organization=self.org, label="Retired", active=False
        )
        for kwargs, message in (
            ({"vehicle": foreign_vehicle}, "vehicle not found"),
            ({"vehicle": inactive}, "vehicle not found or not active"),
            ({"template": foreign_template}, "template not found"),
            ({"template": retired}, "template not found or not active"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(BindingError, message):
                self.bind(reconcile=False, **kwargs)
        self.assertEqual(self.bindings(), [])


class ReplayAndReconcileTests(BindingCase):
    def call(self, context, **kw):
        return bind_requirement(
            vehicle_id=str(self.vehicle.pk),
            template_id=str(self.template.pk),
            slot="test-0600",
            **{**context, **kw},
        )

    def test_the_same_operation_replays_and_a_changed_intent_is_flagged(self):
        context = self.context()
        first = self.call(context)
        again = self.call(context)
        self.assertTrue(again.replayed)
        self.assertFalse(again.operation_reused)
        self.assertEqual(again.body, first.body)
        self.assertEqual(len(self.bindings()), 1)
        reused = self.call(context, action="UNBIND")
        self.assertTrue(reused.operation_reused)
        self.assertEqual(len(self.bindings()), 1)

    def test_binding_reconciles_the_vehicle_and_a_replay_does_not(self):
        context = self.context()
        self.call(context)
        self.assertEqual(len(self.obligations()), 6)
        with mock.patch("cya.obligations.commands.reconcile_vehicle") as reconcile:
            self.call(context)
        reconcile.assert_not_called()

    def test_a_failure_during_reconciliation_does_not_undo_the_binding(self):
        with (
            mock.patch("cya.obligations.generation.build_plan", side_effect=RuntimeError("boom")),
            self.assertLogs("cya.obligations.commands", "ERROR") as logs,
        ):
            self.call(self.context())
        self.assertEqual(len(self.bindings()), 1)
        self.assertEqual(Obligation.objects.count(), 0)
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.generate()  # the scheduled generator repairs it
        self.assertEqual(len(self.obligations()), 6)


class BindRequirementCommandTests(BindingCase):
    def run_cli(self, **overrides):
        options = {
            "organization": str(self.org.pk),
            "actor": str(self.admin.pk),
            "vehicle": str(self.vehicle.pk),
            "template": str(self.template.pk),
            "slot": "test-0600",
            "operation_id": str(uuid4()),
            **overrides,
        }
        arguments = []
        for key, value in options.items():
            arguments += ["--" + key.replace("_", "-"), value]
        out = StringIO()
        call_command("bind_requirement", *arguments, stdout=out)
        return out.getvalue()

    def test_binds_replays_and_unbinds(self):
        operation = str(uuid4())
        self.assertIn("Binding committed", self.run_cli(operation_id=operation))
        self.assertIn("Unchanged: replayed", self.run_cli(operation_id=operation))
        self.assertIn("Binding committed", self.run_cli(action="UNBIND"))
        self.assertEqual([r.action for r in self.bindings()], ["BIND", "UNBIND"])

    def test_refusals_are_reported_without_a_traceback_or_a_change(self):
        with self.assertRaisesRegex(CommandError, "Binding refused: no schedule"):
            self.run_cli(slot="no-such-slot")
        with self.assertRaisesRegex(CommandError, "Binding failed"):
            self.run_cli(actor=new_id())  # unknown actor
        with self.assertRaisesRegex(CommandError, "Binding failed"):
            self.run_cli(effective_at="not-a-date")
        self.assertEqual(self.bindings(), [])

    def test_a_reused_operation_id_for_different_intent_is_a_conflict(self):
        operation = str(uuid4())
        self.run_cli(operation_id=operation)
        with self.assertRaisesRegex(CommandError, "reused for different intent"):
            self.run_cli(operation_id=operation, action="UNBIND")
        self.assertEqual(len(self.bindings()), 1)

    def test_effective_at_is_accepted_as_an_aware_instant(self):
        self.run_cli(effective_at=(self.clock.now + timedelta(days=1)).isoformat())
        self.assertEqual(len(self.bindings()), 1)
