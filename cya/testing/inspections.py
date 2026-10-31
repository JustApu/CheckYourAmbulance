"""Command-backed M3 exit journey using synthetic data and real committing transactions.

This driver runs only through the scenario test runner, never against runtime data.
External networking is blocked by both the binding and OfflineTestRunner.
"""

import io
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import time, timedelta
from random import Random
from threading import Barrier, Lock
from uuid import uuid4

from asgiref.sync import async_to_sync
from channels.layers import InMemoryChannelLayer
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction

from cya.accounts.models import Organization, User
from cya.attention.checklist import vehicle_checklist
from cya.authorization.commands import change_role
from cya.authorization.roles import grant_initial_crew
from cya.checklists.commands import publish_template_version, save_template_draft
from cya.checklists.models import ChecklistTemplate, Compartment, Item, Vehicle
from cya.checks.commands import CheckError, correct_check, invalidate_chain, record_check
from cya.checks.conflicts import CheckConflict
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.conditions.uti import complete_unable_obligation
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies.models import DeficiencyCategory, DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.severity import ensure_default_severities
from cya.inspections.commands import (
    auto_close_inactive_sessions,
    end_session,
    open_or_join_session,
    reopen_session,
)
from cya.inspections.models import InspectionSession, SessionEvent
from cya.inspections.projections import current_state, read_session
from cya.kits.commands import add_kit_instance, record_kit_assignment
from cya.obligations.commands import bind_requirement
from cya.obligations.generation import run_generation
from cya.obligations.history import replay_checks
from cya.obligations.models import Obligation
from cya.obligations.projections import get_vehicle_obligations_as_of, load_projection_history
from cya.obligations.state import State, rebuild_vehicle_projection
from cya.odometer.commands import record_odometer
from cya.odometer.models import EngineHoursEntry, OdometerEntry
from cya.odometer.state import get_vehicle_odometer_states_as_of
from cya.schedules.commands import publish_rule
from cya.schedules.engine import Rule
from cya.testing.network import NetworkAccessBlocked
from cya.testing.role_http import expect


class InspectionClock:
    """Thread-safe, strictly increasing server instants for concurrent command writers."""

    def __init__(self, instant):
        self._now = instant
        self._lock = Lock()

    @property
    def now(self):
        with self._lock:
            return self._now

    def set(self, instant):
        with self._lock:
            if instant < self._now:
                raise ValueError("scenario clock cannot run backwards")
            self._now = instant

    def __call__(self):
        with self._lock:
            self._now += timedelta(microseconds=1)
            return self._now


class InspectionScenario:
    def __init__(self, clock, fixture):
        self.clock, self.fixture = clock, fixture
        self.start = clock.now
        self.end = self.start + timedelta(days=2)
        self.snapshots = []

    def context(self, actor=None, operation_id=None):
        return {
            "organization_id": str(self.org.pk),
            "actor_id": str((actor or self.admin).pk),
            "operation_id": operation_id or str(uuid4()),
            "correlation_id": str(uuid4()),
            "request_attempt_id": str(uuid4()),
        }

    def user(self, name):
        user = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject=f"synthetic-m3-{name}",
            email=f"m3-{name}@example.test",
            display_name=f"Synthetic M3 {name}",
        )
        with transaction.atomic():
            grant_initial_crew(user=user, command_id=str(uuid4()))
        return user

    def setup(self):
        self.org = Organization.objects.create(
            name=self.fixture["organization"], station_zone="UTC"
        )
        self.admin = self.user("administrator")
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=self.admin.pk,
            stdout=io.StringIO(),
        )
        self.crew = [self.user(name) for name in self.fixture["crew"]]
        self.supervisor = self.user("supervisor")
        change_role(
            target_id=str(self.supervisor.pk),
            role="SUPERVISOR",
            action="GRANT",
            **self.context(),
        )
        self.vehicle = Vehicle.objects.create(organization=self.org, display_name="Synthetic M3 A")
        self.empty_vehicle = Vehicle.objects.create(
            organization=self.org, display_name="Synthetic M3 zero-kit control"
        )
        self.compartment = Compartment.objects.create(
            organization=self.org,
            vehicle=self.vehicle,
            stable_slot_id=str(uuid4()),
            revision=1,
            label="Synthetic equipment cabinet",
            effective_at=self.start,
        )
        Compartment.objects.create(
            organization=self.org,
            vehicle=self.empty_vehicle,
            stable_slot_id=self.compartment.stable_slot_id,
            revision=1,
            label="Synthetic empty control cabinet",
            effective_at=self.start,
        )
        self.template = ChecklistTemplate.objects.create(
            organization=self.org, label="Synthetic M3 inspection"
        )
        self.items = {
            name: Item.objects.create(organization=self.org, label=f"Synthetic {name}")
            for name in self.fixture["items"]
        }
        self.kit_a = self.add_kit("A")
        self.kit_b = self.add_kit("B")
        definitions = [
            {
                "item_id": str(item.pk),
                "display_order": index * 10,
                "kind": "PRESENCE_CONDITION",
                "instructions": "Synthetic integration check.",
                "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
                "compartment_id": str(self.compartment.pk),
                **({"kit_id": self.kit_a} if name == "kit contents" else {}),
            }
            for index, (name, item) in enumerate(self.items.items(), 1)
        ]
        self.version = save_template_draft(
            template_id=str(self.template.pk), items=definitions, **self.context()
        ).body["version_id"]
        publish_template_version(version_id=self.version, **self.context())
        # Overlap is deliberate: at 05:45 both windows are open, earliest due is 06:00.
        for slot, minute in (("m3-0600", 0), ("m3-0630", 30)):
            publish_rule(slot=slot, rule=Rule("UTC", time(6, minute), self.start), **self.context())
            for vehicle in (self.vehicle, self.empty_vehicle):
                bind_requirement(
                    vehicle_id=str(vehicle.pk),
                    template_id=str(self.template.pk),
                    slot=slot,
                    **self.context(),
                )
        expect(run_generation(str(self.org.pk)).failures, [], "M3 generation")
        ensure_default_severities(self.org.pk, self.start)
        self.category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=str(uuid4()),
            revision=1,
            label="Synthetic M3 defect",
            supply_related=False,
            effective_at=self.start,
        )
        self.clock.set(self.start + timedelta(hours=5, minutes=45))

    def add_kit(self, label):
        return add_kit_instance(
            label=f"Synthetic identical kit {label}",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.compartment.pk),
            **self.context(self.supervisor),
        ).body["kit_id"]

    def defect(self):
        return {
            "create": {
                "category_id": str(self.category.stable_category_id),
                "severity": "HIGH",
                "summary": "Synthetic M3 defect",
                "description": "Synthetic integration evidence.",
            }
        }

    def record(self, name, actor=None, operation_id=None, **kw):
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.items[name].pk),
            checklist_version_id=self.version,
            outcome=kw.pop("outcome", "PASSED"),
            **kw,
            **self.context(actor or self.crew[0], operation_id),
        )

    def rows(self, instant=None, vehicle=None):
        return get_vehicle_obligations_as_of(
            str((vehicle or self.vehicle).pk),
            instant or self.clock.now,
            organization_id=str(self.org.pk),
            start=self.start,
            end=self.end,
        )

    def item_rows(self, name, kit=None, instant=None):
        return [
            r
            for r in self.rows(instant)
            if r.obligation.item_id == str(self.items[name].pk)
            and r.obligation.kit_instance_id == kit
        ]

    def earliest(self, name, kit=None):
        return min(self.item_rows(name, kit), key=lambda r: r.obligation.due_at)

    def checklist(self, instant=None):
        return vehicle_checklist(
            str(self.org.pk), str(self.vehicle.pk), as_of=instant or self.clock.now
        )

    def red(self, name):
        return next(
            r.attention.red for r in self.checklist().rows if r.item_id == str(self.items[name].pk)
        )

    def capture(self, label):
        instant = self.clock.now
        self.snapshots.append(
            (
                label,
                instant,
                self.rows(instant),
                self.checklist(instant),
                self.odometer_states(instant),
            )
        )

    def odometer_states(self, instant=None):
        return get_vehicle_odometer_states_as_of(
            str(self.vehicle.pk),
            instant or self.clock.now,
            organization_id=str(self.org.pk),
            start=self.start,
            end=self.end,
        )

    @staticmethod
    def refused(error_type, call, label, code=None):
        try:
            call()
        except error_type as error:
            if code is not None:
                expect(error.code, code, label)
            return error
        raise AssertionError(f"{label}: accepted a forbidden operation")

    @staticmethod
    def concurrent(jobs):
        """Release all writers together; close each thread's own database connection."""
        barrier = Barrier(len(jobs))

        def run(job):
            try:
                barrier.wait(timeout=15)
                return job()
            except CheckConflict as error:
                return error
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            return [f.result(timeout=60) for f in futures]

    def open(self, actor, **kw):
        return open_or_join_session(
            vehicle_id=str(self.vehicle.pk),
            template_id=str(self.template.pk),
            **kw,
            **self.context(actor),
        ).body["session_id"]

    def crew_checks(self):
        self.session = self.open(self.crew[0])
        expect(self.open(self.crew[1]), self.session, "second crew joins")
        expect(self.open(self.crew[2]), self.session, "third crew joins")
        names = ["suction", "oxygen", "lighting"]
        results = self.concurrent(
            [
                lambda name=name, user=user: self.record(name, user, session_id=self.session)
                for name, user in zip(names, self.crew, strict=True)
            ]
        )
        for name, user, result in zip(names, self.crew, results, strict=True):
            expect(result.status, 201, "concurrent distinct item committed")
            check = CheckRecord.objects.get(pk=result.body["check_id"])
            expect(check.actor_user_id, user.pk, f"{name} individual attribution")
            expect(self.earliest(name).state, State.SATISFIED, f"{name} compliance while open")
        expect(self.earliest("unchecked").state, State.CURRENT, "unchecked before close")
        before = self.rows()
        end_session(session_id=self.session, **self.context(self.crew[0]))
        expect(self.rows(), before, "explicit close cannot change compliance")
        expect(
            current_state(InspectionSession.objects.get(pk=self.session)).state,
            "CLOSED",
            "closed despite unchecked item",
        )
        reopen_session(session_id=self.session, **self.context(self.crew[0]))
        expect(self.rows(), before, "reopening cannot change compliance")
        self.capture("T1")

    def replay_and_race(self):
        operation = str(uuid4())
        lost = self.record("replay", operation_id=operation, session_id=self.session)
        later = self.record("replay", self.crew[1], basis_check_id=lost.body["check_id"])
        count = CheckRecord.objects.count()
        replay = self.record("replay", operation_id=operation, session_id=self.session)
        expect(
            (replay.body, replay.status, replay.replayed),
            (lost.body, lost.status, True),
            "lost-response replay after newer check",
        )
        expect(CheckRecord.objects.count(), count, "one record per operation")
        # A warning consumes neither a check nor its idempotency key.
        warning_op = str(uuid4())

        def warn():
            return self.record("replay", operation_id=warning_op)

        for _ in range(2):
            error = self.refused(CheckConflict, warn, "repeat warning")
            expect(error.newest["check_id"], later.body["check_id"], "warning names newer check")
        expect(
            CommandReceipt.objects.filter(operation_id=warning_op).exists(),
            False,
            "warning leaves operation reusable",
        )
        intervening = self.record("replay", self.crew[2], basis_check_id=later.body["check_id"])
        stale = self.refused(
            CheckConflict,
            lambda: self.record("replay", acknowledges_check_id=later.body["check_id"]),
            "stale acknowledgement",
        )
        expect(stale.newest["check_id"], intervening.body["check_id"], "fresh warning")
        proceeded = self.record("replay", acknowledges_check_id=intervening.body["check_id"])
        expect(proceeded.status, 201, "new decision proceeds")
        expect(CheckRecord.objects.count(), count + 2, "warnings wrote no checks")
        self.pass_fail_race()

    def pass_fail_race(self):
        # Real concurrent Pass/Fail writers: either may win; the loser must acknowledge.
        outcomes = self.concurrent(
            [
                lambda: self.record("race", self.crew[0]),
                lambda: self.record(
                    "race", self.crew[1], outcome="FAILED", deficiency=self.defect()
                ),
            ]
        )
        warnings = [(i, r) for i, r in enumerate(outcomes) if isinstance(r, CheckConflict)]
        expect(len(warnings), 1, "exactly one concurrent writer warned")
        index, warning = warnings[0]
        winner = outcomes[1 - index]
        winner_check = CheckRecord.objects.get(pk=winner.body["check_id"])
        expect(
            (warning.status, warning.newest["outcome"], warning.newest["server_recorded_at"]),
            (409, winner_check.outcome, winner_check.recorded_at),
            "warning includes committed outcome and server instant",
        )
        expect(
            warning.newest["check_id"], winner.body["check_id"], "race warning identifies winner"
        )
        expect(
            warning.newest["actor"]["user_id"], str(self.crew[1 - index].pk), "warning attribution"
        )
        args = {"outcome": "FAILED", "deficiency": self.defect()} if index == 1 else {}
        outcomes[index] = self.record(
            "race", self.crew[index], acknowledges_check_id=winner.body["check_id"], **args
        )
        expect(
            {CheckRecord.objects.get(pk=r.body["check_id"]).outcome for r in outcomes},
            {"PASSED", "FAILED"},
            "both independent race results retained",
        )
        # A later independent Pass must never hide the unresolved defect, whichever racer won.
        newest = CheckRecord.objects.filter(item=self.items["race"]).latest("recorded_at")
        self.record("race", self.crew[2], basis_check_id=str(newest.pk))
        expect(self.red("race"), True, "later Pass leaves race item red")

    def correction_and_invalidation(self):
        failed = self.record("correction", outcome="FAILED", deficiency=self.defect())
        root = failed.body["check_id"]
        deficiency = failed.body["deficiency_ids"][0]
        self.refused(
            CheckError,
            lambda: correct_check(
                check_id=root,
                expected_tip_check_id=root,
                outcome="PASSED",
                **self.context(self.supervisor),
            ),
            "supervisor cannot correct another author",
            "AUTHOR_REQUIRED",
        )
        independent = self.record("correction", self.supervisor, basis_check_id=root)
        expect(
            CheckRecord.objects.get(pk=independent.body["check_id"]).root_check_id,
            independent.body["check_id"],
            "supervisor records independent root",
        )
        expect(
            DeficiencyProjection.objects.get(deficiency_id=deficiency).status,
            "OPEN",
            "fresh Pass is not a correction or repair",
        )
        corrected = correct_check(
            check_id=root,
            expected_tip_check_id=root,
            outcome="PASSED",
            **self.context(self.crew[0]),
        )
        expect(
            DeficiencyProjection.objects.get(deficiency_id=deficiency).status,
            "REPORTED_IN_ERROR",
            "author correct-entry reports accidental failure in error",
        )
        expect(CheckRecord.objects.get(pk=root).outcome, "FAILED", "original failure retained")
        self.corrected_root = root
        self.corrected_tip = corrected.body["check_id"]
        self.corrected_deficiency = deficiency
        # A second effective failed chain prevents automatic cancellation (COR-4).
        shared = self.record("supported defect", outcome="FAILED", deficiency=self.defect())
        shared_id = shared.body["deficiency_ids"][0]
        other = self.record(
            "supported defect",
            self.crew[1],
            outcome="FAILED",
            basis_check_id=shared.body["check_id"],
            deficiency={"existing_id": shared_id},
        )
        correct_check(
            check_id=shared.body["check_id"],
            expected_tip_check_id=shared.body["check_id"],
            outcome="PASSED",
            **self.context(self.crew[0]),
        )
        expect(
            DeficiencyProjection.objects.get(deficiency_id=shared_id).status,
            "OPEN",
            "other effective failure keeps deficiency open",
        )
        self.supported_check = other.body["check_id"]
        self.supported_deficiency = shared_id
        self.capture("T2")
        self.invalidate_evidence()

    def invalidate_evidence(self):
        root, deficiency = self.corrected_root, self.corrected_deficiency
        shared_id = self.supported_deficiency
        operational_status = self.checklist().vehicle_status
        self.refused(
            CommandDenied,
            lambda: invalidate_chain(
                check_id=self.supported_check,
                explanation="Synthetic rejected crew invalidation.",
                **self.context(self.crew[0]),
            ),
            "crew cannot invalidate",
        )
        self.refused(
            CheckError,
            lambda: invalidate_chain(
                check_id=self.supported_check, explanation="", **self.context()
            ),
            "invalidation requires explanation",
            "EXPLANATION_REQUIRED",
        )
        invalidate_chain(
            check_id=self.supported_check,
            explanation="Synthetic evidence invalid.",
            **self.context(),
        )
        expect(
            DeficiencyProjection.objects.get(deficiency_id=shared_id).status,
            "OPEN",
            "invalidated failure leaves deficiency open",
        )
        invalidate_chain(
            check_id=self.corrected_tip, explanation="Synthetic chain invalid.", **self.context()
        )
        expect(
            DeficiencyProjection.objects.get(deficiency_id=deficiency).status,
            "OPEN",
            "invalidating corrected chain reopens reported-in-error deficiency",
        )
        self.refused(
            CheckError,
            lambda: correct_check(
                check_id=root,
                expected_tip_check_id=self.corrected_tip,
                outcome="FAILED",
                **self.context(self.crew[0]),
            ),
            "invalid chain cannot be corrected",
        )
        expect(
            self.checklist().vehicle_status,
            operational_status,
            "invalidation never changes operational status",
        )
        expect(
            CheckRecord.objects.filter(root_check_id=root).count(),
            2,
            "invalidated correction chain preserves both rows",
        )
        expect(
            self.red("correction"), True, "reopened deficiency remains red despite independent Pass"
        )

    def unable_and_odometer(self):
        self.refused(
            CheckError,
            lambda: self.record("unable", outcome="UNABLE_TO_INSPECT", deficiency=self.defect()),
            "Unable needs reason",
        )
        unable = self.record(
            "unable",
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic access blocked.",
            deficiency=self.defect(),
        )
        row = self.earliest("unable")
        expect(row.state, State.CURRENT, "Unable leaves obligation unsatisfied")
        args = {
            "obligation_id": row.obligation.id,
            "check_id": unable.body["check_id"],
            "explanation": "Synthetic supervisor review of inaccessible item.",
        }
        self.refused(
            CommandDenied,
            lambda: complete_unable_obligation(**args, **self.context(self.crew[0])),
            "crew cannot complete Unable",
        )
        complete_unable_obligation(**args, **self.context(self.supervisor))
        completed = self.earliest("unable")
        expect(
            (completed.state, completed.outcome, completed.display_text),
            (
                State.SATISFIED,
                "SUPERVISOR_COMPLETED",
                "Completed by supervisor — unable to inspect",
            ),
            "supervisor completion is distinct from Passed",
        )
        expect(
            DeficiencyProjection.objects.get(deficiency_id=unable.body["deficiency_ids"][0]).status,
            "OPEN",
            "supervisor completion leaves deficiency open",
        )
        self.odometer()

    def odometer(self):
        before = self.odometer_states()
        expect(
            sorted(r.due_at for r in before),
            sorted({r.obligation.due_at for r in self.rows()}),
            "exactly one odometer obligation for every scheduled inspection",
        )
        expect(
            len([r for r in before if r.due_at == self.start + timedelta(hours=6)]),
            1,
            "one shared odometer obligation per inspection",
        )
        record_odometer(
            vehicle_id=str(self.vehicle.pk),
            value=5000,
            unit="mi",
            **self.context(self.crew[1]),
        )
        expect(
            min(self.odometer_states(), key=lambda r: r.due_at).state,
            State.SATISFIED,
            "any crew member satisfies vehicle odometer",
        )
        count = OdometerEntry.objects.count()
        self.refused(
            CheckError,
            lambda: record_odometer(
                vehicle_id=str(self.vehicle.pk), value=4999, unit="mi", **self.context(self.crew[2])
            ),
            "lower odometer requires confirmation",
            "ANOMALY_CONFIRMATION_REQUIRED",
        )
        expect(OdometerEntry.objects.count(), count, "unconfirmed reading writes nothing")
        reading = record_odometer(
            vehicle_id=str(self.vehicle.pk),
            value=4999,
            unit="mi",
            confirm_anomaly=True,
            explanation="Synthetic instrument replacement.",
            **self.context(self.crew[2]),
        )
        expect(
            OdometerEntry.objects.get(pk=reading.body["entry_id"]).anomaly_confirmed,
            True,
            "lower reading confirmation retained",
        )
        expect(EngineHoursEntry.objects.count(), 0, "engine hours optional")

    def kits_and_links(self):
        self.record("kit contents", kit_id=self.kit_a)
        expect(
            self.earliest("kit contents", self.kit_a).state, State.SATISFIED, "first kit checked"
        )
        expect(
            self.earliest("kit contents", self.kit_b).state,
            State.CURRENT,
            "identical second kit unsatisfied",
        )
        expect(
            [
                r
                for r in self.rows(vehicle=self.empty_vehicle)
                if r.obligation.item_id == str(self.items["kit contents"].pk)
            ],
            [],
            "zero-kit vehicle owes no kit obligation",
        )
        self.clock.set(self.start + timedelta(hours=6))
        added = self.add_kit("mid-window")
        added_rows = self.item_rows("kit contents", added)
        expect(
            [r.obligation.due_at for r in added_rows],
            [
                self.start + timedelta(days=1, hours=6),
                self.start + timedelta(days=1, hours=6, minutes=30),
            ],
            "new kit skips both already-open windows and owes the next inspections",
        )
        record_kit_assignment(kit_id=self.kit_b, active=False, **self.context(self.supervisor))
        removed = self.item_rows("kit contents", self.kit_b)
        expect(
            [r.obligation.due_at for r in removed],
            [self.start + timedelta(hours=6)],
            "removal preserves arrived due and removes future dues",
        )
        self.link_cardinality()
        self.capture("T3")

    def link_cardinality(self):
        # Two eligible overlapping obligations; the command may choose exactly one without a rule.
        response = self.record("link cardinality")
        check = CheckRecord.objects.get(pk=response.body["check_id"])
        rows = self.item_rows("link cardinality")
        candidates = [
            r for r in rows if r.obligation.early_at <= check.recorded_at <= r.obligation.grace_at
        ]
        expect(len(candidates), 2, "overlapping windows really eligible")
        expect(
            [r["obligation_id"] for r in response.body["obligation_links"]],
            [min(candidates, key=lambda r: r.obligation.due_at).obligation.id],
            "earliest due only",
        )
        other = next(
            r
            for r in candidates
            if r.obligation.id != response.body["obligation_links"][0]["obligation_id"]
        )

        def forbidden_link():
            with transaction.atomic():
                CheckObligationLink.objects.create(
                    organization=self.org,
                    check_record=check,
                    obligation=Obligation.objects.get(pk=other.obligation.id),
                    ordinal=1,
                    shared_rule=None,
                    command=check.command,
                    recorded_at=check.recorded_at,
                    actor_user=check.actor_user,
                    actor_roles_at_time=check.actor_roles_at_time,
                )

        self.refused(DatabaseError, forbidden_link, "unruled second link rejected by database")
        invalidate_chain(
            check_id=str(check.pk), explanation="Synthetic link invalidation.", **self.context()
        )
        expect(
            [
                r.state
                for r in self.item_rows("link cardinality")
                if r.obligation.id in {c.obligation.id for c in candidates}
            ],
            [State.DUE, State.CURRENT],
            "invalidated check satisfies neither overlapping obligation",
        )

    def idle_and_history(self):
        session = InspectionSession.objects.get(pk=self.session)
        last_activity = current_state(session).last_activity_at
        # A separate session remains open throughout: its status cannot erase completed work.
        never_closed = self.open(self.crew[2], separate=True)
        expect(self.earliest("suction").state, State.SATISFIED, "unclosed session needs no submit")
        self.clock.set(last_activity + timedelta(hours=5, minutes=59))
        read_session(self.org.pk, self.session)
        expect(current_state(session).last_activity_at, last_activity, "viewing is not activity")
        self.clock.set(last_activity + timedelta(hours=6, minutes=1))
        before, count = self.rows(), CheckRecord.objects.count()
        result = auto_close_inactive_sessions()
        expect(result["failed"], 0, "auto-close failures")
        expect(current_state(session).state, "INCOMPLETE", "six-hour inactivity close")
        event = SessionEvent.objects.filter(session=session).latest("sequence")
        expect(
            event.effective_at, last_activity + timedelta(hours=6), "activity-based close timestamp"
        )
        expect(
            (self.rows(), CheckRecord.objects.count()),
            (before, count),
            "close preserves compliance and checks",
        )
        reopen_session(session_id=self.session, **self.context(self.crew[0]))
        expect(
            InspectionSession.objects.get(pk=self.session).checklist_version_id,
            self.version,
            "reopen retains original version",
        )
        expect(
            AuditEvent.objects.filter(action="SESSION_REOPEN", actor_user=self.crew[0]).exists(),
            True,
            "crew reopen is audited",
        )
        expect(self.rows(), before, "reopen changes no compliance")
        expect(
            current_state(InspectionSession.objects.get(pk=never_closed)).state,
            "OPEN",
            "other session was never manually closed",
        )
        self.verify_history()

    def verify_history(self):
        expect([s[0] for s in self.snapshots], ["T1", "T2", "T3"], "three historical observations")
        history = load_projection_history(
            str(self.vehicle.pk),
            self.clock.now,
            organization_id=str(self.org.pk),
            start=self.start,
            end=self.end,
        )
        rng = Random(88)
        shuffled = {
            f.name: tuple(rng.sample(value, len(value)))
            for f in fields(history)
            if isinstance(value := getattr(history, f.name), tuple)
        }
        history = replace(history, **shuffled)
        checks, _ = replay_checks(history, self.clock.now)
        expect(
            any(c.id in {self.corrected_root, self.corrected_tip} for c in checks),
            False,
            "invalid chain revives no earlier outcome",
        )
        expect(
            rebuild_vehicle_projection(
                history, str(self.vehicle.pk), self.clock.now, self.start, self.end
            ),
            self.rows(),
            "shuffled final replay equals live projection",
        )
        for label, instant, rows, checklist, odometer in self.snapshots:
            expect(self.rows(instant), rows, f"{label} exact historical obligation query")
            expect(
                rebuild_vehicle_projection(
                    history, str(self.vehicle.pk), instant, self.start, self.end
                ),
                rows,
                f"{label} shuffled database-free event replay",
            )
            expect(
                self.checklist(instant), checklist, f"{label} historical checklist and attention"
            )
            expect(self.odometer_states(instant), odometer, f"{label} historical odometer")
        expect(
            DeficiencyEvent.objects.filter(deficiency_id=self.corrected_deficiency)
            .order_by("sequence")
            .values_list("to_status", flat=True)
            .last(),
            "OPEN",
            "deficiency event replay agrees with current projection",
        )
        try:
            socket.getaddrinfo("m3-no-network.example.invalid", 443)
        except NetworkAccessBlocked:
            pass
        else:
            raise AssertionError("M3 scenario guard allowed external DNS")

    def live_delivery(self):
        """Subscribe locally, commit a real check, and receive its identifier-only update."""
        from unittest.mock import patch

        layer = InMemoryChannelLayer()
        channel = async_to_sync(layer.new_channel)()
        group = f"vehicle.{self.org.pk}.{self.vehicle.pk}"
        async_to_sync(layer.group_add)(group, channel)
        try:
            with patch("cya.live_updates.publish.get_channel_layer", return_value=layer):
                result = self.record("live delivery", self.crew[2])

                async def receive():
                    import asyncio

                    return await asyncio.wait_for(layer.receive(channel), timeout=5)

                message = async_to_sync(receive)()
            expect(
                message,
                {
                    "type": "live.update",
                    "organization_id": str(self.org.pk),
                    "vehicle_id": str(self.vehicle.pk),
                    "event_id": result.body["check_id"],
                    "kind": "check.recorded",
                },
                "committed check delivered through in-process channel layer",
            )
            expect(
                CheckRecord.objects.filter(pk=message["event_id"]).exists(),
                True,
                "delivery refers to committed evidence",
            )
        finally:
            async_to_sync(layer.group_discard)(group, channel)
            async_to_sync(layer.flush)()
