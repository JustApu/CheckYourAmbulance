"""Publication reconciliation of pre-window obligations (SPEC 5.7, CHK-4, contracts section 2)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import time
from unittest import mock

from django.db import connection

from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistPublication, Item
from cya.conditions.evaluator import (
    latest_obligation_determination,
    record_obligation_determination,
)
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.obligations import generation
from cya.obligations.generation import reconcile_publication, reconcile_unit
from cya.obligations.models import Obligation, ObligationSelection, OdometerObligation
from cya.obligations.planning import build_plan
from cya.obligations.tests.support import ObligationCase


class ReconciliationCase(ObligationCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.generate()  # 06-01 06:00 (D1) and 06-02 06:00 (D2), three items each

    def events(self, item, due):
        return list(ObligationSelection.objects.filter(item=item, due_at=due).order_by("sequence"))

    def kinds(self, item, due):
        return [e.selection_type for e in self.events(item, due)]

    def effective(self, due=None):
        return [o for o in self.obligations() if due is None or o.due_at == due]

    def publication_of(self, version_id):
        return ChecklistPublication.objects.get(checklist_version_id=version_id)

    def governing_versions(self, due=None):
        return {
            str(o.requirement_version.publication.checklist_version_id) for o in self.effective(due)
        }

    def override(self, remove, effective_at=None):
        draft = checklist_commands.save_override_draft(
            vehicle_id=str(self.vehicle.pk),
            template_version_id=self.v1,
            entries=[{"item_id": str(item.pk), "action": "REMOVE"} for item in remove],
            **self.context(),
        ).body["override_version_id"]
        checklist_commands.publish_override_version(
            override_version_id=draft, effective_at=effective_at, **self.context()
        )
        return draft


class TemplatePublicationTests(ReconciliationCase):
    def test_new_version_replaces_prewindow_obligations_through_the_publish_signal(self):
        self.clock.set(self.at(hours=4))  # both due instants are still before their early window
        old = {o.pk for o in self.effective()}
        v2 = self.published_version()
        self.assertEqual(self.governing_versions(), {v2})
        self.assertEqual(len(self.effective()), 6)
        for due in (self.at(hours=6), self.at(days=1, hours=6)):
            for item in (self.suction, self.masks, self.siren):
                self.assertEqual(self.kinds(item, due), ["SELECT", "REPLACE"])
        # History is retained: the superseded obligations still exist, but none is effective.
        self.assertTrue(old < {o.pk for o in self.all_obligation_rows()})
        self.assertFalse(old & {o.pk for o in self.effective()})

    def test_each_replacement_carries_the_superseded_determination_of_its_command(self):
        self.clock.set(self.at(hours=4))
        self.published_version()
        replaces = ObligationSelection.objects.filter(selection_type="REPLACE")
        self.assertEqual(replaces.count(), 6)
        for event in replaces:
            previous = event.previous_selection
            determination = event.supersession_determination
            self.assertEqual(determination.determination_type, "SUPERSEDED")
            self.assertEqual(determination.obligation_id, str(previous.selected_obligation_id))
            self.assertEqual(determination.command_id, event.command_id)
            self.assertEqual(determination.vehicle_id, str(self.vehicle.pk))
            self.assertEqual(determination.due_at, event.due_at)
            self.assertEqual(determination.evaluated_inputs["reason"], "REQUIREMENT_CHANGED")
            self.assertEqual(determination.evaluated_inputs["trigger"], "PUBLICATION")
            self.assertEqual(event.service_identity, "worker.obligation_generator")
            self.assertIsNone(event.actor_user_id)

    def test_in_window_and_past_obligations_keep_their_selection(self):
        self.clock.set(self.at(hours=5, minutes=30))  # D1's window opened at 05:00
        v2 = self.published_version()
        self.assertEqual(self.governing_versions(self.at(hours=6)), {self.v1})
        self.assertEqual(self.governing_versions(self.at(days=1, hours=6)), {v2})
        self.assertEqual(self.kinds(self.suction, self.at(hours=6)), ["SELECT"])
        self.clock.set(self.at(hours=8))  # past D1's grace period as well
        self.generate()
        self.assertEqual(self.governing_versions(self.at(hours=6)), {self.v1})

    def test_apply_to_open_obligations_is_never_available(self):
        self.clock.set(self.at(hours=5, minutes=30))
        self.published_version()
        for _ in range(3):
            self.generate()
            reconcile_publication(
                str(self.org.pk), str(ChecklistPublication.objects.latest("recorded_at").pk)
            )
        self.assertEqual(self.governing_versions(self.at(hours=6)), {self.v1})
        self.assertEqual(self.kinds(self.suction, self.at(hours=6)), ["SELECT"])

    def test_future_effective_version_governs_only_later_due_instants(self):
        v2 = self.published_version(effective_at=self.at(days=1))  # effective 06-02 00:00
        self.assertEqual(self.governing_versions(self.at(hours=6)), {self.v1})
        self.assertEqual(self.governing_versions(self.at(days=1, hours=6)), {v2})

    def test_version_effective_beyond_the_horizon_changes_nothing_yet(self):
        before = ObligationSelection.objects.count()
        self.published_version(effective_at=self.at(days=10))
        self.assertEqual(ObligationSelection.objects.count(), before)

    def test_reconciling_again_is_a_no_op(self):
        self.clock.set(self.at(hours=4))
        self.published_version()
        publication = ChecklistPublication.objects.latest("recorded_at")
        rows = (ObligationSelection.objects.count(), Obligation.objects.count())
        summary = reconcile_publication(str(self.org.pk), str(publication.pk))
        self.assertEqual({r.status for r in summary.results}, {"UNCHANGED"})
        self.assertEqual((ObligationSelection.objects.count(), Obligation.objects.count()), rows)

    def test_an_unbound_vehicle_is_not_touched_by_a_template_publication(self):
        self.clock.set(self.at(hours=4))
        self.published_version()
        self.assertEqual(self.obligations(vehicle=self.vehicles[1]), [])

    def test_as_of_answers_do_not_change_when_a_later_publication_replaces_them(self):
        self.clock.set(self.at(hours=3))
        before = self.clock.now
        self.clock.set(self.at(hours=4))
        v2 = self.published_version()
        then = self.obligations(as_of=before)
        self.assertEqual(
            {str(o.requirement_version.publication.checklist_version_id) for o in then}, {self.v1}
        )
        now = self.obligations()
        self.assertEqual(
            {str(o.requirement_version.publication.checklist_version_id) for o in now}, {v2}
        )
        self.assertEqual(len(then), len(now))

    def test_the_superseded_determination_extends_the_existing_chain(self):
        due = self.at(hours=6)
        old = next(o for o in self.effective(due) if o.item_id == str(self.suction.pk))
        waived = record_obligation_determination(
            organization=self.org,
            obligation_id=old.pk,
            determination_type="WAIVED",
            vehicle_id=str(self.vehicle.pk),
            due_at=due,
            explanation="Synthetic carry-over waiver.",
            evaluated_inputs={},
        )
        self.clock.set(self.at(hours=4))
        self.published_version()
        leaf = latest_obligation_determination(self.org, old.pk)
        self.assertEqual(leaf.determination_type, "SUPERSEDED")
        self.assertEqual(leaf.prior_determination_id, waived.pk)

    def test_vehicle_out_of_service_still_gets_its_existing_obligations_replaced(self):
        from cya.operations import commands as operation_commands

        self.clock.set(self.at(hours=3))
        operation_commands.set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic outage.",
            **self.context(self.supervisor),
        )
        extra = Item.objects.create(organization=self.org, label="Synthetic extra")
        self.clock.set(self.at(hours=4))
        v2 = self.published_version(items=[*self.base_items(), self.spec(extra, 40)])
        self.assertEqual(self.governing_versions(), {v2})  # replaced even while out of service
        self.assertNotIn(str(extra.pk), {str(o.item_id) for o in self.effective()})  # no new item


class OverrideAndRetirementTests(ReconciliationCase):
    def test_removed_item_is_retired_with_a_determination_and_leaves_no_effective_obligation(self):
        self.clock.set(self.at(hours=4))
        self.override([self.siren])
        due = self.at(hours=6)
        self.assertEqual(self.kinds(self.siren, due), ["SELECT", "RETIRE"])
        retire = self.events(self.siren, due)[-1]
        self.assertIsNone(retire.selected_obligation_id)
        self.assertEqual(retire.supersession_determination.determination_type, "SUPERSEDED")
        self.assertEqual(
            retire.supersession_determination.evaluated_inputs["reason"], "REQUIREMENT_REMOVED"
        )
        self.assertNotIn(str(self.siren.pk), {str(o.item_id) for o in self.effective()})
        # The override is part of every remaining item's pin, so those were replaced.
        self.assertEqual(self.kinds(self.suction, due), ["SELECT", "REPLACE"])

    def test_item_that_applies_again_before_its_window_is_selected_without_supersession(self):
        self.clock.set(self.at(hours=3))
        self.override([self.siren])
        self.clock.set(self.at(hours=4))
        self.override([self.suction])  # the siren is required again, the suction is not
        due = self.at(hours=6)
        self.assertEqual(self.kinds(self.siren, due), ["SELECT", "RETIRE", "REPLACE"])
        again = self.events(self.siren, due)[-1]
        self.assertIsNone(again.supersession_determination_id)
        self.assertIsNotNone(again.selected_obligation_id)
        self.assertEqual(again.previous_selection.selection_type, "RETIRE")
        self.assertIn(str(self.siren.pk), {str(o.item_id) for o in self.effective(due)})
        self.assertNotIn(str(self.suction.pk), {str(o.item_id) for o in self.effective(due)})

    def test_a_retired_item_is_not_resurrected_once_its_window_has_opened(self):
        self.clock.set(self.at(hours=3))
        self.override([self.siren])
        self.clock.set(self.at(hours=5, minutes=30))  # D1 in-window; D2 still pre-window
        self.override([self.suction])
        self.assertEqual(self.kinds(self.siren, self.at(hours=6)), ["SELECT", "RETIRE"])
        self.assertEqual(
            self.kinds(self.siren, self.at(days=1, hours=6)), ["SELECT", "RETIRE", "REPLACE"]
        )

    def test_retiring_an_item_whose_window_has_opened_is_not_allowed(self):
        self.clock.set(self.at(hours=5, minutes=30))
        self.override([self.siren])
        self.assertEqual(self.kinds(self.siren, self.at(hours=6)), ["SELECT"])
        self.assertEqual(self.kinds(self.siren, self.at(days=1, hours=6)), ["SELECT", "RETIRE"])


class UnitGuardTests(ReconciliationCase):
    def publish_without_reconciling(self):
        with mock.patch("cya.obligations.generation.reconcile_publication"):
            return self.published_version()

    def test_a_unit_that_changes_between_preview_and_lock_is_replanned_not_misapplied(self):
        self.clock.set(self.at(hours=4))
        self.publish_without_reconciling()
        due = self.at(hours=6)
        stale = build_plan(str(self.org.pk), str(self.vehicle.pk), due, self.clock.now)
        v3 = self.publish_without_reconciling()  # a newer version lands after the preview
        real = generation.build_plan
        calls = []

        def preview_is_stale(*args, **kwargs):
            calls.append(1)
            return stale if len(calls) == 1 else real(*args, **kwargs)

        with mock.patch("cya.obligations.generation.build_plan", side_effect=preview_is_stale):
            result = reconcile_unit(str(self.org.pk), str(self.vehicle.pk), due)
        self.assertEqual(result.status, "APPLIED")
        self.assertGreaterEqual(len(calls), 3)  # stale preview, rejected lock, fresh preview
        self.assertEqual(self.governing_versions(due), {v3})  # never the stale version
        self.assertEqual(self.kinds(self.suction, due), ["SELECT", "REPLACE"])
        failed = AuditEvent.objects.filter(outcome="FAILED", action="OBLIGATIONS_RECONCILE")
        self.assertEqual(failed.count(), 1)  # the rolled-back attempt is audited, not hidden

    def test_a_unit_that_never_settles_reports_a_conflict_and_writes_nothing(self):
        self.clock.set(self.at(hours=4))
        self.publish_without_reconciling()
        due = self.at(hours=6)
        stale = build_plan(str(self.org.pk), str(self.vehicle.pk), due, self.clock.now)
        self.publish_without_reconciling()
        before = ObligationSelection.objects.count()
        real = generation.build_plan
        calls = []

        def previews_stale_locks_fresh(*args, **kwargs):
            calls.append(1)
            return stale if len(calls) % 2 == 1 else real(*args, **kwargs)

        with mock.patch(
            "cya.obligations.generation.build_plan", side_effect=previews_stale_locks_fresh
        ):
            with self.assertRaises(generation.ReconcileConflict):
                reconcile_unit(str(self.org.pk), str(self.vehicle.pk), due)
        self.assertEqual(len(calls), 2 * generation.MAX_ATTEMPTS)  # bounded, never unbounded
        self.assertEqual(ObligationSelection.objects.count(), before)


class SignalTests(ReconciliationCase):
    def test_a_failing_receiver_neither_fails_nor_undoes_the_publication(self):
        self.clock.set(self.at(hours=4))
        with (
            mock.patch(
                "cya.obligations.generation.reconcile_publication", side_effect=RuntimeError("boom")
            ),
            self.assertLogs("cya.checklists.signals", "ERROR") as logs,
        ):
            v2 = self.published_version()
        self.assertTrue(self.publication_of(v2))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertEqual(self.governing_versions(), {self.v1})  # not reconciled yet
        self.generate()  # the next scheduled run repairs it
        self.assertEqual(self.governing_versions(), {v2})

    def test_a_replayed_publication_does_not_reconcile_again(self):
        self.clock.set(self.at(hours=4))
        draft = self.draft()
        context = self.context()
        checklist_commands.publish_template_version(version_id=draft, **context)
        with mock.patch("cya.obligations.generation.reconcile_publication") as reconcile:
            result = checklist_commands.publish_template_version(version_id=draft, **context)
        self.assertTrue(result.replayed)
        reconcile.assert_not_called()

    def test_a_denied_or_failed_publication_sends_nothing(self):
        with mock.patch("cya.obligations.generation.reconcile_publication") as reconcile:
            with self.assertRaises(CommandDenied):
                checklist_commands.publish_template_version(
                    version_id=self.draft(), **self.context(self.crew)
                )
        reconcile.assert_not_called()


class ReceiptAndAuditTests(ReconciliationCase):
    def test_each_unit_is_one_audited_worker_command(self):
        receipts = CommandReceipt.objects.filter(command_type="OBLIGATIONS_RECONCILE")
        self.assertEqual(receipts.count(), 2)
        for receipt in receipts:
            self.assertIsNone(receipt.actor_user_id)
            self.assertEqual(receipt.service_identity, "worker.obligation_generator")
            self.assertEqual(receipt.response_body["code"], "RECONCILED")
            self.assertEqual(receipt.response_body["selected"], 3)
            self.assertEqual(receipt.response_body["odometer"], 1)
            audit = AuditEvent.objects.get(command=receipt)
            self.assertEqual(audit.outcome, "SUCCEEDED")
            self.assertEqual(audit.service_identity, "worker.obligation_generator")

    def test_the_job_key_names_the_change_so_a_changed_unit_gets_a_new_key(self):
        due = self.at(hours=6)
        self.clock.set(self.at(hours=4))
        self.published_version()
        keys = list(
            CommandReceipt.objects.filter(command_type="OBLIGATIONS_RECONCILE")
            .order_by("recorded_at")
            .values_list("job_key", flat=True)
        )
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(k.startswith('["obligation-unit","') for k in keys))
        self.assertGreater(len(keys), 2)
        self.assertEqual(
            reconcile_unit(str(self.org.pk), str(self.vehicle.pk), due).status, "UNCHANGED"
        )


class LateRepairTests(ReconciliationCase):
    """A change recorded in time is applied by a late repair; one recorded too late never is."""

    def publish_without_reconciling(self, **kw):
        with mock.patch("cya.obligations.generation.reconcile_publication"):
            return self.published_version(**kw)

    def test_a_missed_publication_callback_is_repaired_after_the_window_when_it_was_in_time(self):
        self.clock.set(self.at(hours=4))  # D1's window opens at 05:00
        v2 = self.publish_without_reconciling()
        self.assertEqual(self.governing_versions(), {self.v1})  # the callback never ran
        self.clock.set(self.at(hours=5, minutes=30))  # the next scheduled run is after E
        summary = self.generate()
        self.assertEqual(summary.failures, [])
        self.assertEqual(self.governing_versions(self.at(hours=6)), {v2})
        self.assertEqual(self.kinds(self.suction, self.at(hours=6)), ["SELECT", "REPLACE"])
        self.assertEqual({r.status for r in self.generate().results}, {"UNCHANGED"})  # stable

    def test_a_publication_recorded_after_the_window_opened_is_never_applied_late(self):
        self.clock.set(self.at(hours=5, minutes=30))
        v2 = self.publish_without_reconciling()  # recorded after D1's E
        self.clock.set(self.at(hours=6, minutes=30))
        self.generate()
        self.assertEqual(self.governing_versions(self.at(hours=6)), {self.v1})  # in-window: kept
        self.assertEqual(self.governing_versions(self.at(days=1, hours=6)), {v2})  # pre-window

    def test_a_missed_removal_is_repaired_after_the_window_when_it_was_in_time(self):
        self.clock.set(self.at(hours=4))
        with mock.patch("cya.obligations.generation.reconcile_publication"):
            self.override([self.siren])
        self.clock.set(self.at(hours=5, minutes=30))
        self.generate()
        self.assertEqual(self.kinds(self.siren, self.at(hours=6)), ["SELECT", "RETIRE"])

    def test_a_missed_reapplication_is_repaired_after_the_window_when_it_was_in_time(self):
        self.clock.set(self.at(hours=3))
        self.override([self.siren])
        self.clock.set(self.at(hours=4))
        with mock.patch("cya.obligations.generation.reconcile_publication"):
            self.override([self.suction])  # the siren applies again; nothing reconciled it
        self.clock.set(self.at(hours=5, minutes=30))
        self.generate()
        self.assertEqual(self.kinds(self.siren, self.at(hours=6)), ["SELECT", "RETIRE", "REPLACE"])

    def test_a_removal_after_the_window_opened_does_not_hide_a_timely_reapplication(self):
        self.clock.set(self.at(hours=3))
        self.override([self.siren])
        self.clock.set(self.at(hours=4))
        with mock.patch("cya.obligations.generation.reconcile_publication"):
            self.override([self.suction])  # the siren applies again, in time; not reconciled
        self.clock.set(self.at(hours=5, minutes=30))
        with mock.patch("cya.obligations.generation.reconcile_publication"):
            self.override([self.siren])  # removed again, after E: too late for this obligation
        self.clock.set(self.at(hours=6, minutes=30))
        summary = self.generate()
        self.assertEqual(summary.failures, [])
        self.assertEqual(self.kinds(self.siren, self.at(hours=6)), ["SELECT", "RETIRE", "REPLACE"])
        self.assertEqual({r.status for r in self.generate().results}, {"UNCHANGED"})  # stable

    def test_a_late_repair_applies_only_the_changes_recorded_before_the_window(self):
        self.clock.set(self.at(hours=4))
        v2 = self.publish_without_reconciling()
        self.clock.set(self.at(hours=5, minutes=30))  # a second version after E
        self.publish_without_reconciling()
        self.clock.set(self.at(hours=6, minutes=30))
        self.generate()
        self.assertEqual(self.governing_versions(self.at(hours=6)), {v2})  # not the later one
        # The repair itself creates a new obligation; that must not move the cutoff forward.
        for later in (self.at(hours=6, minutes=40), self.at(hours=9), self.at(days=1, hours=3)):
            self.clock.set(later)
            self.generate()
            self.assertEqual(self.governing_versions(self.at(hours=6)), {v2}, later)
        self.assertEqual(self.kinds(self.suction, self.at(hours=6)), ["SELECT", "REPLACE"])

    def test_obligations_record_the_cutoff_their_requirement_was_resolved_at(self):
        created = [o for o in self.obligations() if o.due_at == self.at(hours=6)]
        self.assertEqual({o.basis_at for o in created}, {o.recorded_at for o in created})
        self.clock.set(self.at(hours=4))
        self.publish_without_reconciling()
        self.clock.set(self.at(hours=6, minutes=30))
        self.generate()
        repaired = [o for o in self.obligations() if o.due_at == self.at(hours=6)]
        # Created at 06:30 but resolved from the records before the window opened (05:00).
        self.assertEqual({o.basis_at for o in repaired}, {self.at(hours=5)})
        self.assertTrue(all(o.recorded_at > o.basis_at for o in repaired))

    def test_a_schedule_moved_in_time_retires_the_old_inspection_after_its_due_instant(self):
        due = self.at(hours=6)
        self.clock.set(self.at(hours=4))
        self.publish_schedule("test-0600", wall_time=time(6, 30), effective_at=self.at(hours=4))
        self.clock.set(self.at(hours=7))  # the first run after both the window and D
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.kinds(self.suction, due), ["SELECT", "RETIRE"])
        self.assertEqual([o for o in self.obligations() if o.due_at == due], [])
        self.assertEqual([o for o in self.odometers() if o.due_at == due], [])
        moved = self.at(hours=6, minutes=30)
        self.assertEqual(len([o for o in self.obligations() if o.due_at == moved]), 3)

    def test_a_binding_removed_in_time_retires_the_obligations_after_their_due_instant(self):
        due = self.at(hours=6)
        self.clock.set(self.at(hours=4))
        self.bind(action="UNBIND", reconcile=False)  # the vehicle's reconciliation never ran
        self.clock.set(self.at(hours=7))
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.kinds(self.suction, due), ["SELECT", "RETIRE"])
        self.assertEqual([o for o in self.obligations() if o.due_at == due], [])

    def test_a_removal_recorded_after_the_window_never_retires_a_past_obligation(self):
        due = self.at(hours=6)
        self.clock.set(self.at(hours=5, minutes=30))
        self.bind(action="UNBIND", reconcile=False)  # recorded in-window
        self.clock.set(self.at(hours=7))
        self.generate()
        self.assertEqual(self.kinds(self.suction, due), ["SELECT"])

    def test_an_obligation_created_late_adopts_current_data_and_is_not_downgraded(self):
        other = self.vehicles[1]
        self.clock.set(self.at(hours=4))
        v2 = self.publish_without_reconciling()
        self.bind(vehicle=other, reconcile=False)
        self.clock.set(self.at(hours=5, minutes=30))  # D1's window is already open
        self.generate(vehicle_ids=[str(other.pk)])
        due = self.at(hours=6)
        created = [o for o in self.obligations(vehicle=other) if o.due_at == due]
        self.assertEqual(len(created), 3)
        self.assertEqual(
            {str(o.requirement_version.publication.checklist_version_id) for o in created}, {v2}
        )
        self.clock.set(self.at(hours=5, minutes=40))
        v3 = self.publish_without_reconciling()  # recorded after those obligations existed
        self.clock.set(self.at(hours=6, minutes=30))
        self.generate(vehicle_ids=[str(other.pk)])
        now_pinned = {
            str(o.requirement_version.publication.checklist_version_id)
            for o in self.obligations(vehicle=other)
            if o.due_at == due
        }
        self.assertEqual(now_pinned, {v2})  # neither downgraded nor moved to v3
        self.assertNotIn(v3, now_pinned)


class ScheduleRaceTests(ObligationCase):
    """A schedule revision committing while a unit is being written is not lost (SPEC 5.7)."""

    def setUp(self):
        super().setUp()
        self.published_version()
        self.publish_schedule("test-0600")  # window 05:00-07:00 for the 06:00 due instant
        self.bind(reconcile=False)
        self.due = self.at(hours=6)

    def test_a_revision_committed_after_the_locked_read_is_applied_by_the_next_run(self):
        self.clock.set(self.at(hours=4))
        real = generation.build_plan
        state = {"calls": 0, "published": False}

        def revise_after_the_locked_read(*args, **kwargs):
            plan = real(*args, **kwargs)
            state["calls"] += 1
            if state["calls"] == 2 and not state["published"]:  # D1's locked recomputation
                state["published"] = True
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(self.publish_in_thread).result(timeout=60)
            return plan

        with mock.patch.object(generation, "build_plan", revise_after_the_locked_read):
            self.assertEqual(self.generate().failures, [])
        stale = [o for o in self.obligations() if o.due_at == self.due]
        self.assertEqual(
            {o.early_at for o in stale}, {self.at(hours=5)}
        )  # committed with revision 1
        self.clock.set(self.at(hours=5, minutes=30))  # the window of the stale obligations is open
        self.assertEqual(self.generate().failures, [])
        healed = [o for o in self.obligations() if o.due_at == self.due]
        self.assertEqual({o.early_at for o in healed}, {self.at(hours=3)})  # revision 2's window
        self.assertEqual({o.requirement_version.schedule_rule.revision for o in healed}, {2})

    def publish_in_thread(self):
        try:
            self.publish_schedule("test-0600", early_seconds=10800, effective_at=self.clock.now)
        finally:
            connection.close()


class OdometerFollowsInspectionTests(ReconciliationCase):
    def odometer_windows(self, due):
        return [(o.early_at, o.grace_at) for o in self.odometers() if o.due_at == due]

    def test_a_pre_window_schedule_revision_leaves_one_effective_odometer_with_the_new_window(self):
        due = self.at(hours=6)
        self.assertEqual(self.odometer_windows(due), [(self.at(hours=5), self.at(hours=7))])
        self.clock.set(self.at(hours=4))
        self.publish_schedule(
            "test-0600", early_seconds=7200, grace_seconds=10800, effective_at=self.at(hours=4)
        )
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.odometer_windows(due), [(self.at(hours=4), self.at(hours=9))])
        self.assertEqual(OdometerObligation.objects.filter(due_at=due).count(), 2)  # history kept
        windows = {(o.early_at, o.grace_at) for o in self.obligations() if o.due_at == due}
        self.assertEqual(windows, {(self.at(hours=4), self.at(hours=9))})  # equals the items'

    def test_an_in_window_schedule_revision_leaves_the_odometer_with_its_inspection(self):
        due = self.at(hours=6)
        self.clock.set(self.at(hours=5, minutes=30))
        self.publish_schedule(
            "test-0600",
            early_seconds=7200,
            grace_seconds=10800,
            effective_at=self.at(hours=5, minutes=30),
        )
        self.generate()
        self.assertEqual(self.odometer_windows(due), [(self.at(hours=5), self.at(hours=7))])
        self.assertEqual(OdometerObligation.objects.filter(due_at=due).count(), 1)

    def test_a_retired_inspection_has_no_effective_odometer_and_the_new_one_has_one(self):
        self.clock.set(self.at(hours=1))
        self.publish_schedule("test-0600", wall_time=time(6, 30), effective_at=self.at(days=1))
        self.generate()
        old_due, new_due = self.at(days=1, hours=6), self.at(days=1, hours=6, minutes=30)
        self.assertEqual(self.odometer_windows(old_due), [])  # its obligations were retired
        self.assertEqual(len(self.odometer_windows(new_due)), 1)
        self.assertEqual(OdometerObligation.objects.filter(due_at=old_due).count(), 1)

    def test_as_of_answers_about_odometers_follow_the_item_selections(self):
        due = self.at(hours=6)
        before = self.clock.now
        self.clock.set(self.at(hours=4))
        self.publish_schedule(
            "test-0600", early_seconds=7200, grace_seconds=10800, effective_at=self.at(hours=4)
        )
        self.generate()
        then = [(o.early_at, o.grace_at) for o in self.odometers(as_of=before) if o.due_at == due]
        self.assertEqual(then, [(self.at(hours=5), self.at(hours=7))])
