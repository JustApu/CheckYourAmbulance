"""As-of API and independent replay/reference comparisons for AC-26 and AC-27."""

from dataclasses import replace
from datetime import timedelta
from random import Random
from unittest import mock

from cya.accounts.models import Organization
from cya.attention.facts import item_attention
from cya.checklists.models import Vehicle
from cya.checks.models import CheckEvent, CheckRecord
from cya.checks.state import CheckDecision, CheckSnapshot, effective_checks
from cya.checks.tests.test_obligation_links import ObligationLinkCleanupMixin
from cya.conditions.evaluator import record_obligation_determination
from cya.conditions.models import ObligationDetermination
from cya.conditions.sync import create_conditional_rule
from cya.deficiencies.models import DeficiencyProjection
from cya.kits.tests.support import KitCase
from cya.obligations.models import ObligationSelection
from cya.obligations.projections import get_vehicle_obligations_as_of, load_projection_history
from cya.obligations.queries import effective_obligations
from cya.obligations.state import NO_KIT, CheckEvidence, State, rebuild_vehicle_projection
from cya.obligations.tests.support import ObligationCase
from cya.obligations.tests.synthetic_history import generate_history
from cya.operations.status import status_at


class ProjectionTests(ObligationCase):
    def setUp(self):
        super().setUp()
        self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.generate()

    def query(self, instant=None, **kw):
        return get_vehicle_obligations_as_of(
            str(self.vehicle.pk), instant or self.clock.now, organization_id=str(self.org.pk), **kw
        )

    def test_check_adapter_is_scoped_historical_and_default_is_unsatisfied(self):
        self.clock.set(self.at(hours=6))
        row = self.obligations()[0]
        check = CheckEvidence(
            "stub",
            str(self.org.pk),
            str(self.vehicle.pk),
            str(row.item_id),
            str(row.pk),
            self.at(hours=5),
            "FAILED",
            voided_at=self.at(hours=7),
        )
        lookup = mock.Mock(return_value=[check])
        at_before = self.clock.now
        before = self.query(check_lookup=lookup)
        self.assertEqual(
            next(r for r in before if r.obligation.id == str(row.pk)).outcome, "FAILED"
        )
        self.assertEqual(lookup.call_args.kwargs["as_of"], self.clock.now)
        self.clock.set(self.at(hours=8))
        now = self.query(check_lookup=lookup)
        self.assertEqual(
            next(r for r in now if r.obligation.id == str(row.pk)).state, State.OVERDUE
        )
        self.assertFalse(any(r.state == State.SATISFIED for r in self.query()))
        history = load_projection_history(
            str(self.vehicle.pk),
            self.clock.now,
            organization_id=str(self.org.pk),
            check_lookup=lookup,
        )
        with self.assertNumQueries(0):
            self.assertEqual(
                rebuild_vehicle_projection(
                    history,
                    str(self.vehicle.pk),
                    at_before,
                    at_before - timedelta(days=7),
                    at_before + timedelta(hours=48),
                ),
                before,
            )
        with self.assertRaises(ValueError):
            self.query(check_lookup=lambda **kw: [replace(check, organization_id="another")])
        with self.assertRaises(RuntimeError):
            self.query(check_lookup=mock.Mock(side_effect=RuntimeError("stub unavailable")))

    def test_determinations_are_recorded_time_scoped_not_current_leaves(self):
        row = self.obligations()[0]
        rule = create_conditional_rule(
            organization=self.org,
            rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
            revision=1,
            typed_inputs={"item_id": str(row.item_id)},
            effective_at=self.clock.now,
            actor_user=self.admin,
        )
        self.clock.set(self.at(hours=4))
        waiver = record_obligation_determination(
            organization=self.org, obligation_id=str(row.pk), determination_type="WAIVED", rule=rule
        )
        at_waiver = self.clock.now
        before = self.query(at_waiver)
        self.clock.set(self.at(hours=8))
        record_obligation_determination(
            organization=self.org,
            obligation_id=str(row.pk),
            determination_type="REINSTATED",
            prior_determination=waiver,
        )
        self.assertEqual(self.query(at_waiver), before)
        self.assertEqual(
            next(r for r in before if r.obligation.id == str(row.pk)).state, State.WAIVED
        )
        self.assertEqual(
            next(r for r in self.query() if r.obligation.id == str(row.pk)).state, State.OVERDUE
        )

    def test_inclusive_recorded_boundary_and_mutable_active_flag_do_not_rewrite_history(self):
        event = ObligationSelection.objects.order_by("recorded_at").first()
        at = event.recorded_at
        expected = self.query(at)
        self.assertTrue(expected)
        self.assertFalse(self.query(at - timedelta(microseconds=1)))
        Vehicle.objects.filter(pk=self.vehicle.pk).update(active=False)
        self.assertEqual(self.query(at), expected)

    def test_scope_bounds_limits_and_bad_adapter_payloads_fail_explicitly(self):
        org = Organization.objects.create(name="Synthetic other", station_zone="UTC")
        with self.assertRaises(Vehicle.DoesNotExist):
            get_vehicle_obligations_as_of(
                str(self.vehicle.pk), self.clock.now, organization_id=str(org.pk)
            )
        for kw in (
            {"start": self.at(days=-40)},
            {"start": self.at(days=2), "end": self.at(days=1)},
        ):
            with self.assertRaises(ValueError):
                self.query(**kw)
        with mock.patch("cya.obligations.projections.MAX_ROWS", 1):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                self.query()
        with self.assertRaises(ValueError):
            self.query(check_lookup=lambda **kw: [object()])


class HistoryAssertions:
    """AC-26/AC-27 over a seeded history: live equals an independent oracle, replay equals both."""

    def reference_check(self, obligation, instant, fallback):
        """Use the existing chain reader plus stored links, independently of export/rebuild."""
        rows = list(
            CheckRecord.objects.filter(
                organization=self.org, vehicle=self.vehicle, item_id=obligation.item_id
            )
        )
        decisions = CheckEvent.objects.filter(
            organization=self.org, root_check_id__in={r.root_check_id for r in rows}
        )
        effective = effective_checks(
            [
                CheckSnapshot(
                    str(r.pk),
                    str(r.root_check_id),
                    str(r.predecessor_check_id) if r.predecessor_check_id else None,
                    r.recorded_at,
                    r.outcome,
                )
                for r in rows
            ],
            [
                CheckDecision(
                    str(e.root_check_id),
                    str(e.selected_check_id),
                    e.event_type,
                    e.recorded_at,
                    str(e.replacement_check_id) if e.replacement_check_id else None,
                )
                for e in decisions
            ],
            as_of=instant,
        )
        by_id = {str(r.pk): r for r in rows}
        linked = []
        for e in effective:
            if e.invalidated:
                continue
            row = by_id[e.check_id]
            root = by_id[e.root_id]
            if root.recorded_at < obligation.early_at:
                continue
            if row.obligation_links.filter(
                obligation=obligation, recorded_at__lte=instant
            ).exists():
                linked.append((root.recorded_at, e.check_id, e.outcome))
        for _at, _pk, outcome in sorted(linked):
            if outcome in ("PASSED", "FAILED"):
                return State.SATISFIED, outcome
        determination = (
            ObligationDetermination.objects.filter(
                obligation_id=obligation.pk, recorded_at__lte=instant
            )
            .order_by("-recorded_at", "-id")
            .first()
        )
        if determination and determination.determination_type == "SUPERVISOR_COMPLETED":
            for _at, pk, outcome in linked:
                if (
                    outcome == "UNABLE_TO_INSPECT"
                    and determination.supporting_checks.filter(
                        check_record_id=pk, recorded_at__lte=instant
                    ).exists()
                ):
                    return State.SATISFIED, "SUPERVISOR_COMPLETED"
        return fallback, None

    def assert_history(self, seed, **history):
        start, end = self.at(), self.at(days=17)
        observations = []
        org, vehicle = str(self.org.pk), str(self.vehicle.pk)
        for instant in generate_history(self, seed=seed, **history):
            live = get_vehicle_obligations_as_of(
                vehicle, instant, organization_id=org, start=start, end=end
            )
            # Independent oracle uses the established ORM selection reader and status reader,
            # not the new replay fold. Check-rich histories have a separate oracle below.
            reference = []
            for row in effective_obligations(org, start, end, vehicle_id=vehicle, as_of=instant):
                status = status_at(org, vehicle, min(row.due_at, instant))
                if status and status.status == "OUT_OF_SERVICE":
                    state = State.EXEMPT
                elif instant < row.early_at:
                    state = State.UPCOMING
                elif instant < row.due_at:
                    state = State.CURRENT
                elif instant <= row.grace_at:
                    state = State.DUE
                else:
                    state = State.OVERDUE
                outcome = None
                if history.get("checks") and state != State.EXEMPT:
                    state, outcome = self.reference_check(row, instant, state)
                reference.append((str(row.pk), str(row.requirement_version_id), state, outcome))
            self.assertEqual(
                [
                    (r.obligation.id, r.obligation.requirement_version_id, r.state, r.outcome)
                    for r in live
                ],
                reference,
                f"seed={seed}, instant={instant}",
            )
            if history.get("checks"):
                keys = {
                    (r.obligation.item_id, r.obligation.kit_instance_id or NO_KIT) for r in live
                }
                attention = item_attention(org, vehicle, keys, instant)
                for result in live:
                    self.assertEqual(
                        result.attention,
                        attention[
                            (result.obligation.item_id, result.obligation.kit_instance_id or NO_KIT)
                        ],
                    )
                # SPEC 19.5: independently compare every lifecycle cache, including cancelled
                # deficiencies that no longer contribute to attention.
                from cya.obligations.history import deficiency_states

                snapshot = load_projection_history(
                    vehicle, instant, organization_id=org, start=start, end=end
                )
                recomputed = deficiency_states(
                    snapshot.deficiency_headers, snapshot.deficiency_events, instant
                )
                self.assertEqual(
                    {d.deficiency_id: d.status for d in recomputed},
                    dict(
                        DeficiencyProjection.objects.filter(organization_id=org).values_list(
                            "deficiency_id", "status"
                        )
                    ),
                )
            observations.append((instant, live))
        if history.get("checks"):
            outcomes = {r.outcome for _instant, states in observations for r in states}
            self.assertTrue({"PASSED", "FAILED", "SUPERVISOR_COMPLETED"} <= outcomes, outcomes)
            self.assertTrue(
                any(r.self_completed for _instant, states in observations for r in states)
            )
        history = load_projection_history(
            vehicle, self.clock.now, organization_id=org, start=start, end=end
        )
        rng = Random(seed)

        def shuffled(rows):
            return tuple(rng.sample(list(rows), len(rows)))

        rebuilt = replace(
            history,
            obligations=shuffled(history.obligations),
            selections=shuffled(history.selections),
            determinations=shuffled(history.determinations),
            statuses=shuffled(history.statuses),
            checks=shuffled(history.checks),
            check_records=shuffled(history.check_records),
            check_decisions=shuffled(history.check_decisions),
            check_links=shuffled(history.check_links),
            check_supports=shuffled(history.check_supports),
            deficiency_headers=shuffled(history.deficiency_headers),
            deficiency_events=shuffled(history.deficiency_events),
        )
        for instant, expected in observations:
            with self.subTest(seed=seed, instant=instant):
                self.assertEqual(
                    get_vehicle_obligations_as_of(
                        vehicle, instant, organization_id=org, start=start, end=end
                    ),
                    expected,
                )
                with self.assertNumQueries(0):
                    self.assertEqual(
                        rebuild_vehicle_projection(rebuilt, vehicle, instant, start, end), expected
                    )


class RandomizedHistoryTests(HistoryAssertions, ObligationCase):
    def test_seeded_weeks_replay_matches_live_and_past_answers_survive_later_changes(self):
        self.assert_history(6027)

    def test_another_seed_rebuilds_the_same_historical_states(self):
        self.assert_history(26060)


class CheckHistoryTests(ObligationLinkCleanupMixin, HistoryAssertions, KitCase):
    def test_seeded_checks_deficiencies_and_kits_replay_without_database(self):
        self.assert_history(8701, checks=True, kits=True)

    def test_second_seed_recomputes_deficiency_status_and_attention(self):
        self.assert_history(8702, checks=True, kits=True)
