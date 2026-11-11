"""AC-26 current projection equivalence, clock boundaries and recovery, entirely offline."""

import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from random import Random
from types import SimpleNamespace
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
from cya.obligations import current as current_module
from cya.obligations.current import (
    attach_status_fact,
    compile_history,
    current_rows,
    evaluate_facts,
    refresh_obligations,
    status_timeline,
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
    def test_status_facts_are_serialized_once_per_event_per_read(self):
        at = datetime(2026, 6, 10, tzinfo=UTC)
        events = [
            SimpleNamespace(
                pk="status-1",
                vehicle_id="vehicle",
                sequence=1,
                recorded_at=at - timedelta(hours=2),
                status="IN_SERVICE",
            ),
            SimpleNamespace(
                pk="status-2",
                vehicle_id="vehicle",
                sequence=2,
                recorded_at=at - timedelta(hours=1),
                status="OUT_OF_SERVICE",
            ),
        ]

        serialize = current_module._json
        with mock.patch("cya.obligations.current._json", wraps=serialize) as json_encode:
            timelines = status_timeline(events)
            facts = {"obligation": {}, "checks": [], "determinations": [], "suspensions": []}
            results = [attach_status_fact(facts, "vehicle", at, at, timelines) for _ in range(100)]

        self.assertEqual(json_encode.call_count, 2)
        self.assertEqual(
            {result["statuses"][0]["status"] for result in results},
            {"OUT_OF_SERVICE"},
        )

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

    def test_an_outdated_facts_version_falls_back_to_replay_without_reader_writes(self):
        # PR #96 review 6761273052: rows written before facts version 2 (issue #80) must not
        # fail current reads after the migration; the readers replay and never rebuild.
        at, org, vehicle = self.clock.now, str(self.org.pk), str(self.vehicle.pk)
        self.assertIsNotNone(current_rows(org, [vehicle], at))
        row = ObligationProjection.objects.filter(organization_id=org, vehicle_id=vehicle).first()
        row.facts = {**row.facts, "version": 1}
        row.save(update_fields=["facts"])
        before = list(ObligationProjection.objects.order_by("pk").values())
        self.assertIsNone(current_rows(org, [vehicle], at))
        self.assertEqual(fleet_readiness(org, now=at), fleet_readiness(org, now=at, as_of=at))

        def plain(states):
            return {k: replace(v, attention=None, deficiencies=()) for k, v in states.items()}

        self.assertEqual(
            plain(_obligation_by_key(org, vehicle, at, None, current=True)),
            plain(_obligation_by_key(org, vehicle, at, None)),
        )
        self.assertEqual(before, list(ObligationProjection.objects.order_by("pk").values()))

    # Issue #99: the per-process facts cache.

    def selected_row(self, org, vehicle, at):
        (facts,) = [
            f
            for f in current_rows(org, [vehicle], at)[vehicle]
            if f["obligation"]["item_id"] == str(self.suction.pk)
            and f["obligation"]["due_at"].startswith(self.at(hours=6).date().isoformat())
        ]
        return ObligationProjection.objects.get(obligation_id=facts["obligation"]["id"])

    def test_a_cached_row_whose_content_changes_is_never_served_even_with_the_same_refresh_time(
        self,
    ):
        at, org, vehicle = self.clock.now, str(self.org.pk), str(self.vehicle.pk)
        current_module.FACTS_CACHE.clear()
        row = self.selected_row(org, vehicle, at)  # the read caches every selected row
        self.assertGreater(len(current_module.FACTS_CACHE), 0)
        stamp = row.refreshed_at
        changed = {**row.facts, "obligation": {**row.facts["obligation"], "explanation": "Marker"}}
        ObligationProjection.objects.filter(pk=row.pk).update(facts=changed, refreshed_at=stamp)
        (served,) = [
            f
            for f in current_rows(org, [vehicle], at)[vehicle]
            if f["obligation"]["id"] == str(row.pk)
        ]
        self.assertEqual(served["obligation"]["explanation"], "Marker")
        self.assertEqual(evaluate_facts(served, at).obligation.explanation, "Marker")

    def test_cached_reads_equal_uncached_reads_and_replay_and_readers_never_change_the_cache(self):
        org = str(self.org.pk)
        self.clock.set(self.at(hours=5, minutes=15))
        self.record(self.suction)  # evidence on one row, so the cache holds checks too
        self.clock.set(self.at(hours=7, minutes=30))
        at = self.clock.now
        with self.settings(CYA_CURRENT_FACTS_CACHE_ROWS=0):
            current_module.FACTS_CACHE.clear()
            uncached = fleet_readiness(org, now=at)
            self.assertEqual(len(current_module.FACTS_CACHE), 0)
        cold = fleet_readiness(org, now=at)  # fills the cache
        warm = fleet_readiness(org, now=at)  # served from it
        self.assertEqual(cold, uncached)
        self.assertEqual(warm, uncached)
        self.assertEqual(warm, fleet_readiness(org, now=at, as_of=at))
        self.assert_current_matches_replay()
        # Every cached entry still equals its stored row and carries no read's status.
        stored = dict(ObligationProjection.objects.values_list("obligation_id", "facts"))
        entries = list(current_module.FACTS_CACHE._entries.items())
        self.assertTrue(entries)
        for (oid, _digest), entry in entries:
            self.assertEqual(dict(entry), stored[oid])
            self.assertEqual(entry.evidence[4], ())

    def test_the_cache_keeps_at_most_the_configured_number_of_rows(self):
        at, org, vehicle = self.clock.now, str(self.org.pk), str(self.vehicle.pk)
        current_module.FACTS_CACHE.clear()
        with self.settings(CYA_CURRENT_FACTS_CACHE_ROWS=3):
            first = current_rows(org, [vehicle], at)
            self.assertEqual(len(current_module.FACTS_CACHE), 3)
            self.assertEqual(current_rows(org, [vehicle], at), first)
            self.assertEqual(len(current_module.FACTS_CACHE), 3)
        current_module.FACTS_CACHE.clear()

    def test_stored_facts_reports_changed_missing_and_outdated_rows_without_returning_them(self):
        at, org, vehicle = self.clock.now, str(self.org.pk), str(self.vehicle.pk)
        rows = list(
            ObligationProjection.objects.filter(organization_id=org, vehicle_id=vehicle)[:3]
        )
        self.assertEqual(len(rows), 3)
        good, stale, outdated = rows
        ObligationProjection.objects.filter(pk=outdated.pk).update(facts=["not", "facts"])
        current_module.FACTS_CACHE.clear()
        from django.db.models.functions import MD5

        digest = dict(
            ObligationProjection.objects.annotate(d=MD5("facts")).values_list("obligation_id", "d")
        )
        missing = "00000000-0000-4000-8000-000000000099"
        found, changed, old = current_module.stored_facts(
            org,
            {
                good.pk: digest[good.pk],
                stale.pk: "0" * 32,  # refreshed after its digest was read
                outdated.pk: digest[outdated.pk],
                missing: "0" * 32,
            },
        )
        self.assertEqual(set(found), {good.pk})
        self.assertEqual(dict(found[good.pk]), good.facts)
        self.assertEqual(changed, {stale.pk, missing})
        self.assertEqual(old, {outdated.pk})
        # The stale row is cached under its actual content, never under the expected digest.
        keys = set(current_module.FACTS_CACHE._entries)
        self.assertIn((stale.pk, digest[stale.pk]), keys)
        self.assertNotIn((stale.pk, "0" * 32), keys)
        self.assertNotIn((outdated.pk, digest[outdated.pk]), keys)
        self.assertIsNone(current_rows(org, [vehicle], at))  # malformed facts: replay instead

    def test_a_row_refreshed_between_digest_and_fetch_is_replayed_at_the_read_instant(self):
        at, org, vehicle = self.clock.now, str(self.org.pk), str(self.vehicle.pk)
        expected = current_rows(org, [vehicle], at)
        target = expected[vehicle][0]["obligation"]["id"]
        real = current_module.stored_facts

        def racing(organization_id, digests):
            found, changed, outdated = real(organization_id, digests)
            found.pop(target)
            return found, changed | {target}, outdated

        with (
            mock.patch.object(current_module, "stored_facts", side_effect=racing),
            mock.patch.object(
                current_module, "_source_facts", wraps=current_module._source_facts
            ) as replay,
        ):
            actual = current_rows(org, [vehicle], at)
        self.assertEqual(actual, expected)
        replay.assert_called_once()
        self.assertEqual([o.pk for o in replay.call_args.args[1]], [target])

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
        cached = current_rows(org, [str(self.vehicle.pk)], self.clock.now, include_all_ad_hoc=True)
        missing_id = cached[str(self.vehicle.pk)][0]["obligation"]["id"]
        ObligationProjection.objects.filter(pk=missing_id).delete()
        self.assertIsNone(
            current_rows(org, [str(self.vehicle.pk)], self.clock.now, include_all_ad_hoc=True)
        )
        call_command("rebuild_obligation_projection", organization=org, stdout=io.StringIO())
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

    def test_status_changes_join_at_read_time_without_refreshing_future_rows(self):
        from cya.operations.commands import set_vehicle_status

        # Fill the full supported generation horizon so this exercises the many-future-row case.
        self.clock.set(self.at(hours=5))
        self.generate(horizon=168, lookback=168)
        self.assertGreater(ObligationProjection.objects.count(), 40)
        before = list(ObligationProjection.objects.order_by("pk").values())

        with mock.patch(
            "cya.obligations.refresh.refresh_obligations",
            side_effect=AssertionError("status writes must not refresh obligation rows"),
        ) as refresh:
            set_vehicle_status(
                vehicle_id=str(self.vehicle.pk),
                status="OUT_OF_SERVICE",
                explanation="Synthetic outage.",
                **self.context(self.supervisor),
            )
        refresh.assert_not_called()
        self.assertEqual(before, list(ObligationProjection.objects.order_by("pk").values()))
        self.assert_current_matches_replay()

        self.clock.set(self.at(days=7, hours=8))
        before_return = list(ObligationProjection.objects.order_by("pk").values())
        with mock.patch(
            "cya.obligations.refresh.refresh_obligations",
            side_effect=AssertionError("status writes must not refresh obligation rows"),
        ) as refresh:
            set_vehicle_status(
                vehicle_id=str(self.vehicle.pk),
                status="IN_SERVICE",
                explanation="Synthetic return.",
                **self.context(self.supervisor),
            )
        refresh.assert_not_called()
        self.assertEqual(before_return, list(ObligationProjection.objects.order_by("pk").values()))
        self.assert_current_matches_replay()
        call_command(
            "rebuild_obligation_projection",
            organization=str(self.org.pk),
            verify=True,
            stdout=io.StringIO(),
        )

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

    def test_save_waiting_on_status_command_replays_after_the_status_commit(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from django.db import connection

        from cya.operations import commands as operation_commands
        from cya.operations.commands import set_vehicle_status

        self.clock.set(self.at(hours=5, minutes=15))
        locked, release = threading.Event(), threading.Event()

        def pause_status_hint(*args, **kwargs):
            locked.set()
            if not release.wait(20):
                raise RuntimeError("synthetic race timeout")

        def run(call):
            try:
                return call()
            finally:
                connection.close()

        with (
            mock.patch.object(operation_commands, "_hint", side_effect=pause_status_hint),
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
    def test_stable_current_page_uses_projection_selection_without_rechecking_events(self):
        from django.db.models import QuerySet

        from cya.obligations import current
        from cya.obligations.models import Obligation, ObligationProjection
        from cya.operations.models import VehicleStatusEvent

        organization = "00000000-0000-0000-0000-000000000001"
        vehicle = "00000000-0000-0000-0000-000000000002"
        obligation = "00000000-0000-0000-0000-000000000003"
        instant = datetime(2026, 6, 1, tzinfo=UTC)
        facts = {"version": current.FACTS_VERSION, "obligation": {"id": obligation}}
        current.FACTS_CACHE.clear()

        def capture(rows, limit=None):
            if not isinstance(rows, QuerySet):
                return ()
            if rows.model is Obligation:
                return ((obligation, True),)
            if rows.model is ObligationProjection and "facts" in rows._fields:
                return ((obligation, facts, "digest"),)  # the cache miss fetch
            if rows.model is ObligationProjection:
                return ((obligation, vehicle, "digest", instant, instant, True),)
            if rows.model is VehicleStatusEvent:
                return ()
            self.fail(f"unexpected bounded query for {rows.model!r}")

        with (
            mock.patch.object(current, "_bounded", side_effect=capture),
            mock.patch.object(
                current, "selected_ids", side_effect=AssertionError("selection stream reread")
            ),
            # These minimal facts are not restorable evidence; only the query shape is tested.
            mock.patch.object(current, "_evidence", return_value=((),) * 5),
        ):
            result = current_rows(organization, [vehicle], instant)

        self.assertEqual(result[vehicle], [{**facts, "statuses": []}])
        current.FACTS_CACHE.clear()  # never leave the stubbed evidence for a later test

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
            mock.patch.object(current, "selected_ids", return_value=set()) as selected,
        ):
            result = current_rows(
                "00000000-0000-0000-0000-000000000001",
                [vehicle],
                datetime(2026, 6, 1, tzinfo=UTC),
            )
        self.assertEqual(result, {vehicle: []})
        self.assertEqual(len(queries), 3)
        self.assertTrue(all("LIMIT 3" in sql for sql in queries), queries)
        selected.assert_not_called()
