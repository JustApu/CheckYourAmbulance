"""Ahead-of-time generation: horizon, idempotency, repair, exemption, schedules and failures."""

from datetime import UTC, datetime, time, timedelta

from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.db.models import Count

from cya.accounts.models import Organization
from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistTemplate
from cya.conditions.sync import create_conditional_rule
from cya.core.models import AuditEvent, CommandReceipt
from cya.obligations.commands import BindingError
from cya.obligations.models import (
    Obligation,
    ObligationSelection,
    OdometerObligation,
    RequirementVersion,
)
from cya.obligations.tests.support import START, ObligationCase
from cya.operations import commands as operation_commands
from cya.schedules.models import ScheduleRule


def counts():
    return (
        Obligation.objects.count(),
        ObligationSelection.objects.count(),
        RequirementVersion.objects.count(),
        OdometerObligation.objects.count(),
        CommandReceipt.objects.count(),
        AuditEvent.objects.count(),
    )


class GenerationCase(ObligationCase):
    def setUp(self):
        super().setUp()
        self.version_id = self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)

    def go_out_of_service(self, at, vehicle=None):
        self.clock.set(at)
        operation_commands.set_vehicle_status(
            vehicle_id=str((vehicle or self.vehicle).pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic outage.",
            **self.context(self.supervisor),
        )

    def return_to_service(self, at, vehicle=None):
        self.clock.set(at)
        operation_commands.set_vehicle_status(
            vehicle_id=str((vehicle or self.vehicle).pk),
            status="IN_SERVICE",
            explanation="Synthetic return.",
            **self.context(self.supervisor),
        )


class HorizonAndIdempotencyTests(GenerationCase):
    def test_generates_every_item_for_each_due_instant_through_the_horizon(self):
        summary = self.generate()
        self.assertEqual(summary.failures, [])
        self.assertEqual(len(summary.results), 2)
        obligations = self.obligations()
        self.assertEqual(len(obligations), 6)  # 3 items x 2 due instants (06-01 and 06-02 06:00)
        self.assertEqual(self.due_instants(), [self.at(hours=6), self.at(days=1, hours=6)])
        for obligation in obligations:
            self.assertTrue(obligation.routine)
            self.assertEqual(obligation.early_at, obligation.due_at - timedelta(hours=1))
            self.assertEqual(obligation.grace_at, obligation.due_at + timedelta(hours=1))
            self.assertEqual(str(obligation.vehicle_id), str(self.vehicle.pk))
        self.assertEqual(summary.total("selected"), 6)

    def test_each_obligation_pins_the_inputs_it_was_resolved_from(self):
        self.generate()
        for obligation in self.obligations():
            pin = obligation.requirement_version
            self.assertEqual(str(pin.vehicle_id), str(self.vehicle.pk))
            self.assertEqual(str(pin.item_id), str(obligation.item_id))
            self.assertIsNone(pin.override_version_id)
            self.assertIsNone(pin.conditional_rule_id)
            self.assertEqual(pin.binding.slot, "test-0600")
            self.assertEqual(pin.schedule_rule.slot, "test-0600")
            self.assertEqual(str(pin.publication.checklist_version_id), self.version_id)

    def test_odometer_obligation_per_scheduled_inspection(self):
        self.generate()
        rows = list(OdometerObligation.objects.order_by("due_at"))
        rule = ScheduleRule.objects.get(slot="test-0600")
        self.assertEqual(
            [r.scheduled_inspection_key for r in rows],
            [
                f"test-0600@2026-06-01T06:00:00.000000Z#{rule.pk}",
                f"test-0600@2026-06-02T06:00:00.000000Z#{rule.pk}",
            ],
        )
        self.assertEqual({str(r.schedule_rule_id) for r in rows}, {str(rule.pk)})
        self.assertEqual(rows[0].early_at, self.at(hours=5))
        self.assertEqual(rows[0].grace_at, self.at(hours=7))
        self.assertEqual(len(self.odometers()), 2)  # both are effective

    def test_rerun_creates_nothing_and_preserves_selections(self):
        self.generate()
        before = counts()
        selections = list(self.selection_events().values_list("pk", flat=True))
        summary = self.generate()
        self.assertEqual({r.status for r in summary.results}, {"UNCHANGED"})
        self.assertEqual(counts(), before)
        self.assertEqual(list(self.selection_events().values_list("pk", flat=True)), selections)

    def test_each_anchor_has_exactly_one_effective_obligation(self):
        self.generate()
        self.generate()
        anchors = self.obligations() and Obligation.objects.values(
            "vehicle", "item", "due_at"
        ).annotate(n=Count("id")).filter(n__gt=1)
        self.assertFalse(list(anchors))
        self.assertEqual(len(self.obligations()), len(self.all_obligation_rows()))

    def test_later_run_adds_only_the_new_due_instants(self):
        self.generate()
        first = {o.pk for o in self.obligations()}
        self.clock.set(self.at(days=1, hours=1))
        self.generate()
        again = {o.pk for o in self.obligations()}
        self.assertTrue(first < again)
        self.assertEqual(len(again - first), 3)
        self.assertEqual(self.due_instants()[-1], self.at(days=2, hours=6))

    def test_missed_runs_are_repaired_by_the_next_run_without_duplicates(self):
        self.generate()
        self.clock.set(self.at(days=3, hours=12))  # three days of lost job runs
        summary = self.generate()
        self.assertEqual(summary.failures, [])
        self.assertEqual(
            self.due_instants(), [self.at(days=d, hours=6) for d in range(0, 6)]
        )  # 06-01 .. 06-06
        self.assertEqual(len(self.obligations()), 18)
        self.assertEqual(len(self.obligations()), len(self.all_obligation_rows()))
        self.assertEqual(OdometerObligation.objects.count(), 6)

    def test_nothing_is_generated_before_the_schedule_binding_or_checklist_exist(self):
        self.clock.set(self.at(days=-10))
        summary = self.generate()  # lookback reaches before every effective instant
        self.assertEqual(summary.results, [])
        self.assertEqual(Obligation.objects.count(), 0)

    def test_dry_run_reports_without_writing_or_auditing(self):
        before = counts()
        summary = self.generate(dry_run=True)
        self.assertEqual({r.status for r in summary.results}, {"WOULD_APPLY"})
        self.assertEqual(summary.total("selected"), 6)
        self.assertEqual(counts(), before)

    def test_horizon_and_lookback_are_bounded(self):
        for bad in ({"horizon": -1}, {"horizon": 169}, {"lookback": 745}, {"horizon": 1.5}):
            with self.assertRaises(ValueError):
                self.generate(**bad)

    def test_horizon_and_lookback_choose_which_due_instants_are_generated(self):
        self.clock.set(self.at(hours=6, minutes=30))
        summary = self.generate(horizon=0, lookback=1)
        self.assertEqual(summary.failures, [])
        self.assertEqual(self.due_instants(), [self.at(hours=6)])  # not 06-02: horizon is zero

    def test_unbound_and_inactive_vehicles_get_nothing(self):
        other = self.vehicles[1]
        self.generate()
        self.assertEqual(self.obligations(vehicle=other), [])
        self.vehicle.active = False
        self.vehicle.save(update_fields=["active"])
        before = counts()
        self.clock.set(self.at(days=1))
        self.generate()
        self.assertEqual(counts(), before)

    def test_vehicle_filter_limits_the_run(self):
        self.bind(vehicle=self.vehicles[1], reconcile=False)
        self.generate(vehicle_ids=[str(self.vehicles[1].pk)])
        self.assertEqual(self.obligations(vehicle=self.vehicle), [])
        self.assertEqual(len(self.obligations(vehicle=self.vehicles[1])), 6)


class OutOfServiceTests(GenerationCase):
    def test_vehicle_out_of_service_now_gets_no_new_obligations(self):
        self.go_out_of_service(self.at(hours=1))
        self.generate()
        self.assertEqual(Obligation.objects.count(), 0)
        self.assertEqual(OdometerObligation.objects.count(), 0)

    def test_returning_before_the_due_instant_generates_it_on_the_next_run(self):
        self.go_out_of_service(self.at(hours=1))
        self.generate()
        self.return_to_service(self.at(hours=3))
        self.generate()
        self.assertEqual(self.due_instants(), [self.at(hours=6), self.at(days=1, hours=6)])

    def test_due_instant_inside_an_outage_is_never_backfilled_after_return(self):
        self.go_out_of_service(self.at(hours=5))
        self.return_to_service(self.at(hours=8))
        self.clock.set(self.at(hours=9))
        self.generate()
        # 06-01 06:00 fell inside the outage and is never backfilled; later ones are ordinary.
        self.assertEqual(self.due_instants(), [self.at(days=1, hours=6), self.at(days=2, hours=6)])
        self.assertEqual(OdometerObligation.objects.count(), 2)

    def test_status_at_the_due_instant_decides_not_status_at_window_open(self):
        # Out of service when the window opens (05:00) but back before D (06:00): required.
        self.go_out_of_service(self.at(hours=4))
        self.return_to_service(self.at(hours=5, minutes=30))
        self.clock.set(self.at(hours=7))
        self.generate()
        self.assertIn(self.at(hours=6), self.due_instants())

    def test_an_existing_obligation_is_never_retired_for_a_later_outage(self):
        self.generate()
        selections = self.selection_events().count()
        self.go_out_of_service(self.at(hours=5, minutes=30))
        self.generate()
        self.assertEqual(self.selection_events().count(), selections)
        self.assertEqual(len(self.obligations()), 6)  # exemption is derived, not stored (19.2)


class ScheduleAndBindingTests(GenerationCase):
    def test_future_effective_binding_applies_only_from_its_instant(self):
        other = self.vehicles[1]
        self.bind(vehicle=other, effective_at=self.at(days=1), reconcile=False)
        self.generate()
        self.assertEqual(self.due_instants(), [self.at(hours=6), self.at(days=1, hours=6)])
        self.assertEqual(
            {o.due_at for o in self.obligations(vehicle=other)}, {self.at(days=1, hours=6)}
        )

    def test_unbinding_retires_prewindow_obligations_and_stops_generation(self):
        self.generate()
        self.clock.set(self.at(hours=1))
        self.bind(action="UNBIND")  # reconciles the vehicle: both due instants are pre-window
        self.assertEqual(self.obligations(), [])
        self.assertEqual(
            set(
                self.selection_events().filter(sequence=2).values_list("selection_type", flat=True)
            ),
            {"RETIRE"},
        )
        self.generate()
        self.assertEqual(self.obligations(), [])

    def test_unbinding_after_the_window_opened_keeps_that_obligation(self):
        self.generate()
        self.clock.set(self.at(hours=5, minutes=30))
        self.bind(action="UNBIND")
        self.assertEqual({o.due_at for o in self.obligations()}, {self.at(hours=6)})

    def test_schedule_revision_moves_pending_occurrences(self):
        self.generate()  # 06-01 06:00 and 06-02 06:00
        self.clock.set(self.at(hours=1))
        self.publish_schedule("test-0600", wall_time=time(6, 30), effective_at=self.at(days=1))
        summary = self.generate()
        self.assertEqual(summary.failures, [])
        self.assertEqual(
            self.due_instants(), [self.at(hours=6), self.at(days=1, hours=6, minutes=30)]
        )
        retired = self.selection_events().filter(selection_type="RETIRE")
        self.assertEqual(retired.count(), 3)
        self.assertEqual({e.due_at for e in retired}, {self.at(days=1, hours=6)})

    def test_two_slots_for_one_vehicle_give_two_inspections_a_day(self):
        self.publish_schedule("test-1800", wall_time=time(18))
        self.bind(slot="test-1800", reconcile=False)
        self.generate()
        self.assertEqual(
            self.due_instants(),
            [
                self.at(hours=6),
                self.at(hours=18),
                self.at(days=1, hours=6),
                self.at(days=1, hours=18),
            ],
        )
        self.assertEqual(OdometerObligation.objects.count(), 4)

    def test_weekday_slot_skips_the_weekend(self):
        self.publish_schedule("test-weekday", wall_time=time(9), recurrence="WEEKDAY")
        self.bind(slot="test-weekday", vehicle=self.vehicles[1], reconcile=False)
        self.clock.set(self.at(days=4, hours=12))  # Friday 06-05
        self.generate(vehicle_ids=[str(self.vehicles[1].pk)], horizon=72)
        dues = {o.due_at for o in self.obligations(vehicle=self.vehicles[1])}
        self.assertIn(self.at(days=4, hours=9), dues)  # Friday
        self.assertNotIn(self.at(days=5, hours=9), dues)  # Saturday
        self.assertNotIn(self.at(days=6, hours=9), dues)  # Sunday


class DaylightSavingCase(ObligationCase):
    """TIME-3 on the absolute instants, with the station in New York."""

    def prepare(self, wall_time):
        Organization.objects.filter(pk=self.org.pk).update(station_zone="America/New_York")
        self.org.refresh_from_db()
        self.published_version()
        self.publish_schedule("dst-slot", wall_time=wall_time)
        self.bind(slot="dst-slot", reconcile=False)


class SpringForwardTests(DaylightSavingCase):
    clock_start = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)

    def test_nonexistent_time_resolves_to_the_first_valid_instant_and_is_idempotent(self):
        self.prepare(time(2, 30))
        self.generate(horizon=72)
        dues = self.due_instants()
        # 2026-03-08 02:30 does not exist in New York: the gap ends at 03:00 EDT = 07:00 UTC.
        self.assertIn(datetime(2026, 3, 8, 7, 0, tzinfo=UTC), dues)
        self.assertIn(datetime(2026, 3, 9, 6, 30, tzinfo=UTC), dues)  # 02:30 EDT
        before = counts()
        self.assertEqual({r.status for r in self.generate(horizon=72).results}, {"UNCHANGED"})
        self.assertEqual(counts(), before)


class FallBackTests(DaylightSavingCase):
    clock_start = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

    def test_ambiguous_time_resolves_to_its_first_occurrence(self):
        self.prepare(time(1, 30))
        self.generate(horizon=72)
        dues = self.due_instants()
        # 01:30 on 2026-11-01 occurs twice; the first is EDT = 05:30 UTC. The next day is EST.
        self.assertIn(datetime(2026, 11, 1, 5, 30, tzinfo=UTC), dues)
        self.assertNotIn(datetime(2026, 11, 1, 6, 30, tzinfo=UTC), dues)
        self.assertIn(datetime(2026, 11, 2, 6, 30, tzinfo=UTC), dues)
        # Window arithmetic is on absolute instants, whatever the wall clock does.
        first = next(
            o for o in self.obligations() if o.due_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        )
        self.assertEqual(first.grace_at - first.early_at, timedelta(hours=2))


class DeactivationTests(GenerationCase):
    """A bound template is always active: deactivation follows a recorded unbind."""

    def deactivate(self):
        with transaction.atomic():
            ChecklistTemplate.objects.filter(pk=self.template.pk).update(active=False)

    def test_a_template_cannot_be_deactivated_while_a_vehicle_is_bound_to_it(self):
        with self.assertRaisesRegex(DatabaseError, "still bound to a vehicle"):
            self.deactivate()
        self.assertTrue(ChecklistTemplate.objects.get(pk=self.template.pk).active)

    def test_a_scheduled_unbind_does_not_yet_free_the_template(self):
        # The trigger uses the database clock, so the instant must be really in the future.
        later = datetime.now(UTC) + timedelta(days=30)
        self.bind(action="UNBIND", effective_at=later, reconcile=False)
        with self.assertRaisesRegex(DatabaseError, "still bound to a vehicle"):
            self.deactivate()  # the vehicle stays bound until the unbind takes effect

    def test_deactivation_after_the_unbind_is_allowed_and_nothing_is_generated_for_it(self):
        self.generate()
        self.clock.set(self.at(hours=1))
        self.bind(action="UNBIND")  # retires pending obligations and stops generation
        self.deactivate()
        self.clock.set(self.at(days=3))
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.obligations(), [])

    def test_a_template_bound_by_one_vehicle_stays_active_after_another_unbinds(self):
        self.bind(vehicle=self.vehicles[1], reconcile=False)
        self.clock.set(self.at(hours=1))
        self.bind(action="UNBIND", reconcile=False)  # the first vehicle only
        with self.assertRaisesRegex(DatabaseError, "still bound to a vehicle"):
            self.deactivate()

    def test_reactivating_is_always_allowed(self):
        self.clock.set(self.at(hours=1))
        self.bind(action="UNBIND", reconcile=False)
        self.deactivate()
        ChecklistTemplate.objects.filter(pk=self.template.pk).update(active=True)
        self.assertTrue(ChecklistTemplate.objects.get(pk=self.template.pk).active)

    def test_an_inactive_template_cannot_be_bound(self):
        self.clock.set(self.at(hours=1))
        self.bind(action="UNBIND", reconcile=False)
        self.deactivate()
        with self.assertRaisesRegex(BindingError, "template not found or not active"):
            self.bind(reconcile=False)

    def test_unbinding_then_rebinding_restores_pre_window_obligations_with_new_pins(self):
        self.generate()
        self.clock.set(self.at(hours=1))
        self.bind(action="UNBIND")  # retires every pre-window obligation
        self.assertEqual(self.obligations(), [])
        self.clock.set(self.at(hours=2))
        self.bind()  # a new binding revision: new pins, so nothing collides with the archive
        self.assertEqual(len(self.obligations()), 6)
        kinds = {
            tuple(e.selection_type for e in self.events_of(item, self.at(hours=6)))
            for item in (self.suction, self.masks, self.siren)
        }
        self.assertEqual(kinds, {("SELECT", "RETIRE", "REPLACE")})
        self.assertEqual(len(self.odometers()), 2)

    def events_of(self, item, due):
        return list(self.selection_events().filter(item=item, due_at=due).order_by("sequence"))


class ConditionalRulePinTests(GenerationCase):
    def test_conditional_rule_in_force_is_pinned_for_its_item_only(self):
        rule = create_conditional_rule(
            organization=self.org,
            rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
            revision=1,
            typed_inputs={"item_id": str(self.suction.pk)},
            effective_at=START,
            actor_user=self.admin,
            actor_roles_at_time=["ADMINISTRATOR"],
        )
        self.generate()
        for obligation in self.obligations():
            expected = str(rule.pk) if str(obligation.item_id) == str(self.suction.pk) else None
            self.assertEqual(
                obligation.requirement_version.conditional_rule_id, expected, obligation.item_id
            )

    def test_rule_effective_later_changes_only_later_pre_window_obligations(self):
        self.generate()
        rule = create_conditional_rule(
            organization=self.org,
            rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
            revision=1,
            typed_inputs={"item_id": str(self.suction.pk)},
            effective_at=self.at(days=1),
            actor_user=self.admin,
            actor_roles_at_time=["ADMINISTRATOR"],
        )
        self.generate()
        pinned = {
            o.due_at: o.requirement_version.conditional_rule_id
            for o in self.obligations()
            if str(o.item_id) == str(self.suction.pk)
        }
        self.assertIsNone(pinned[self.at(hours=6)])
        self.assertEqual(pinned[self.at(days=1, hours=6)], str(rule.pk))


class FailureIsolationTests(GenerationCase):
    def stale_vehicle(self):
        """A vehicle whose override overlays version 1 while version 2 governs from 06-02."""
        other = self.vehicles[1]
        self.bind(vehicle=other, reconcile=False)
        draft = checklist_commands.save_override_draft(
            vehicle_id=str(other.pk),
            template_version_id=self.version_id,
            entries=[{"item_id": str(self.siren.pk), "action": "REMOVE"}],
            **self.context(),
        ).body["override_version_id"]
        checklist_commands.publish_override_version(
            override_version_id=draft, effective_at=None, **self.context()
        )
        with self.assertLogs("cya.obligations.signals", "ERROR"):
            self.published_version(effective_at=self.at(days=1))  # version 2, no matching override
        return other

    def test_an_unresolvable_requirement_is_reported_not_skipped_or_retired(self):
        other = self.stale_vehicle()
        before = self.selection_events(other).count()
        summary = self.generate()
        # Only the due instants version 2 governs fail; the rest of the run continues.
        self.assertEqual({f.vehicle_id for f in summary.failures}, {str(other.pk)})
        self.assertEqual({f.due_at for f in summary.failures}, {self.at(days=1, hours=6)})
        self.assertTrue(all("StaleOverrideError" in f.error for f in summary.failures))
        self.assertEqual(len(self.obligations(vehicle=self.vehicle)), 6)
        # Fail closed: what already existed for the failing vehicle is left exactly as it was.
        self.assertEqual(self.selection_events(other).count(), before)
        self.assertFalse(self.selection_events().filter(selection_type="RETIRE").exists())


class GenerateObligationsCommandTests(GenerationCase):
    def run_command(self, *args):
        from io import StringIO

        out, err = StringIO(), StringIO()
        call_command(
            "generate_obligations",
            "--organization",
            str(self.org.pk),
            *args,
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    def test_reports_what_it_wrote_and_is_idempotent(self):
        out, _ = self.run_command()
        self.assertIn("Wrote: 6 selected, 0 replaced, 0 retired, 2 odometer in 2 unit(s)", out)
        out, _ = self.run_command()
        self.assertIn("Wrote: 0 selected", out)
        self.assertIn("2 unit(s) already current; 0 failed", out)

    def test_dry_run_writes_nothing(self):
        before = counts()
        out, _ = self.run_command("--dry-run")
        self.assertIn("Would write: 6 selected", out)
        self.assertEqual(counts(), before)

    def test_unknown_organization_and_bad_options_are_refused(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("generate_obligations", "--organization", "not-a-uuid")
        with self.assertRaises(CommandError):
            self.run_command("--horizon-hours", "9999")

    def test_vehicle_filter_must_be_canonical_uuids(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_command("--vehicle", "not-a-uuid")
        out, _ = self.run_command("--vehicle", str(self.vehicle.pk))
        self.assertIn("6 selected", out)

    def test_exits_nonzero_and_names_failed_units(self):
        from unittest import mock

        from django.core.management.base import CommandError

        with (
            mock.patch("cya.obligations.generation.build_plan", side_effect=RuntimeError("boom")),
            self.assertRaises(CommandError) as caught,
        ):
            self.run_command()
        self.assertIn("failed", str(caught.exception))
