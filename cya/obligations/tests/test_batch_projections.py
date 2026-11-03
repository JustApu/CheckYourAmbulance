"""The batch obligation history loader (issue #33) equals the per-vehicle loader, vehicle by vehicle.

load_projection_histories runs each query of load_projection_history once for a page of vehicles
and splits the rows by vehicle. These tests compare the two loaders' ProjectionHistory values,
which are exact tuples in the loaders' order, over a seeded history with checks, deficiencies and
kits and over a scenario that touches every evidence kind on two vehicles that share checklist
items. They also check that the query count does not grow with the number of vehicles and that
the bounds refuse rather than cut.
"""

from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import connection
from django.test.utils import CaptureQueriesContext

from cya.attention.tests.support import AttentionCase
from cya.checklists.models import Vehicle
from cya.checks.commands import correct_check, invalidate_chain
from cya.checks.models import CheckObligationLink
from cya.checks.tests.test_obligation_links import ObligationLinkCleanupMixin
from cya.conditions.uti import complete_unable_obligation
from cya.deficiencies.closure import close_administrator
from cya.deficiencies.models import DeficiencyEvent
from cya.deficiencies.suspension import suspend_checks
from cya.kits.tests.support import KitCase
from cya.obligations import projections
from cya.obligations.adhoc import request_inspection
from cya.obligations.projections import load_projection_histories, load_projection_history
from cya.obligations.tests.synthetic_history import generate_history
from cya.operations.commands import set_vehicle_status

WINDOW_BACK = timedelta(days=7)
WINDOW_AHEAD = timedelta(hours=48)


class BatchAssertions:
    def assert_batch_equals_per_vehicle(self, vehicle_ids, instant, **window):
        org = str(self.org.pk)
        batch = load_projection_histories(vehicle_ids, instant, organization_id=org, **window)
        self.assertEqual(list(batch), list(dict.fromkeys(vehicle_ids)))
        for vehicle_id in vehicle_ids:
            single = load_projection_history(vehicle_id, instant, organization_id=org, **window)
            self.assertEqual(batch[vehicle_id], single, f"vehicle={vehicle_id}, instant={instant}")
        return batch


class SeededBatchTests(BatchAssertions, ObligationLinkCleanupMixin, KitCase):
    """Weeks of publications, schedules, status changes, checks, deficiencies and kits."""

    def assert_seed(self, seed):
        vehicle_ids = [str(v.pk) for v in self.vehicles]
        window = {"start": self.at(), "end": self.at(days=17)}
        seen = set()
        for index, instant in enumerate(generate_history(self, seed=seed, checks=True, kits=True)):
            if index == 0:
                # Vehicle B shares the checklist once its schedule exists, so the batch must split
                # real rows between two vehicles.
                self.bind(vehicle=self.vehicles[1])
            batch = self.assert_batch_equals_per_vehicle(vehicle_ids, instant, **window)
            for vehicle_id, history in batch.items():
                if history.obligations:
                    seen.add(vehicle_id)
        mine = load_projection_history(
            str(self.vehicle.pk), self.clock.now, organization_id=str(self.org.pk), **window
        )
        # The history really exercises checks, determinations and deficiencies.
        self.assertTrue(mine.check_records and mine.check_links and mine.deficiency_events)
        self.assertTrue(mine.determinations)
        self.assertEqual(seen, set(vehicle_ids))  # every vehicle had obligations to split

    def test_a_seeded_history_loads_the_same_in_a_batch(self):
        self.assert_seed(8701)

    def test_another_seed_loads_the_same_in_a_batch(self):
        self.assert_seed(8702)


class TwoVehicleCase(AttentionCase):
    """Vehicle B is bound to the same checklist as A, so the two share every item."""

    def setUp(self):
        super().setUp()
        self.bind(vehicle=self.vehicles[1], reconcile=False)
        self.assertEqual(self.generate().failures, [])


class ScenarioBatchTests(BatchAssertions, TwoVehicleCase):
    """Every evidence kind on two vehicles that share checklist items, at several instants."""

    def latest(self, pk):
        return str(DeficiencyEvent.objects.filter(deficiency_id=pk).latest("sequence").pk)

    def test_two_vehicles_with_every_evidence_kind_split_exactly(self):
        a, b = self.vehicles[0], self.vehicles[1]
        empty = Vehicle.objects.create(organization=self.org, display_name="Synthetic empty")
        vehicle_ids = [str(a.pk), str(b.pk), str(empty.pk)]
        self.clock.set(self.clock.now + timedelta(hours=5))  # inside the 06:00 window
        instants = [self.clock.now]

        # Vehicle A: a Pass, an invalidated chain, a corrected Failed check, and an Unable check
        # that a supervisor completes (a determination with a supporting check).
        self.record(self.suction)
        bad = self.record(self.masks, quantity=6).body["check_id"]
        invalidate_chain(check_id=bad, explanation="Synthetic reason.", **self.context(self.admin))
        wrong = self.failed(self.siren).body["check_id"]
        correct_check(
            check_id=wrong, expected_tip_check_id=wrong, outcome="PASSED", **self.context(self.crew)
        )
        unable = self.failed(
            self.kit_item,
            kit_id=self.reference,
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic reason.",
        ).body["check_id"]
        link = CheckObligationLink.objects.get(check_record_id=unable)
        complete_unable_obligation(
            obligation_id=str(link.obligation_id),
            check_id=unable,
            explanation="Synthetic completion.",
            **self.context(self.supervisor),
        )
        instants.append(self.clock.now)

        # Vehicle B: its own Pass and Failed check (a deficiency) on the same items, a check
        # suspension that a closure then ends, an ad hoc order and an out-of-service interval.
        self.clock.set(self.clock.now + timedelta(minutes=10))
        self.record(self.suction, vehicle=b)
        deficiency = self.failed(self.masks, vehicle=b, quantity=1).body["deficiency_ids"][0]
        suspend_checks(
            deficiency_id=deficiency,
            expected_event_id=self.latest(deficiency),
            items=[{"item_id": str(self.masks.pk)}],
            **self.context(self.supervisor),
        )
        request_inspection(
            vehicle_id=str(b.pk),
            items=[{"item_id": str(self.suction.pk), "kit_instance_id": None}],
            due_at=self.clock.now + timedelta(hours=6),
            explanation="Synthetic return inspection.",
            **self.context(self.supervisor),
        )
        instants.append(self.clock.now)
        self.clock.set(self.clock.now + timedelta(minutes=10))
        close_administrator(
            deficiency_id=deficiency,
            expected_event_id=self.latest(deficiency),
            explanation="Synthetic closure.",
            **self.context(self.admin),
        )
        set_vehicle_status(
            vehicle_id=str(b.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic reason.",
            **self.context(self.supervisor),
        )
        self.clock.set(self.clock.now + timedelta(days=1))
        instants.append(self.clock.now)

        for instant in instants:
            with self.subTest(instant=instant):
                batch = self.assert_batch_equals_per_vehicle(
                    vehicle_ids, instant, start=instant - WINDOW_BACK, end=instant + WINDOW_AHEAD
                )
        final_a, final_b = batch[str(a.pk)], batch[str(b.pk)]
        # Each vehicle kept only its own rows although the items are shared.
        self.assertTrue(final_a.check_records and final_b.check_records)
        self.assertFalse(
            {r.snapshot.id for r in final_a.check_records}
            & {r.snapshot.id for r in final_b.check_records}
        )
        self.assertTrue(final_a.determinations and final_a.check_supports)
        self.assertTrue(final_b.suspensions and final_b.statuses and final_b.deficiency_events)
        self.assertTrue(any(not o.routine for o in final_b.obligations))  # the ad hoc order
        self.assertFalse(final_a.suspensions or final_a.statuses)
        self.assertEqual(batch[str(empty.pk)].obligations, ())


class BatchQueryTests(BatchAssertions, TwoVehicleCase):
    def queries(self, vehicle_ids):
        with CaptureQueriesContext(connection) as captured:
            load_projection_histories(vehicle_ids, self.clock.now, organization_id=str(self.org.pk))
        return len(captured.captured_queries)

    def test_the_query_count_does_not_grow_with_the_number_of_vehicles(self):
        self.clock.set(self.clock.now + timedelta(hours=5))
        a, b = self.vehicles[0], self.vehicles[1]
        # Vehicle A alone already has every evidence kind, so no query is skipped for an empty
        # id list: only the number of vehicles changes between the three reads.
        self.record(self.suction)
        self.failed(self.siren)
        self.failed(self.masks, vehicle=b, quantity=1)
        third = Vehicle.objects.create(organization=self.org, display_name="Synthetic third")
        one = self.queries([str(a.pk)])
        self.assertEqual(self.queries([str(a.pk), str(b.pk)]), one)
        self.assertEqual(self.queries([str(a.pk), str(b.pk), str(third.pk)]), one)
        with CaptureQueriesContext(connection) as captured:
            for vehicle in (a, b):
                load_projection_history(
                    str(vehicle.pk), self.clock.now, organization_id=str(self.org.pk)
                )
        self.assertGreater(len(captured.captured_queries), one)  # the storm this replaces

    def test_bounds_refuse_rather_than_cut(self):
        org = str(self.org.pk)
        a, b = str(self.vehicles[0].pk), str(self.vehicles[1].pk)
        self.assertEqual(load_projection_histories([], self.clock.now, organization_id=org), {})
        self.assertEqual(
            list(load_projection_histories([a, a, b], self.clock.now, organization_id=org)),
            [a, b],
        )
        with mock.patch.object(projections, "MAX_PAGE_VEHICLES", 1):
            with self.assertRaisesRegex(ValueError, "at most 1 vehicles"):
                load_projection_histories([a, b], self.clock.now, organization_id=org)
        with mock.patch.object(projections, "MAX_ROWS", 1):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                load_projection_histories([a, b], self.clock.now, organization_id=org)
        for unknown in (str(uuid4()),):
            with self.assertRaises(Vehicle.DoesNotExist):
                load_projection_histories([a, unknown], self.clock.now, organization_id=org)
        with self.assertRaises(Vehicle.DoesNotExist):  # another organization's scope
            load_projection_histories([a], self.clock.now, organization_id=str(uuid4()))
        with self.assertRaises(ValueError):  # the same interval rule as the per-vehicle loader
            load_projection_histories(
                [a],
                self.clock.now,
                organization_id=org,
                start=self.clock.now,
                end=self.clock.now + timedelta(days=40),
            )
