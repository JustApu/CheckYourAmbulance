"""Synthetic M2 exit scenario driver; real audited commands and local stubs only.

Runs only in a committing test database. The clock is supplied by the scenario
binding; no fixture, external provider, or operational data is modified.
"""

import io
import socket
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from random import Random
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.db import DatabaseError, transaction

from cya.accounts.models import Organization, User
from cya.authorization.roles import grant_initial_crew
from cya.checklists.commands import publish_template_version, save_template_draft
from cya.checklists.models import ChecklistItemDef, ChecklistPublication, Compartment, Item, Vehicle
from cya.checklists.seed import seed_fleet
from cya.conditions.evaluator import evaluate_carry_over_rule, record_obligation_determination
from cya.conditions.models import ObligationDetermination
from cya.conditions.sync import create_conditional_rule, ingest_roster_snapshot
from cya.core.models import AuditEvent, CommandReceipt
from cya.integrations import RosterShift, RosterUnavailable
from cya.obligations.commands import bind_requirement
from cya.obligations.generation import run_generation
from cya.obligations.models import Obligation, ObligationSelection, OdometerObligation
from cya.obligations.projections import get_vehicle_obligations_as_of, load_projection_history
from cya.obligations.queries import effective_obligations, odometer_obligations
from cya.obligations.state import CheckEvidence, State, rebuild_vehicle_projection
from cya.operations.commands import set_vehicle_status
from cya.operations.status import out_of_service_intervals
from cya.schedules.commands import publish_rule, seed_default_schedules
from cya.schedules.engine import Rule
from cya.schedules.models import ScheduleRule
from cya.testing.network import NetworkAccessBlocked
from cya.testing.role_http import expect
from cya.testing.stubs import StubPriorCheckLookup, StubRosterProvider


class ScenarioClock:
    def __init__(self, instant):
        self.instant = instant

    def __call__(self):
        self.instant += timedelta(microseconds=1)
        return self.instant

    def set(self, instant):
        if instant < self.instant:
            raise ValueError("scenario clock cannot run backwards")
        self.instant = instant


class ObligationEngineScenario:
    def __init__(self, clock, fixture):
        self.clock, self.fixture = clock, fixture
        self.zone = ZoneInfo(fixture["station_zone"])
        self.start_date = date.fromisoformat(fixture["start_date"])
        self.start, self.end = self.at(0), self.at(7) - timedelta(microseconds=1)
        self.snapshots = []
        self.results = {}

    def at(self, day, hour=0, minute=0):
        local = datetime.combine(
            self.start_date + timedelta(days=day), time(hour, minute), self.zone
        )
        return local.astimezone(UTC)

    def context(self):
        return {
            "organization_id": str(self.org.pk),
            "actor_id": str(self.admin.pk),
            "operation_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "request_attempt_id": str(uuid4()),
        }

    def setup(self):
        self.org = Organization.objects.create(
            name="Synthetic sample EMS organization", station_zone=str(self.zone)
        )
        self.admin = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="synthetic-m2-administrator",
            email="m2-admin@example.test",
            display_name="Synthetic M2 administrator",
        )
        with transaction.atomic():
            grant_initial_crew(user=self.admin, command_id=str(uuid4()))
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=self.admin.pk,
            stdout=io.StringIO(),
        )
        plan, _ = seed_fleet(organization_id=str(self.org.pk), actor_id=str(self.admin.pk))
        self.template_id = plan.template_id
        self.vehicles = list(Vehicle.objects.filter(organization=self.org).order_by("number"))
        expect(
            [v.display_name for v in self.vehicles],
            ["Ambulance 1", "Ambulance 2", "Ambulance 3", "Ambulance 4"],
            "fleet",
        )
        self.glucometer = Item.objects.create(
            organization=self.org, label=self.fixture["glucometer_label"]
        )
        self.v1 = self.publish(self.definitions())
        self.rule = create_conditional_rule(
            organization=self.org,
            rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
            typed_inputs={"item_id": str(self.glucometer.pk)},
            effective_at=self.clock.instant,
            actor_user=self.admin,
        )
        seed_default_schedules(str(self.org.pk), self.clock.instant)
        expect(
            ScheduleRule.objects.filter(organization=self.org).count(), 6, "default schedule count"
        )
        expected_slots = [
            ["first-due-0600", "first-due-1800"],
            ["second-due-weekday", "second-due-weekend"],
            ["third-due-1800"],
            ["fourth-due-1800"],
        ]
        for vehicle, slots in zip(self.vehicles, expected_slots, strict=True):
            for slot in slots:
                bind_requirement(
                    vehicle_id=str(vehicle.pk),
                    template_id=self.template_id,
                    slot=slot,
                    **self.context(),
                )
        publish_rule(
            slot=self.fixture["fold_slot"],
            rule=Rule(str(self.zone), time(1, 30), self.clock.instant),
            **self.context(),
        )
        bind_requirement(
            vehicle_id=str(self.vehicles[0].pk),
            template_id=self.template_id,
            slot=self.fixture["fold_slot"],
            **self.context(),
        )
        self.assert_freeze()

    def definitions(self, remove_warning=False):
        """Copy the seed's published definition fields; no production configuration is read."""
        publication = (
            ChecklistPublication.objects.filter(
                organization=self.org,
                checklist_version__template_id=self.template_id,
            )
            .order_by("-recorded_at")
            .first()
        )
        rows = ChecklistItemDef.objects.filter(
            checklist_version_id=publication.checklist_version_id
        ).order_by("display_order")
        definitions = []
        for row in rows:
            if remove_warning and row.kind == "PRESENCE_CONDITION" and row.system_id:
                self.removed_item_id = str(row.item_id)
                continue
            spec = {
                "item_id": str(row.item_id),
                "display_order": row.display_order,
                "kind": row.kind,
                "instructions": row.instructions,
                "allowed_outcomes": row.allowed_outcomes,
            }
            for field in ("expected_quantity", "unit", "compartment_id", "system_id"):
                value = getattr(row, field)
                if value is not None:
                    spec[field] = value
            definitions.append(spec)
        if not any(d["item_id"] == str(self.glucometer.pk) for d in definitions):
            compartment = (
                Compartment.objects.filter(organization=self.org, vehicle=self.vehicles[0])
                .order_by("id")
                .first()
            )
            definitions.append(
                {
                    "item_id": str(self.glucometer.pk),
                    "display_order": 70,
                    "kind": "PRESENCE_CONDITION",
                    "compartment_id": str(compartment.pk),
                    "instructions": "Synthetic: inspect glucometer.",
                    "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
                }
            )
        return definitions

    def publish(self, definitions):
        draft = save_template_draft(
            template_id=self.template_id, items=definitions, **self.context()
        )
        version_id = draft.body["version_id"]
        publish_template_version(version_id=version_id, **self.context())
        return version_id

    def assert_freeze(self):
        row = ChecklistItemDef.objects.filter(checklist_version_id=self.v1).first()
        try:
            with transaction.atomic():
                ChecklistItemDef.objects.filter(pk=row.pk).update(
                    instructions="Synthetic forbidden edit"
                )
        except DatabaseError as exc:
            expect("published" in str(exc).lower(), True, "freeze guard refusal")
        else:
            raise AssertionError("published definition update was accepted")
        row.refresh_from_db()
        expect(
            row.instructions == "Synthetic forbidden edit", False, "published contents unchanged"
        )

    def generate(self):
        result = run_generation(str(self.org.pk))
        expect(result.failures, [], "generation failures")
        before = self.counts()
        replay = run_generation(str(self.org.pk))
        expect(replay.failures, [], "repeat generation failures")
        expect(self.counts(), before, "idempotent row and audit counts")

    def counts(self):
        return tuple(
            model.objects.count()
            for model in (
                Obligation,
                ObligationSelection,
                OdometerObligation,
                CommandReceipt,
                AuditEvent,
            )
        )

    def rows(self, vehicle, instant=None):
        return get_vehicle_obligations_as_of(
            str(vehicle.pk),
            instant or self.clock.instant,
            organization_id=str(self.org.pk),
            start=self.start,
            end=self.end,
        )

    def capture(self, label):
        instant = self.clock.instant
        states = tuple(self.rows(vehicle, instant) for vehicle in self.vehicles)
        self.snapshots.append((label, instant, states))
        return states

    def run_week(self):
        for day in range(self.fixture["days"]):
            self.clock.set(self.at(day, 0, 30))
            self.generate()
            if day == 0:
                self.clock.set(self.at(0, 5, 30))
                states = self.capture("T1")
                expect(
                    {r.state for r in states[0] if r.obligation.due_at == self.at(0, 6)},
                    {State.CURRENT},
                    "T1 known current states",
                )
                self.assert_boundaries()
            elif day == 3:
                self.clock.set(self.at(3, 5, 30))
                self.update_template()
                self.capture("T2")
            elif day == 4:
                self.clock.set(self.at(4, 4, 30))
                set_vehicle_status(
                    vehicle_id=str(self.vehicles[1].pk),
                    status="OUT_OF_SERVICE",
                    explanation="Synthetic maintenance outage.",
                    **self.context(),
                )
                self.out_at = self.clock.instant
                self.generate()
            elif day == 5:
                self.carry_over(day, 6, "unchanged")
                self.capture("T3")
                self.carry_over(day, 18, "changed")
            elif day == 6:
                self.carry_over(day, 6, "unavailable")
                self.clock.set(self.at(6, 14))
                self.assert_outage_before_return()
                set_vehicle_status(
                    vehicle_id=str(self.vehicles[1].pk),
                    status="IN_SERVICE",
                    explanation="Synthetic maintenance complete.",
                    **self.context(),
                )
                self.back_at = self.clock.instant
                self.generate()
                self.carry_over(day, 18, "missing-check")
        self.clock.set(self.end)
        self.generate()
        self.verify()

    def update_template(self):
        first = self.vehicles[0]
        due_open, due_future = self.at(3, 6), self.at(3, 18)
        before = self.rows(first)
        open_ids = {r.obligation.id for r in before if r.obligation.due_at == due_open}
        future_ids = {r.obligation.id for r in before if r.obligation.due_at == due_future}
        expect((len(open_ids), len(future_ids)), (7, 7), "pre-publication obligation counts")
        self.v2 = self.publish(self.definitions(remove_warning=True))
        after = self.rows(first)
        expect(
            {r.obligation.id for r in after if r.obligation.due_at == due_open},
            open_ids,
            "open obligations pinned",
        )
        future = [r for r in after if r.obligation.due_at == due_future]
        expect(len(future), 6, "pre-window removed item retired")
        expect(
            self.removed_item_id in {r.obligation.item_id for r in future},
            False,
            "removed item absent from replacement inspection",
        )
        expect({r.state for r in future}, {State.UPCOMING}, "T2 future inspection upcoming")
        expect(
            {r.obligation.publication_id for r in future},
            {
                str(
                    ChecklistPublication.objects.get(
                        organization=self.org, checklist_version_id=self.v2
                    ).pk
                )
            },
            "T2 projection uses the new publication",
        )
        expect(
            future_ids & {r.obligation.id for r in future}, set(), "pre-window obligations replaced"
        )
        expect(
            {
                str(o.requirement_version.publication.checklist_version_id)
                for o in effective_obligations(
                    str(self.org.pk),
                    due_future,
                    due_future,
                    vehicle_id=str(first.pk),
                )
            },
            {self.v2},
            "replacement requirement version",
        )
        self.archived_ids = future_ids

    def carry_over(self, day, hour, mode):
        due = self.at(day, hour)
        self.clock.set(due)
        vehicle = self.vehicles[0]
        row = next(
            r.obligation
            for r in self.rows(vehicle)
            if r.obligation.due_at == due and r.obligation.item_id == str(self.glucometer.pk)
        )
        prior_start = due - timedelta(hours=6)
        prior = self.fixture["unchanged_crew"]
        current = self.fixture["changed_crew"] if mode == "changed" else prior
        provider = StubRosterProvider(
            [
                RosterShift("synthetic-prior", tuple(prior), prior_start, due),
                RosterShift("synthetic-current", tuple(current), due, due + timedelta(hours=6)),
            ],
            available=mode != "unavailable",
        )
        lookup = StubPriorCheckLookup()
        if mode != "missing-check":
            lookup.add_check(
                check_id=str(uuid4()),
                organization_id=str(self.org.pk),
                vehicle_id=str(vehicle.pk),
                item_id=str(self.glucometer.pk),
                checked_at=due - timedelta(minutes=30),
            )
        try:
            shifts = provider.shifts_overlapping(prior_start, due + timedelta(hours=6))
        except RosterUnavailable:
            shifts = None
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="synthetic-humanity",
            shifts=shifts,
            fetched_at=self.clock.instant,
            vehicle_id=str(vehicle.pk),
            is_available=shifts is not None,
            interval_start=prior_start,
            interval_end=due + timedelta(hours=6),
            prior_check_lookup=lookup,
        )
        evaluation = evaluate_carry_over_rule(
            self.org,
            str(vehicle.pk),
            str(self.glucometer.pk),
            due,
            self.rule,
            prior_check_lookup=lookup,
            early_at=row.early_at,
            grace_at=row.grace_at,
            evaluation_time=self.clock.instant,
        )
        expect(evaluation.waived, mode == "unchanged", f"carry-over {mode}")
        if evaluation.waived:
            record_obligation_determination(
                organization=self.org,
                obligation_id=row.id,
                vehicle_id=str(vehicle.pk),
                due_at=due,
                determination_type="WAIVED",
                rule=self.rule,
                evaluated_inputs=evaluation.evaluated_inputs,
            )
        else:
            expect(
                ObligationDetermination.objects.filter(
                    obligation_id=row.id, determination_type="WAIVED"
                ).exists(),
                False,
                "no fail-closed waiver",
            )
        state = next(r.state for r in self.rows(vehicle) if r.obligation.id == row.id)
        expect(
            state, State.WAIVED if mode == "unchanged" else State.DUE, f"derived carry-over {mode}"
        )
        self.results[mode] = state

    def assert_outage_before_return(self):
        second = self.vehicles[1]
        for day, hour in [(4, 12), (5, 9)]:
            states = [
                r.state for r in self.rows(second) if r.obligation.due_at == self.at(day, hour)
            ]
            expect(set(states), {State.EXEMPT}, "pre-created inspection exempt at D")
        self.outage_missing_due = self.at(6, 9)
        expect(
            Obligation.objects.filter(vehicle=second, due_at=self.outage_missing_due).exists(),
            False,
            "no generation during outage",
        )

    def assert_boundaries(self):
        first = self.vehicles[0]
        row = next(r.obligation for r in self.rows(first) if r.obligation.due_at == self.at(0, 6))
        for instant, expected in [
            (row.early_at - timedelta(microseconds=1), State.UPCOMING),
            (row.early_at, State.CURRENT),
            (row.due_at, State.DUE),
            (row.grace_at, State.DUE),
            (row.grace_at + timedelta(microseconds=1), State.OVERDUE),
        ]:
            state = next(r.state for r in self.rows(first, instant) if r.obligation.id == row.id)
            expect(state, expected, "integrated window boundary")
        check = CheckEvidence(
            str(uuid4()),
            str(self.org.pk),
            str(first.pk),
            row.item_id,
            row.id,
            self.at(0, 5, 15),
            "PASSED",
        )
        states = get_vehicle_obligations_as_of(
            str(first.pk),
            self.at(0, 7, 1),
            organization_id=str(self.org.pk),
            start=self.start,
            end=self.end,
            check_lookup=lambda **kw: [check],
        )
        expect(
            next(r.state for r in states if r.obligation.id == row.id),
            State.SATISFIED,
            "early on-time linked check stub",
        )

    def verify(self):
        first, second = self.vehicles[:2]
        first_dues = {r.obligation.due_at for r in self.rows(first)}
        expect(
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC) in first_dues,
            True,
            "fall fold first occurrence",
        )
        expect(
            datetime(2026, 11, 1, 6, 30, tzinfo=UTC) in first_dues,
            False,
            "no second fold occurrence",
        )
        expect(datetime(2026, 10, 31, 10, tzinfo=UTC) in first_dues, True, "06:00 EDT due")
        expect(datetime(2026, 11, 1, 11, tzinfo=UTC) in first_dues, True, "06:00 EST due")
        expect(
            Obligation.objects.filter(vehicle=second, due_at=self.outage_missing_due).exists(),
            False,
            "return does not backfill",
        )
        future = effective_obligations(
            str(self.org.pk), self.at(7, 9), self.at(7, 9), vehicle_id=str(second.pk)
        )
        expect(len(future), 6, "future routine resumes")
        spans = out_of_service_intervals(
            str(self.org.pk), str(second.pk), self.start, self.clock.instant
        )
        expect(
            [(s.start, s.end) for s in spans],
            [(self.out_at, self.back_at)],
            "audited outage interval",
        )
        self.assert_no_phantoms()
        self.assert_history()
        try:
            socket.getaddrinfo("m2-no-network.example.invalid", 443)
        except NetworkAccessBlocked:
            pass
        else:
            raise AssertionError("external DNS was not blocked by the scenario guard")

    def assert_no_phantoms(self):
        effective_ids = set()
        inspection_counts = []
        for index, vehicle in enumerate(self.vehicles):
            rows = self.rows(vehicle)
            anchors = [(r.obligation.item_id, r.obligation.due_at) for r in rows]
            expect(len(anchors), len(set(anchors)), "one effective obligation per anchor")
            effective_ids.update(r.obligation.id for r in rows)
            dues = {r.obligation.due_at for r in rows}
            expected_dues = set()
            for day in range(7):
                if index == 0:
                    expected_dues.update([self.at(day, 1, 30), self.at(day, 6), self.at(day, 18)])
                elif index == 1:
                    if day != 6:  # Tuesday's inspection was never created during the outage.
                        weekend = (self.start_date + timedelta(days=day)).weekday() >= 5
                        expected_dues.add(self.at(day, 12 if weekend else 9))
                else:
                    expected_dues.add(self.at(day, 18))
            expect(dues, expected_dues, "seven-day independent wall-calendar cadence")
            inspection_counts.append(len(dues))
            odometers = odometer_obligations(
                str(self.org.pk), self.start, self.end, vehicle_id=str(vehicle.pk)
            )
            expect(len(odometers), len(dues), "one effective odometer per inspection")
        expect(
            effective_ids & self.archived_ids, set(), "archived requirements never phantom overdues"
        )
        expect(inspection_counts, [21, 6, 7, 7], "seven-day inspection totals")
        expect(len(effective_ids), 266, "seven-day item obligation total after reconciliation")
        tails = {}
        for event in ObligationSelection.objects.filter(
            organization=self.org,
            due_at__gte=self.start,
            due_at__lte=self.end,
        ).order_by("vehicle_id", "item_id", "due_at", "-sequence"):
            tails.setdefault((event.vehicle_id, event.item_id, event.due_at), event)
        selected = {
            str(e.selected_obligation_id) for e in tails.values() if e.selected_obligation_id
        }
        expect(
            effective_ids, selected, "projection contains exactly the authoritative selected rows"
        )
        expect(
            all(
                r.state == State.OVERDUE
                for r in self.rows(self.vehicles[2])
                if r.obligation.due_at < self.at(6, 12)
            ),
            True,
            "ordinary uninspected obligations really become overdue",
        )

    def assert_history(self):
        expect(
            [label for label, _, _ in self.snapshots],
            ["T1", "T2", "T3"],
            "three distinct historical observations",
        )
        rng = Random(61)
        for index, vehicle in enumerate(self.vehicles):
            history = load_projection_history(
                str(vehicle.pk),
                self.clock.instant,
                organization_id=str(self.org.pk),
                start=self.start,
                end=self.end,
            )
            history = replace(
                history, selections=tuple(rng.sample(history.selections, len(history.selections)))
            )
            for label, instant, expected in self.snapshots:
                expect(
                    self.rows(vehicle, instant), expected[index], f"{label} historical API equality"
                )
                expect(
                    rebuild_vehicle_projection(
                        history, str(vehicle.pk), instant, self.start, self.end
                    ),
                    expected[index],
                    f"{label} event replay equality",
                )
