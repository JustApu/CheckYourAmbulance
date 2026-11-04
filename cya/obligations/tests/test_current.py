"""AC-26 current projection equivalence, clock boundaries and recovery, entirely offline."""

import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from random import Random
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import SimpleTestCase

from cya.attention.checklist import _obligation_by_key
from cya.attention.readiness import _current_obligations, _obligations, fleet_readiness
from cya.attention.tests.support import AttentionCase
from cya.checks.commands import correct_check, invalidate_chain
from cya.checks.state import CheckDecision, CheckSnapshot
from cya.obligations.current import (
    compile_history,
    current_rows,
    evaluate_facts,
    refresh_obligations,
)
from cya.obligations.history import RecordedCheck, RecordedLink
from cya.obligations.models import ObligationProjection
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import (
    NO_KIT,
    ObligationEvidence,
    ProjectionHistory,
    SelectionEvidence,
    State,
    StatusEvidence,
    SuspensionEvidence,
    rebuild_vehicle_projection,
)


class CompiledEvidenceTests(SimpleTestCase):
    def test_randomized_evidence_matches_replay_and_readiness_after_clock_advances(self):
        at = datetime(2026, 6, 10, tzinfo=UTC)
        for seed in range(50):
            rng = Random(seed)
            obligations, selections, records, links, decisions, suspensions = [], [], [], [], [], []
            for n in range(12):
                oid, kit = f"o{n}", "kit-a" if n % 3 else None
                due = at + timedelta(hours=rng.randint(-170, 48))
                obligation = ObligationEvidence(
                    oid,
                    "org",
                    "vehicle",
                    f"item{n % 4}",
                    "req",
                    "pub",
                    None,
                    None,
                    at - timedelta(days=10),
                    due - timedelta(hours=1),
                    due,
                    due + timedelta(hours=1),
                    kit_instance_id=kit,
                )
                obligations.append(obligation)
                selections.append(
                    SelectionEvidence(
                        "vehicle",
                        obligation.item_id,
                        due,
                        1,
                        obligation.recorded_at,
                        oid,
                        kit or NO_KIT,
                    )
                )
                if rng.random() < 0.5 and due < at:
                    root = f"c{n}"
                    recorded = due - timedelta(minutes=15)
                    records.append(
                        RecordedCheck(
                            CheckSnapshot(
                                root,
                                root,
                                None,
                                recorded,
                                rng.choice(("PASSED", "FAILED", "UNABLE_TO_INSPECT")),
                            ),
                            obligation.item_id,
                            kit or NO_KIT,
                            "crew",
                        )
                    )
                    links.append(RecordedLink(root, oid, recorded))
                    if rng.random() < 0.5:
                        tip = root + "-correction"
                        corrected = min(at, recorded + timedelta(minutes=5))
                        records.append(
                            RecordedCheck(
                                CheckSnapshot(tip, root, root, corrected, "PASSED"),
                                obligation.item_id,
                                kit or NO_KIT,
                                "crew",
                            )
                        )
                        decisions.append(CheckDecision(root, root, "CORRECTION", corrected, tip))
                        links.append(RecordedLink(tip, oid, corrected))
                    else:
                        tip = root
                    if rng.random() < 0.3:
                        decisions.append(CheckDecision(root, tip, "INVALIDATION", at))
                if rng.random() < 0.3:
                    start = min(at, due - timedelta(minutes=30))
                    suspensions.append(SuspensionEvidence(oid, start, suspension_id=f"s{n}"))
            statuses = (
                StatusEvidence(
                    "status",
                    "vehicle",
                    1,
                    at - timedelta(days=4),
                    rng.choice(("IN_SERVICE", "OUT_OF_SERVICE")),
                ),
            )
            history = ProjectionHistory(
                "org",
                obligations=tuple(obligations),
                selections=tuple(selections),
                check_records=tuple(records),
                check_links=tuple(links),
                check_decisions=tuple(decisions),
                suspensions=tuple(suspensions),
                statuses=statuses,
            )
            compiled = compile_history(history, at)
            for delta in (timedelta(0), timedelta(hours=1), timedelta(hours=49)):
                instant = at + delta
                states = rebuild_vehicle_projection(
                    history,
                    "vehicle",
                    instant,
                    instant - timedelta(days=7),
                    instant + timedelta(hours=48),
                )
                facts = [compiled[s.obligation.id] for s in states]
                with self.subTest(seed=seed, delta=delta):
                    self.assertEqual(
                        [evaluate_facts(f, instant) for f in facts],
                        [replace(s, attention=None, deficiencies=()) for s in states],
                    )
                    self.assertEqual(
                        _current_obligations("org", instant, facts),
                        _obligations("org", "vehicle", instant, history),
                    )

    def test_shared_chain_history_counts_are_deduplicated_across_obligations(self):
        at = datetime(2026, 6, 1, 6, tzinfo=UTC)
        obligations = tuple(
            ObligationEvidence(
                oid,
                "org",
                "vehicle",
                "item",
                "req",
                "pub",
                None,
                None,
                at - timedelta(hours=2),
                at - timedelta(hours=1),
                at + timedelta(seconds=n),
                at + timedelta(seconds=n),
            )
            for n, oid in enumerate(("a", "b"))
        )
        root, tip = "root", "tip"
        records = (
            RecordedCheck(
                CheckSnapshot(root, root, None, at - timedelta(minutes=10), "FAILED"),
                "item",
                NO_KIT,
                "crew",
            ),
            RecordedCheck(
                CheckSnapshot(tip, root, root, at - timedelta(minutes=5), "PASSED"),
                "item",
                NO_KIT,
                "crew",
            ),
        )
        history = ProjectionHistory(
            "org",
            obligations=obligations,
            selections=tuple(
                SelectionEvidence(
                    "vehicle",
                    "item",
                    o.due_at,
                    1,
                    o.recorded_at,
                    o.id,
                )
                for o in obligations
            ),
            check_records=records,
            check_decisions=(
                CheckDecision(root, root, "CORRECTION", at - timedelta(minutes=5), tip),
                CheckDecision(root, tip, "INVALIDATION", at),
            ),
            check_links=tuple(
                RecordedLink(check, oid, at) for check in (root, tip) for oid in ("a", "b")
            ),
        )
        facts = list(compile_history(history, at).values())
        self.assertEqual(
            _current_obligations("org", at, facts), _obligations("org", "vehicle", at, history)
        )
        counts, _ = _current_obligations("org", at, facts)
        self.assertEqual((counts["INVALIDATED"], counts["SUPERSEDED"]), (1, 1))

    def test_future_waiver_rule_effective_instant_needs_no_refresh(self):
        from cya.obligations.state import DeterminationEvidence, RuleEvidence

        at = datetime(2026, 6, 1, tzinfo=UTC)
        obligation = ObligationEvidence(
            "o",
            "org",
            "v",
            "i",
            "r",
            "p",
            None,
            None,
            at,
            at,
            at + timedelta(hours=2),
            at + timedelta(hours=3),
        )
        rule = RuleEvidence("rule", at, at + timedelta(hours=1))
        determination = DeterminationEvidence("d", "o", at, "WAIVED", rule=rule)
        facts = compile_history(
            ProjectionHistory("org", obligations=(obligation,), determinations=(determination,)), at
        )["o"]
        self.assertEqual(evaluate_facts(facts, at).state, State.CURRENT)
        self.assertEqual(evaluate_facts(facts, at + timedelta(hours=1)).state, State.WAIVED)

    def test_microsecond_boundaries_survive_storage_and_bad_versions_fail(self):
        at = datetime(2026, 6, 1, 6, 0, 0, 123456, tzinfo=UTC)
        obligation = ObligationEvidence(
            "o",
            "org",
            "v",
            "i",
            "r",
            "p",
            None,
            None,
            at - timedelta(days=1),
            at - timedelta(hours=1),
            at,
            at,
        )
        facts = compile_history(ProjectionHistory("org", obligations=(obligation,)), at)["o"]
        self.assertEqual(evaluate_facts(facts, at).state, State.DUE)
        self.assertEqual(evaluate_facts(facts, at + timedelta(microseconds=1)).state, State.OVERDUE)
        with self.assertRaisesRegex(ValueError, "rebuild required"):
            evaluate_facts({**facts, "version": 100}, at)


class CurrentProjectionTests(AttentionCase):
    def assert_current_matches_replay(self):
        at, org, vehicle = self.clock.now, str(self.org.pk), str(self.vehicle.pk)
        actual = current_rows(org, [vehicle], at, include_all_ad_hoc=True)
        self.assertIsNotNone(actual)
        expected = get_vehicle_obligations_as_of(
            vehicle, at, organization_id=org, include_all_ad_hoc=True
        )
        self.assertEqual(
            {s.obligation.id: s for s in (evaluate_facts(f, at) for f in actual[vehicle])},
            {s.obligation.id: replace(s, attention=None, deficiencies=()) for s in expected},
        )
        self.assertEqual(
            _obligation_by_key(org, vehicle, at, None, current=True),
            {
                key: replace(value, attention=None, deficiencies=())
                for key, value in _obligation_by_key(org, vehicle, at, None).items()
            },
        )
        # Full readiness includes the same window counts, deficiency facts, status and suspensions.
        live = fleet_readiness(org, now=at)
        historical = fleet_readiness(org, now=at, as_of=at)
        self.assertEqual(live, historical)

    def test_projection_does_not_take_a_foreign_key_lock_on_the_mutable_vehicle(self):
        from django.db import connection

        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, "obligation_projection")
        parents = {
            value["foreign_key"][0] for value in constraints.values() if value["foreign_key"]
        }
        self.assertIn("obligation", parents)
        self.assertNotIn("vehicle", parents)

    def test_commands_refresh_corrections_invalidations_and_kits_atomically(self):
        self.clock.set(self.at(hours=5, minutes=15))
        self.assert_current_matches_replay()
        first = self.failed(self.suction).body["check_id"]
        self.assert_current_matches_replay()
        tip = correct_check(
            check_id=first, expected_tip_check_id=first, outcome="PASSED", **self.context(self.crew)
        ).body["check_id"]
        self.assert_current_matches_replay()
        invalidate_chain(
            check_id=tip, explanation="Synthetic invalidation.", **self.context(self.admin)
        )
        self.assert_current_matches_replay()
        self.record(self.kit_item, kit_id=self.reference)
        self.assert_current_matches_replay()
        self.deactivate(self.kit_b)
        self.assert_current_matches_replay()

    def test_clock_crossings_and_missed_rows_need_no_write_or_job(self):
        before = list(ObligationProjection.objects.order_by("pk").values())
        for at in (
            self.at(hours=5),
            self.at(hours=6),
            self.at(hours=7),
            self.at(hours=19),
            self.at(hours=19) + timedelta(microseconds=1),
            self.at(days=1, hours=6),
        ):
            self.clock.set(at)
            self.assert_current_matches_replay()
        self.assertEqual(before, list(ObligationProjection.objects.order_by("pk").values()))

    def test_rebuild_dry_run_verification_idempotence_and_missing_cache_fallback(self):
        org = str(self.org.pk)
        row = ObligationProjection.objects.first()
        ObligationProjection.objects.filter(pk=row.pk).update(
            facts={}, due_at=row.due_at + timedelta(hours=1)
        )
        changed = list(ObligationProjection.objects.order_by("pk").values())
        for option in ("dry_run", "verify"):
            with self.assertRaisesRegex(CommandError, "differences found"):
                call_command(
                    "rebuild_obligation_projection",
                    organization=org,
                    stdout=io.StringIO(),
                    **{option: True},
                )
            self.assertEqual(changed, list(ObligationProjection.objects.order_by("pk").values()))
        call_command("rebuild_obligation_projection", organization=org, stdout=io.StringIO())
        repaired = list(ObligationProjection.objects.order_by("pk").values())
        call_command("rebuild_obligation_projection", organization=org, stdout=io.StringIO())
        self.assertEqual(repaired, list(ObligationProjection.objects.order_by("pk").values()))
        call_command(
            "rebuild_obligation_projection", organization=org, verify=True, stdout=io.StringIO()
        )
        self.assert_current_matches_replay()
        ObligationProjection.objects.all().delete()
        self.assertIsNone(current_rows(org, [str(self.vehicle.pk)], self.clock.now))
        self.assertEqual(self.rows(), self.rows(as_of=self.clock.now))
        call_command("rebuild_obligation_projection", organization=org, stdout=io.StringIO())
        self.assert_current_matches_replay()

    def test_suspension_end_and_out_of_service_preserve_capped_historical_states(self):
        from cya.deficiencies.models import DeficiencyEvent
        from cya.deficiencies.suspension import end_suspension, suspend_checks
        from cya.operations.commands import set_vehicle_status

        self.clock.set(self.at(hours=5, minutes=15))
        report = self.failed(
            self.suction, outcome="UNABLE_TO_INSPECT", explanation="Synthetic reason."
        )
        deficiency = report.body["deficiency_ids"][0]
        started = suspend_checks(
            deficiency_id=deficiency,
            expected_event_id=str(
                DeficiencyEvent.objects.filter(deficiency_id=deficiency).latest("sequence").pk
            ),
            items=[{"item_id": str(self.suction.pk)}],
            **self.context(self.supervisor),
        )
        self.assert_current_matches_replay()
        self.clock.set(self.at(hours=8))  # G closed while suspended
        end_suspension(
            deficiency_id=deficiency,
            suspension_id=started.body["suspension_ids"][0],
            expected_event_id=str(
                DeficiencyEvent.objects.filter(deficiency_id=deficiency).latest("sequence").pk
            ),
            **self.context(self.supervisor),
        )
        self.assert_current_matches_replay()
        set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic outage.",
            **self.context(self.supervisor),
        )
        self.assert_current_matches_replay()
        self.clock.set(self.at(days=1, hours=8))
        set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="IN_SERVICE",
            explanation="Synthetic return.",
            **self.context(self.supervisor),
        )
        self.assert_current_matches_replay()

    def test_supervisor_determinations_and_ad_hoc_orders_refresh(self):
        from cya.conditions.uti import complete_unable_obligation, invalidate_supervisor_completion
        from cya.obligations.adhoc import request_inspection

        self.clock.set(self.at(hours=5, minutes=15))
        unable = self.failed(
            self.suction, outcome="UNABLE_TO_INSPECT", explanation="Synthetic reason."
        ).body
        completed = complete_unable_obligation(
            obligation_id=unable["obligation_links"][0]["obligation_id"],
            check_id=unable["check_id"],
            explanation="Synthetic supervisor decision.",
            **self.context(self.supervisor),
        )
        self.assert_current_matches_replay()
        invalidate_supervisor_completion(
            determination_id=str(completed.pk),
            explanation="Synthetic withdrawal.",
            **self.context(self.admin),
        )
        self.assert_current_matches_replay()
        request_inspection(
            vehicle_id=str(self.vehicle.pk),
            due_at=self.at(hours=10),
            items=[{"item_id": str(self.suction.pk), "kit_instance_id": None}],
            explanation="Synthetic explicit order.",
            **self.context(self.supervisor),
        )
        self.assert_current_matches_replay()

    def test_rebuild_rolls_back_a_group_when_independent_replay_disagrees(self):
        org = str(self.org.pk)
        row = ObligationProjection.objects.order_by("due_at", "obligation_id").first()
        ObligationProjection.objects.filter(pk=row.pk).update(facts={})
        with mock.patch(
            "cya.obligations.management.commands.rebuild_obligation_projection.get_vehicle_obligations_as_of",
            return_value=(),
        ):
            with self.assertRaisesRegex(CommandError, "event replay mismatch"):
                call_command(
                    "rebuild_obligation_projection", organization=org, stdout=io.StringIO()
                )
        self.assertEqual(ObligationProjection.objects.get(pk=row.pk).facts, {})

    def test_rebuild_replay_time_is_capped_for_old_due_groups(self):
        from cya.obligations.projections import MAX_INTERVAL

        org = str(self.org.pk)
        due = (
            ObligationProjection.objects.order_by("due_at").values_list("due_at", flat=True).first()
        )
        now = due + MAX_INTERVAL + timedelta(days=90)
        command = "cya.obligations.management.commands.rebuild_obligation_projection"
        with (
            mock.patch(f"{command}.timezone.now", return_value=now),
            mock.patch(
                f"{command}.get_vehicle_obligations_as_of",
                wraps=get_vehicle_obligations_as_of,
            ) as replay,
        ):
            call_command(
                "rebuild_obligation_projection",
                organization=org,
                verify=True,
                stdout=io.StringIO(),
            )

        self.assertTrue(replay.call_args_list)
        for call in replay.call_args_list:
            replay_at = call.args[1]
            replay_due = call.kwargs["start"]
            self.assertLessEqual(replay_at, replay_due + MAX_INTERVAL)

    def test_save_after_captured_read_instant_replays_only_the_changed_obligation(self):
        from cya.obligations import current

        self.clock.set(self.at(hours=5, minutes=15))
        captured = self.clock.now
        org, vehicle = str(self.org.pk), str(self.vehicle.pk)
        expected = current_rows(org, [vehicle], captured)
        self.record(self.suction)
        with mock.patch.object(current, "_source_facts", wraps=current._source_facts) as replay:
            actual = current_rows(org, [vehicle], captured)
        self.assertEqual(actual, expected)
        replay.assert_called_once()
        self.assertEqual(len(replay.call_args.args[1]), 1)

    def test_projection_failure_rolls_back_source_command_and_preserves_history(self):
        from cya.checks.models import CheckRecord

        self.clock.set(self.at(hours=5, minutes=15))
        at = self.clock.now
        past = self.rows(as_of=at)
        count = CheckRecord.objects.count()
        with mock.patch(
            "cya.obligations.refresh.refresh_obligations",
            side_effect=RuntimeError("synthetic projection failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic projection failure"):
                self.record(self.suction)
        self.assertEqual(CheckRecord.objects.count(), count)
        self.record(self.suction)
        self.assertEqual(self.rows(as_of=at), past)
        with self.assertRaisesRegex(RuntimeError, "requires a transaction"):
            refresh_obligations(str(self.org.pk), [])
        with transaction.atomic():
            self.assertEqual(refresh_obligations(str(self.org.pk), [], dry_run=True), [])


class CurrentProjectionRaces(AttentionCase):
    assert_current_matches_replay = CurrentProjectionTests.assert_current_matches_replay

    def test_concurrent_saves_of_different_items_match_serial_replay(self):
        from concurrent.futures import ThreadPoolExecutor

        from django.db import connection

        self.clock.set(self.at(hours=5, minutes=15))
        other = self.make_user("concurrent-crew")

        def save(item, actor, **extra):
            try:
                return self.record(item, actor=actor, **extra)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(save, self.suction, self.crew)
            second = pool.submit(save, self.masks, other, quantity=6)
            self.assertEqual(first.result(timeout=30).status, 201)
            self.assertEqual(second.result(timeout=30).status, 201)
        self.assert_current_matches_replay()

    def test_save_waiting_on_status_projection_replays_after_the_status_commit(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from django.db import connection

        from cya.obligations import current
        from cya.operations.commands import set_vehicle_status

        self.clock.set(self.at(hours=5, minutes=15))
        locked, release = threading.Event(), threading.Event()
        real = current._source_facts
        guard = threading.Lock()
        first = True

        def pause(*args, **kwargs):
            nonlocal first
            with guard:
                hold, first = first, False
            if hold:
                locked.set()
                if not release.wait(20):
                    raise RuntimeError("synthetic race timeout")
            return real(*args, **kwargs)

        def run(call):
            try:
                return call()
            finally:
                connection.close()

        with (
            mock.patch.object(current, "_source_facts", side_effect=pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            status = pool.submit(
                run,
                lambda: set_vehicle_status(
                    vehicle_id=str(self.vehicle.pk),
                    status="OUT_OF_SERVICE",
                    explanation="Synthetic concurrent outage.",
                    **self.context(self.supervisor),
                ),
            )
            self.assertTrue(locked.wait(20))
            saving = pool.submit(run, lambda: self.record(self.suction))
            try:
                with self.assertRaises(TimeoutError):
                    saving.result(timeout=0.25)
            finally:
                release.set()
            status.result(timeout=30)
            saving.result(timeout=30)
        self.assert_current_matches_replay()


class CurrentQueryLimitTests(SimpleTestCase):
    def test_every_current_evidence_query_is_limited_before_it_can_be_evaluated(self):
        from django.db.models import QuerySet

        from cya.obligations import current

        queries = []
        bounded = current._bounded

        def capture(rows, limit=None):
            if isinstance(rows, QuerySet):
                # Compile the real MySQL SELECT without executing it; SimpleTestCase
                # forbids database access. Empty results let both reads be inspected.
                queries.append(str(rows.query))
                return ()
            return bounded(rows, limit)

        vehicle = "00000000-0000-0000-0000-000000000002"
        with (
            mock.patch.object(current, "MAX_ROWS", 2),
            mock.patch.object(current, "_bounded", side_effect=capture),
            mock.patch.object(current, "selected_ids", return_value=set()),
        ):
            result = current_rows(
                "00000000-0000-0000-0000-000000000001",
                [vehicle],
                datetime(2026, 6, 1, tzinfo=UTC),
            )
        self.assertEqual(result, {vehicle: []})
        self.assertEqual(len(queries), 2)
        self.assertTrue(all("LIMIT 3" in sql for sql in queries), queries)
