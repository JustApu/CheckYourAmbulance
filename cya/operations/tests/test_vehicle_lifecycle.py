"""Vehicle lifecycle and inactive-vehicle exemption (SPEC 4.8 CFG-1, issue #130 part 2)."""

from dataclasses import replace
from datetime import timedelta

from django.db import connection

from cya.attention.tests.support import AttentionCase
from cya.checklists.models import Vehicle
from cya.core.audit_commands import CommandDenied
from cya.kits.keys import KeyRuleError
from cya.obligations.current import current_rows, evaluate_facts
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.obligations.tests.support import ObligationCase
from cya.operations.models import VehicleActivityEvent
from cya.operations.vehicles import (
    VehicleError,
    create_vehicle,
    record_activity_baseline,
    set_vehicle_active,
    update_vehicle,
)

VIN = "1FDXE4FS8KDC00007"  # synthetic


def events(vehicle_id):
    return list(
        VehicleActivityEvent.objects.filter(vehicle_id=vehicle_id)
        .order_by("sequence")
        .values_list("sequence", "active", "basis")
    )


class LifecycleCommandTests(ObligationCase):
    def create(self, actor=None, vin=VIN, **kw):
        return create_vehicle(
            vin=vin, display_name="Synthetic Unit 7", number="7", **kw, **self.context(actor)
        )

    def test_a_supervisor_creates_a_vehicle_with_its_vin_and_first_event(self):
        result = self.create(actor=self.supervisor).body
        vehicle = Vehicle.objects.get(pk=result["vehicle_id"])
        self.assertEqual((vehicle.key, vehicle.active, vehicle.number), (VIN, True, "7"))
        self.assertEqual(events(vehicle.pk), [(1, True, "CHANGE")])
        with self.assertRaises(VehicleError):
            self.create()  # the VIN is taken
        with self.assertRaises(KeyRuleError):
            self.create(vin="NOT-A-VIN")
        with self.assertRaises(CommandDenied):
            self.create(actor=self.crew, vin="1FDXE4FS8KDC00008")

    def test_name_and_number_are_edited_and_an_unchanged_edit_is_refused(self):
        vehicle_id = self.create().body["vehicle_id"]
        update_vehicle(vehicle_id=vehicle_id, display_name="Renamed", **self.context())
        update_vehicle(vehicle_id=vehicle_id, number=None, **self.context())
        vehicle = Vehicle.objects.get(pk=vehicle_id)
        self.assertEqual(
            (vehicle.display_name, vehicle.number, vehicle.key), ("Renamed", None, VIN)
        )
        with self.assertRaises(VehicleError):
            update_vehicle(vehicle_id=vehicle_id, display_name="Renamed", **self.context())

    def test_activity_needs_a_baseline_and_the_baseline_runs_once(self):
        old = self.vehicles[0]
        Vehicle.objects.filter(pk=self.vehicles[1].pk).update(active=False)
        with self.assertRaises(VehicleError):
            set_vehicle_active(vehicle_id=str(old.pk), active=False, **self.context())
        first = record_activity_baseline(str(self.org.pk))
        self.assertEqual((first.body["vehicles"], first.body["inactive"]), (2, 1))
        self.assertEqual(events(old.pk), [(1, True, "BASELINE")])
        self.assertEqual(events(self.vehicles[1].pk), [(1, False, "BASELINE")])
        again = record_activity_baseline(str(self.org.pk))
        self.assertTrue(again.replayed)
        self.assertEqual(VehicleActivityEvent.objects.count(), 2)
        set_vehicle_active(vehicle_id=str(old.pk), active=False, **self.context(self.supervisor))
        self.assertEqual(events(old.pk), [(1, True, "BASELINE"), (2, False, "CHANGE")])
        self.assertFalse(Vehicle.objects.get(pk=old.pk).active)
        with self.assertRaises(VehicleError):
            set_vehicle_active(vehicle_id=str(old.pk), active=False, **self.context())
        with self.assertRaises(CommandDenied):
            set_vehicle_active(vehicle_id=str(old.pk), active=True, **self.context(self.crew))

    def test_an_activity_change_sends_a_live_obligations_hint(self):
        from unittest import mock

        record_activity_baseline(str(self.org.pk))
        vehicle = str(self.vehicles[0].pk)
        with mock.patch("cya.live_updates.publish.publish_vehicle_update") as publish:
            result = set_vehicle_active(vehicle_id=vehicle, active=False, **self.context())
        publish.assert_called_once_with(
            organization_id=str(self.org.pk),
            vehicle_id=vehicle,
            event_id=result.body["event_id"],
            kind="obligations.changed",
        )

    def test_a_failed_reconciliation_after_an_activity_change_is_logged(self):
        # PR #162 review: reconcile_vehicle reports failed units in its summary, not by raising.
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from unittest import mock

        record_activity_baseline(str(self.org.pk))
        vehicle = str(self.vehicles[0].pk)
        failure = SimpleNamespace(
            vehicle_id=vehicle,
            due_at=datetime(2026, 6, 1, 6, tzinfo=UTC),
            error="synthetic failure",
        )
        with (
            mock.patch(
                "cya.obligations.generation.reconcile_vehicle",
                return_value=SimpleNamespace(failures=[failure]),
            ),
            self.assertLogs("cya.operations.vehicles", level="ERROR") as logged,
        ):
            set_vehicle_active(vehicle_id=vehicle, active=False, **self.context())
        self.assertEqual(len(logged.records), 1)
        self.assertIn(f"vehicle {vehicle} due 2026-06-01T06:00:00+00:00", logged.output[0])
        self.assertIn("synthetic failure", logged.output[0])
        self.assertFalse(Vehicle.objects.get(pk=vehicle).active, "the change itself committed")

    def test_the_baseline_command_counts_first_then_writes_once(self):
        import io

        from django.core.management import call_command

        Vehicle.objects.filter(pk=self.vehicles[1].pk).update(active=False)
        out = io.StringIO()
        call_command(
            "record_vehicle_activity_baseline",
            organization=str(self.org.pk),
            dry_run=True,
            stdout=out,
        )
        self.assertIn("Would record baselines: 2 vehicles (1 inactive)", out.getvalue())
        self.assertFalse(VehicleActivityEvent.objects.exists())
        out = io.StringIO()
        call_command("record_vehicle_activity_baseline", organization=str(self.org.pk), stdout=out)
        self.assertIn("Baselines recorded: 2 vehicles (1 inactive)", out.getvalue())
        out = io.StringIO()
        call_command("record_vehicle_activity_baseline", organization=str(self.org.pk), stdout=out)
        self.assertIn("replayed the earlier run", out.getvalue())
        self.assertEqual(VehicleActivityEvent.objects.count(), 2)

    def test_the_database_refuses_activity_rows_that_skip_the_rules(self):
        from django.db import IntegrityError, OperationalError, transaction

        vehicle_id = self.create().body["vehicle_id"]
        first = VehicleActivityEvent.objects.get(vehicle_id=vehicle_id)
        for case, values in {
            "no receipt of the right type": {
                "sequence": 2,
                "prior_event": first,
                "basis": "CHANGE",
            },
            "a baseline after events": {"sequence": 2, "prior_event": first, "basis": "BASELINE"},
        }.items():
            with self.subTest(case=case), self.assertRaises((IntegrityError, OperationalError)):
                with transaction.atomic():
                    VehicleActivityEvent.objects.create(
                        organization=self.org,
                        vehicle_id=vehicle_id,
                        active=True,
                        command=first.command,  # VEHICLE_CREATE, at another instant
                        recorded_at=first.recorded_at + timedelta(seconds=1),
                        actor_user=first.actor_user,
                        **values,
                    )
        with self.assertRaises((IntegrityError, OperationalError)), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE vehicle_activity_event SET active=0 WHERE id=%s", [str(first.pk)]
                )


class InactiveExemptionTests(AttentionCase):
    """Deactivation exempts routine obligations from its instant on; nothing is backfilled."""

    def states(self, at=None):
        at = at or self.clock.now
        org, vehicle = str(self.org.pk), str(self.vehicle.pk)
        replayed = get_vehicle_obligations_as_of(vehicle, at, organization_id=org)
        current = current_rows(org, [vehicle], at)
        self.assertIsNotNone(current)
        self.assertEqual(
            {s.obligation.id: s for s in (evaluate_facts(f, at) for f in current[vehicle])},
            {s.obligation.id: replace(s, attention=None, deficiencies=()) for s in replayed},
            "current reads match replay",
        )
        by_due = {}
        for state in replayed:
            by_due.setdefault(state.obligation.due_at, set()).add(state.state)
        return by_due

    def test_an_inactive_period_is_exempt_and_reactivation_never_backfills(self):
        record_activity_baseline(str(self.org.pk))
        first_due = min(self.states())  # the first generated due instant (06:00 on day 1)
        self.clock.set(first_due + timedelta(hours=3))  # 09:00 day 1: the 06:00 window is past due
        before = self.states()
        self.assertNotIn(State.EXEMPT, before[first_due])
        deactivated = set_vehicle_active(
            vehicle_id=str(self.vehicle.pk), active=False, **self.context(self.supervisor)
        )
        self.clock.set(first_due + timedelta(days=2, hours=3))  # two dues while inactive
        self.generate()
        during = self.states()
        self.assertNotIn(State.EXEMPT, during[first_due], "earlier history is unchanged")
        inactive_dues = [d for d in during if d > first_due]
        self.assertTrue(inactive_dues)
        for due in inactive_dues:
            self.assertEqual(during[due], {State.EXEMPT}, due)
        set_vehicle_active(
            vehicle_id=str(self.vehicle.pk), active=True, **self.context(self.supervisor)
        )
        self.generate()
        self.clock.set(first_due + timedelta(days=3, hours=3))
        self.generate()
        after = self.states()
        for due in inactive_dues:
            self.assertEqual(after[due], {State.EXEMPT}, f"{due} is not backfilled")
        later = [d for d in after if d > max(inactive_dues)]
        self.assertTrue(later, "obligations resume after reactivation")
        self.assertNotIn(State.EXEMPT, after[max(later)])
        exempt_reason = next(
            s
            for s in get_vehicle_obligations_as_of(
                str(self.vehicle.pk), self.clock.now, organization_id=str(self.org.pk)
            )
            if s.obligation.due_at == inactive_dues[0]
        )
        # The exemption names the deactivation event as its evidence.
        self.assertEqual(exempt_reason.state, State.EXEMPT)
        self.assertEqual(exempt_reason.evidence_id, deactivated.body["event_id"])
