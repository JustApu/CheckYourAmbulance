"""Recording a repair and its materialized verification scope (issue 153 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/153))."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock

from django.db import DatabaseError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from cya.checklists.models import ChecklistItemDef, Compartment
from cya.deficiencies.commands import record_correction, report_deficiency, start_work
from cya.deficiencies.models import (
    DeficiencyAffectedItem,
    DeficiencyEvent,
    DeficiencyProjection,
    VerificationScopeRow,
)
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.tests.support import DeficiencyCase
from cya.kits.commands import add_kit_instance


class CorrectionScopeMigrationTests(TransactionTestCase):
    def trigger_statement(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='cya_deficiency_event_bi'"
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_reversal_restores_previous_trigger_and_forward_reinstalls_correction_guard(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        before = self.trigger_statement()
        target = [
            node
            if node[0] != "cya_deficiencies"
            else ("cya_deficiencies", "0011_administrator_closure_reopen")
            for node in leaves
        ]
        try:
            executor.migrate(target)
            self.assertNotIn("CORRECTION_RECORDED", self.trigger_statement())
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(self.trigger_statement(), before)
        finally:
            MigrationExecutor(connection).migrate(leaves)

    def test_empty_scope_closure_migration_reversal_restores_previous_trigger(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        before = self.trigger_statement()
        target = [
            node
            if node[0] != "cya_deficiencies"
            else ("cya_deficiencies", "0012_correction_verification_scope")
            for node in leaves
        ]
        try:
            executor.migrate(target)
            reversed_trigger = self.trigger_statement()
            self.assertNotIn("EMPTY_SCOPE_CLOSED", reversed_trigger)
            self.assertIn("CORRECTION_RECORDED", reversed_trigger)
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(self.trigger_statement(), before)
        finally:
            MigrationExecutor(connection).migrate(leaves)


class CorrectionCase(DeficiencyCase):
    def report(self, target=None):
        return report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target=target or {"item_id": str(self.suction.pk)},
            **self.new_fields(),
            **self.context(self.crew),
        )

    def work(self, deficiency_id):
        return start_work(
            deficiency_id=deficiency_id,
            expected_event_id=str(
                DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence").pk
            ),
            **self.context(self.supervisor),
        )

    def correction(self, deficiency_id, *, actor=None, expected=None, operation_id=None):
        last = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        return record_correction(
            deficiency_id=deficiency_id,
            expected_event_id=expected or str(last.pk),
            **(
                self.context(actor or self.crew)
                | ({"operation_id": operation_id} if operation_id else {})
            ),
        )

    def ready(self, target=None):
        deficiency_id = self.report(target).body["deficiency_id"]
        self.work(deficiency_id)
        return deficiency_id

    def test_repair_moves_status_and_records_no_kit_scope_with_replay(self):
        deficiency_id = self.ready()
        expected = str(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence").pk
        )
        context = self.context(self.crew)
        result = record_correction(
            deficiency_id=deficiency_id,
            expected_event_id=expected,
            **context,
        )
        replay = record_correction(
            deficiency_id=deficiency_id,
            expected_event_id=expected,
            **context,
        )
        event = DeficiencyEvent.objects.get(pk=result.body["event_id"])
        scope = VerificationScopeRow.objects.get(deficiency_id=deficiency_id)
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status),
            ("CORRECTION_RECORDED", "IN_PROGRESS", "PENDING_VERIFICATION"),
        )
        self.assertEqual(
            DeficiencyProjection.objects.get(deficiency_id=deficiency_id).status,
            "PENDING_VERIFICATION",
        )
        self.assertEqual(
            (scope.item_id, scope.kit_instance_id, scope.instance_key),
            (str(self.suction.pk), None, "00000000-0000-0000-0000-000000000000"),
        )
        self.assertEqual(scope.required_after, event.recorded_at)
        self.assertEqual(scope.scope_revision, 1)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.body["event_id"], result.body["event_id"])
        self.assertEqual(VerificationScopeRow.objects.count(), 1)

    def test_only_in_progress_can_record_correction_and_stale_event_is_conflict(self):
        deficiency_id = self.report().body["deficiency_id"]
        with self.assertRaises(DeficiencyError) as caught:
            self.correction(deficiency_id)
        self.assertEqual(caught.exception.code, "INVALID_TRANSITION")
        self.work(deficiency_id)
        stale = str(DeficiencyEvent.objects.get(deficiency_id=deficiency_id, sequence=1).pk)
        with self.assertRaises(DeficiencyError) as caught:
            self.correction(deficiency_id, expected=stale)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("DEFICIENCY_CHANGED", 409)
        )

    def test_every_role_can_record_a_correction(self):
        for actor in (self.crew, self.supervisor, self.supply, self.admin):
            with self.subTest(role=actor.pk):
                deficiency_id = self.ready()
                result = self.correction(deficiency_id, actor=actor)
                event = DeficiencyEvent.objects.get(pk=result.body["event_id"])
                self.assertEqual(event.actor_user_id, actor.pk)

    def test_empty_scope_fails_closed_without_event_or_projection_change(self):
        deficiency_id = self.ready()
        last = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        with mock.patch("cya.deficiencies.service.correction_scope", return_value=[]):
            with self.assertRaises(DeficiencyError) as caught:
                self.correction(deficiency_id, expected=str(last.pk))
        self.assertEqual(caught.exception.code, "EMPTY_SCOPE")
        self.assertEqual(
            DeficiencyProjection.objects.get(deficiency_id=deficiency_id).status, "IN_PROGRESS"
        )
        self.assertEqual(VerificationScopeRow.objects.count(), 0)

    def test_compartment_scope_includes_current_items(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        deficiency_id = self.ready({"compartment_id": str(compartment.pk)})
        result = self.correction(deficiency_id)
        item_ids = set(
            VerificationScopeRow.objects.filter(
                correction_event_id=result.body["event_id"]
            ).values_list("item_id", flat=True)
        )
        self.assertEqual(
            item_ids,
            {str(i.pk) for i in (self.suction, self.masks, self.gauge, self.special, self.limited)},
        )

    def test_vehicle_scope_uses_the_report_time_affected_item_rows(self):
        from cya.checklists.models import VehicleSystem

        system = VehicleSystem.objects.get(vehicle=self.vehicle)
        deficiency_id = self.ready({"vehicle": True, "system_ids": [str(system.pk)]})
        result = self.correction(deficiency_id)
        item_ids = set(
            VerificationScopeRow.objects.filter(
                correction_event_id=result.body["event_id"]
            ).values_list("item_id", flat=True)
        )
        self.assertEqual(item_ids, {str(self.siren.pk), str(self.odometer.pk)})

    def test_vehicle_scope_preserves_a_reported_item_after_its_definition_is_removed(self):
        from cya.checklists.models import VehicleSystem

        system = VehicleSystem.objects.get(vehicle=self.vehicle)
        deficiency_id = self.ready({"vehicle": True, "system_ids": [str(system.pk)]})
        version = self.draft([item for item in self.items if item["item_id"] != str(self.siren.pk)])
        self.publish(version)
        result = self.correction(deficiency_id)
        rows = {
            str(row.item_id): str(row.definition_id)
            for row in VerificationScopeRow.objects.filter(
                correction_event_id=result.body["event_id"]
            )
        }
        affected = {
            str(row.item_id): str(row.definition_id)
            for row in DeficiencyAffectedItem.objects.filter(deficiency_id=deficiency_id)
        }
        self.assertEqual(rows, affected)

    def test_database_rejects_a_forged_item_outside_the_deficiency_target(self):
        deficiency_id = self.ready()
        result = self.correction(deficiency_id)
        valid = VerificationScopeRow.objects.get(deficiency_id=deficiency_id)
        with self.assertRaises(DatabaseError) as caught:
            VerificationScopeRow.objects.create(
                organization=valid.organization,
                command=valid.command,
                actor_user=valid.actor_user,
                actor_roles_at_time=valid.actor_roles_at_time,
                recorded_at=valid.recorded_at,
                deficiency=valid.deficiency,
                correction_event=valid.correction_event,
                item=self.masks,
                definition=ChecklistItemDef.objects.get(item=self.masks),
                kit_instance=None,
                instance_key="00000000-0000-0000-0000-000000000000",
                required_after=valid.required_after,
                scope_revision=valid.scope_revision,
            )
        self.assertEqual(caught.exception.args[0], 1644)
        self.assertIn("outside the deficiency target", str(caught.exception))
        self.assertEqual(result.body["scope_count"], 1)
        self.assertEqual(VerificationScopeRow.objects.count(), 1)

    def test_two_concurrent_corrections_only_append_once(self):
        deficiency_id = self.ready()
        expected = str(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence").pk
        )
        barrier = Barrier(2)

        def submit():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.correction(deficiency_id, expected=expected)
            except Exception as error:  # returned to the assertion thread
                return error
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _n: submit(), range(2)))
        successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(failures), 1, outcomes)
        self.assertIsInstance(failures[0], DeficiencyError)
        self.assertEqual(failures[0].code, "DEFICIENCY_CHANGED")
        self.assertEqual(VerificationScopeRow.objects.count(), 1)


class CorrectionKitScopeTests(DeficiencyCase):
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

        self.reference_kit = add_kit("Reference kit")
        self.first_kit = self.reference_kit
        self.second_kit = add_kit("Second physical kit")
        self.version_id = self.published_version(
            [*self.base_items(), self.spec(self.kit_item, 80, kit_id=self.reference_kit)]
        )

    def report(self, target):
        return report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target=target,
            **self.new_fields(),
            **self.context(self.crew),
        )

    def work_and_correct(self, target):
        deficiency_id = self.report(target).body["deficiency_id"]
        last = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        start_work(
            deficiency_id=deficiency_id,
            expected_event_id=str(last.pk),
            **self.context(self.supervisor),
        )
        last = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        result = record_correction(
            deficiency_id=deficiency_id,
            expected_event_id=str(last.pk),
            **self.context(self.crew),
        )
        return deficiency_id, result

    def test_compartment_rows_distinguish_identical_items_in_two_kit_instances(self):
        deficiency_id, result = self.work_and_correct({"compartment_id": str(self.cabinet.pk)})
        rows = list(
            VerificationScopeRow.objects.filter(
                deficiency_id=deficiency_id, item_id=self.kit_item.pk
            ).order_by("kit_instance_id")
        )
        self.assertEqual(
            sorted(row.kit_instance_id for row in rows), sorted([self.first_kit, self.second_kit])
        )
        self.assertEqual(len({row.instance_key for row in rows}), 2)
        # The compartment has two non-kit checklist items and the same kit item
        # in each of the two physical kits.
        self.assertEqual(result.body["scope_count"], 4)

    def test_kit_target_records_only_that_physical_kit(self):
        deficiency_id, _result = self.work_and_correct({"kit_id": self.first_kit})
        rows = list(VerificationScopeRow.objects.filter(deficiency_id=deficiency_id))
        self.assertEqual(
            [(row.item_id, row.kit_instance_id) for row in rows],
            [(str(self.kit_item.pk), self.first_kit)],
        )

    def test_logical_item_target_covers_each_active_kit_instance(self):
        deficiency_id, result = self.work_and_correct({"item_id": str(self.kit_item.pk)})
        rows = list(
            VerificationScopeRow.objects.filter(deficiency_id=deficiency_id).order_by(
                "kit_instance_id"
            )
        )
        self.assertEqual({row.kit_instance_id for row in rows}, {self.first_kit, self.second_kit})
        self.assertEqual(result.body["scope_count"], 2)
