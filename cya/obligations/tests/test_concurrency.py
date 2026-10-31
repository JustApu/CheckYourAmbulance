"""Races between generation and publication, lost runs and restart recovery (ARC-4, TIME-1).

Threads use real database connections and the real locks; the tests only make the interleaving
deterministic by pausing one side at a known point. Every scenario ends by checking that each
anchor's selection stream is gapless and linear, whatever order the work happened in.
"""

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError
from unittest import mock

from django.db import connection

from cya.checklists import commands as checklist_commands
from cya.core.models import AuditEvent, CommandReceipt
from cya.obligations import generation, planning
from cya.obligations.generation import reconcile_unit
from cya.obligations.models import (
    Obligation,
    ObligationSelection,
    OdometerObligation,
    RequirementVersion,
)
from cya.obligations.tests.support import ObligationCase


class ConcurrencyCase(ObligationCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.d1 = self.at(hours=6)

    def assert_streams_consistent(self):
        streams = defaultdict(list)
        for event in ObligationSelection.objects.order_by("sequence"):
            streams[(event.vehicle_id, event.item_id, event.due_at)].append(event)
        for anchor, events in streams.items():
            self.assertEqual([e.sequence for e in events], list(range(1, len(events) + 1)), anchor)
            self.assertEqual(events[0].selection_type, "SELECT", anchor)
            for before, after in zip(events, events[1:], strict=False):
                self.assertEqual(after.previous_selection_id, before.pk, anchor)
            chosen = [e.selected_obligation_id for e in events if e.selected_obligation_id]
            self.assertEqual(len(chosen), len(set(chosen)), anchor)  # never selected twice

    def versions_at(self, due):
        return {
            str(o.requirement_version.publication.checklist_version_id)
            for o in self.obligations()
            if o.due_at == due
        }

    def run_in_thread(self, pool, function, *args, **kwargs):
        def run():
            try:
                return function(*args, **kwargs)
            finally:
                connection.close()

        return pool.submit(run)


class PauseAfterLock:
    """Pause the first unit that takes its locks until released (later units run freely)."""

    def __init__(self):
        self.locked = threading.Event()
        self.release = threading.Event()
        self._paused = False
        self._guard = threading.Lock()
        self._real = generation.lock_unit_scope

    def __call__(self, *args, **kwargs):
        vehicle = self._real(*args, **kwargs)
        with self._guard:
            first = not self._paused
            self._paused = True
        if first:
            self.locked.set()
            self.release.wait(timeout=20)
        return vehicle


class GenerationRacingPublicationTests(ConcurrencyCase):
    def test_a_publication_waits_for_the_generator_then_reconciles_over_it(self):
        draft = self.draft()
        pause = PauseAfterLock()
        with (
            mock.patch.object(generation, "lock_unit_scope", pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            generating = self.run_in_thread(
                pool, reconcile_unit, str(self.org.pk), str(self.vehicle.pk), self.d1
            )
            self.assertTrue(pause.locked.wait(timeout=20))
            publishing = self.run_in_thread(
                pool,
                checklist_commands.publish_template_version,
                version_id=draft,
                **self.context(),
            )
            with self.assertRaises(
                TimeoutError
            ):  # blocked on the template lock the generator holds
                publishing.result(timeout=1.5)
            pause.release.set()
            self.assertEqual(generating.result(timeout=30).status, "APPLIED")
            publishing.result(timeout=30)
        self.assert_streams_consistent()
        self.assertEqual(self.versions_at(self.d1), {draft})
        # The generator selected version 1 first; the publication replaced it, in that order.
        self.assertEqual(
            list(
                ObligationSelection.objects.filter(due_at=self.d1, item=self.suction)
                .order_by("sequence")
                .values_list("selection_type", flat=True)
            ),
            ["SELECT", "REPLACE"],
        )

    def test_an_override_publication_waits_for_the_vehicle_lock(self):
        draft = checklist_commands.save_override_draft(
            vehicle_id=str(self.vehicle.pk),
            template_version_id=self.v1,
            entries=[{"item_id": str(self.siren.pk), "action": "REMOVE"}],
            **self.context(),
        ).body["override_version_id"]
        pause = PauseAfterLock()
        with (
            mock.patch.object(generation, "lock_unit_scope", pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            generating = self.run_in_thread(
                pool, reconcile_unit, str(self.org.pk), str(self.vehicle.pk), self.d1
            )
            self.assertTrue(pause.locked.wait(timeout=20))
            publishing = self.run_in_thread(
                pool,
                checklist_commands.publish_override_version,
                override_version_id=draft,
                **self.context(),
            )
            with self.assertRaises(TimeoutError):
                publishing.result(timeout=1.5)
            pause.release.set()
            generating.result(timeout=30)
            publishing.result(timeout=30)
        self.assert_streams_consistent()
        self.assertNotIn(str(self.siren.pk), {str(o.item_id) for o in self.obligations()})

    def test_a_publication_between_preview_and_lock_never_yields_the_obsolete_version(self):
        real = generation.build_plan
        state = {"published": False}

        def preview_then_publish(*args, **kwargs):
            plan = real(*args, **kwargs)
            if not state["published"]:
                state["published"] = True
                state["version"] = self.published_version()  # its own reconciliation runs here
            return plan

        with mock.patch.object(generation, "build_plan", preview_then_publish):
            result = reconcile_unit(str(self.org.pk), str(self.vehicle.pk), self.d1)
        self.assertEqual(result.status, "UNCHANGED")  # replanned: the publication already did it
        self.assertEqual(self.versions_at(self.d1), {state["version"]})
        self.assertEqual(
            set(
                ObligationSelection.objects.filter(due_at=self.d1).values_list(
                    "selection_type", flat=True
                )
            ),
            {"SELECT"},  # version 1 was never selected
        )
        self.assert_streams_consistent()

    def test_a_worker_finishing_during_the_preview_is_not_mistaken_for_a_revival(self):
        self.generate()
        with mock.patch("cya.obligations.generation.reconcile_publication"):
            v2 = self.published_version()  # its callback is lost
        real = planning._refuse_revival
        state = {"raced": False}

        def other_worker_first(*args, **kwargs):
            if not state["raced"]:
                state["raced"] = True
                other = reconcile_unit(str(self.org.pk), str(self.vehicle.pk), self.d1)
                self.assertEqual(other.status, "APPLIED")
            return real(*args, **kwargs)

        with mock.patch.object(planning, "_refuse_revival", other_worker_first):
            result = reconcile_unit(str(self.org.pk), str(self.vehicle.pk), self.d1)
        self.assertEqual(result.status, "UNCHANGED")  # replanned against what the other wrote
        self.assertEqual(self.versions_at(self.d1), {v2})
        self.assert_streams_consistent()

    def test_busy_publishing_and_generating_converge_with_consistent_streams(self):
        self.bind(vehicle=self.vehicles[1], reconcile=False)
        drafts = [self.draft(), self.draft()]
        with ThreadPoolExecutor(max_workers=3) as pool:
            generating = [
                self.run_in_thread(pool, self.generate),
                self.run_in_thread(pool, self.generate),
            ]
            publishing = self.run_in_thread(
                pool, lambda: [self.publish(d, actor=self.admin) for d in drafts]
            )
            for future in generating:
                self.assertEqual(future.result(timeout=120).failures, [])
            publishing.result(timeout=120)
        self.generate()  # a final run repairs whatever the interleaving left pending
        self.assert_streams_consistent()
        latest = drafts[-1]
        self.assertEqual(self.versions_at(self.d1), {latest})
        self.assertEqual(len(self.obligations()), 12)  # 2 vehicles x 2 due instants x 3 items
        self.assertEqual(
            OdometerObligation.objects.count(), 4
        )  # one per vehicle and scheduled inspection


class DuplicateWorkTests(ConcurrencyCase):
    def test_two_generators_on_one_unit_apply_it_once(self):
        real = generation.build_plan
        meeting = Barrier(2)
        local = threading.local()

        def meet_after_preview(*args, **kwargs):
            plan = real(*args, **kwargs)
            if not getattr(local, "met", False):
                local.met = True
                try:
                    meeting.wait(timeout=10)
                except BrokenBarrierError:
                    pass
            return plan

        with (
            mock.patch.object(generation, "build_plan", meet_after_preview),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [
                self.run_in_thread(
                    pool, reconcile_unit, str(self.org.pk), str(self.vehicle.pk), self.d1
                )
                for _ in range(2)
            ]
            statuses = sorted(f.result(timeout=60).status for f in futures)
        self.assertEqual(statuses, ["APPLIED", "REPLAYED"])
        self.assertEqual(
            CommandReceipt.objects.filter(command_type="OBLIGATIONS_RECONCILE").count(), 1
        )
        self.assertEqual(Obligation.objects.count(), 3)
        self.assertEqual(OdometerObligation.objects.count(), 1)
        self.assert_streams_consistent()

    def test_parallel_full_runs_leave_no_duplicates_or_failures(self):
        self.bind(vehicle=self.vehicles[1], reconcile=False)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [self.run_in_thread(pool, self.generate) for _ in range(3)]
            summaries = [f.result(timeout=120) for f in futures]
        self.assertTrue(all(s.failures == [] for s in summaries))
        self.assertEqual(Obligation.objects.count(), 12)
        self.assertEqual(ObligationSelection.objects.count(), 12)
        self.assertEqual(RequirementVersion.objects.count(), 6)
        self.assert_streams_consistent()


class LostRunsAndRestartTests(ConcurrencyCase):
    def test_a_crash_inside_a_unit_rolls_back_all_of_it_and_the_rerun_completes(self):
        with mock.patch.object(
            OdometerObligation.objects, "bulk_create", side_effect=RuntimeError("crash")
        ):
            summary = self.generate()
        self.assertEqual(len(summary.failures), 2)
        self.assertTrue(all("RuntimeError" in f.error for f in summary.failures))
        for model in (Obligation, ObligationSelection, RequirementVersion, OdometerObligation):
            self.assertEqual(model.objects.count(), 0, model)
        self.assertFalse(
            CommandReceipt.objects.filter(command_type="OBLIGATIONS_RECONCILE").exists()
        )
        failed = AuditEvent.objects.filter(action="OBLIGATIONS_RECONCILE", outcome="FAILED")
        self.assertEqual(failed.count(), 2)  # the failure is recorded, not the half-done work
        recovered = self.generate()
        self.assertEqual(recovered.failures, [])
        self.assertEqual(Obligation.objects.count(), 6)
        self.assert_streams_consistent()

    def test_a_crash_after_the_commit_is_recovered_by_the_next_run_without_duplicates(self):
        real = generation.run_command

        def commit_then_lose_the_response(**kwargs):
            real(**kwargs)
            raise ConnectionResetError("the worker died after COMMIT")

        with mock.patch.object(generation, "run_command", commit_then_lose_the_response):
            summary = self.generate()
        self.assertEqual(len(summary.failures), 2)
        self.assertEqual(Obligation.objects.count(), 6)  # the work did commit
        again = self.generate()
        self.assertEqual({r.status for r in again.results}, {"UNCHANGED"})
        self.assertEqual(Obligation.objects.count(), 6)
        self.assertEqual(
            CommandReceipt.objects.filter(command_type="OBLIGATIONS_RECONCILE").count(), 2
        )

    def test_a_killed_run_resumes_where_it_stopped(self):
        # The first unit commits, then the process dies: the second is simply not done yet.
        real = generation.run_command
        calls = []

        def die_on_second(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(**kwargs)

        with (
            mock.patch.object(generation, "run_command", die_on_second),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.generate()
        self.assertEqual(Obligation.objects.count(), 3)
        self.generate()
        self.assertEqual(Obligation.objects.count(), 6)
        self.assert_streams_consistent()

    def test_lost_job_runs_over_several_days_are_repaired_without_duplicates(self):
        self.generate()
        self.clock.set(self.at(days=4, hours=12))  # four days in which no job ran
        summary = self.generate()
        self.assertEqual(summary.failures, [])
        # Due instants 06-01 .. 06-07 (lookback repairs 06-03..06-05, the horizon adds 06-06/07).
        self.assertEqual(len(self.obligations()), 21)
        self.assertEqual(len(self.obligations()), Obligation.objects.count())
        self.assert_streams_consistent()
