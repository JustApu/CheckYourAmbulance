"""Command-backed M4 exit journey: deficiency resolution and readiness (issue 18).

It reuses the M3 journey's synthetic agency (InspectionScenario.setup) and drives the M4 commands
through their real, audited, committing interfaces: assignment, deadlines, dependency, extensions,
correction and verification, invalidation, added verification, administrator closure, check
suspension, operational status, notifications and the readiness dashboard. Only the clock is
injected; email goes to a stub sender and external networking is blocked.

This driver runs only through the scenario test runner, never against runtime data.
"""

import socket
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import timedelta
from random import Random
from threading import Barrier

from django.db import close_old_connections

from cya.accounts.models import User
from cya.attention.readiness import fleet_readiness
from cya.checks.commands import CheckError, correct_check, invalidate_chain
from cya.checks.models import CheckRecord
from cya.core.audit_commands import CommandDenied
from cya.deficiencies import acknowledgement
from cya.deficiencies.closure import close_administrator, reopen_administrator
from cya.deficiencies.commands import (
    assign_deficiency,
    record_correction,
    report_deficiency,
    set_deadline,
    start_work,
)
from cya.deficiencies.dependency import end_dependency, enter_dependency
from cya.deficiencies.expansion import add_verification
from cya.deficiencies.extensions import decide_extension, request_extension
from cya.deficiencies.models import (
    CheckSuspension,
    DeficiencyEvent,
    DeficiencyProjection,
    VerificationScopeRow,
)
from cya.deficiencies.overdue import notify_overdue
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.suspension import suspend_checks
from cya.notifications.delivery import deliver_pending
from cya.notifications.models import NotificationOutbox
from cya.obligations.history import replay_checks
from cya.obligations.projections import load_projection_history
from cya.obligations.state import State, rebuild_vehicle_projection
from cya.operations.commands import set_vehicle_status
from cya.operations.models import VehicleStatusEvent
from cya.testing.inspections import InspectionScenario
from cya.testing.network import NetworkAccessBlocked
from cya.testing.notifications import StubSender, notified
from cya.testing.role_http import expect


class ResolutionScenario(InspectionScenario):
    def setup(self):
        super().setup()
        self.end = self.start + timedelta(days=6)
        self.second_supervisor = self.user("second supervisor")
        from cya.authorization.commands import change_role

        change_role(
            target_id=str(self.second_supervisor.pk),
            role="SUPERVISOR",
            action="GRANT",
            **self.context(),
        )
        acknowledgement.set_acknowledgement_maximum(maximum_days=3, **self.context())
        self.supervisors = sorted([self.supervisor.pk, self.second_supervisor.pk])

    # -- helpers ---------------------------------------------------------------------------

    def newest(self, deficiency_id):
        return str(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence").pk
        )

    def args(self, deficiency_id, actor=None):
        return {
            "deficiency_id": deficiency_id,
            "expected_event_id": self.newest(deficiency_id),
            **self.context(actor or self.supervisor),
        }

    def status(self, deficiency_id):
        return DeficiencyProjection.objects.get(deficiency_id=deficiency_id).status

    def events(self, deficiency_id):
        return list(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id)
            .order_by("sequence")
            .values_list("event_type", flat=True)
        )

    def failed(self, name, actor=None, **kw):
        result = self.routine(name, actor, outcome="FAILED", deficiency=self.defect(), **kw)
        return result.body["check_id"], result.body["deficiency_ids"][0]

    def basis(self, name, kit=None):
        """The item's newest effective check, which a client names as basis_check_id (CON-3)."""
        return next(
            r.latest_check_id
            for r in self.checklist().rows
            if r.item_id == str(self.items[name].pk) and r.kit_id == kit
        )

    def routine(self, name, actor=None, kit=None, **kw):
        return self.record(name, actor, basis_check_id=self.basis(name, kit), kit_id=kit, **kw)

    def verify(self, deficiency_id, name, actor=None, kit=None, basis=None, **kw):
        return self.record(
            name,
            actor or self.crew[1],
            purpose="VERIFICATION",
            verification_for_deficiency_id=deficiency_id,
            basis_check_id=basis if basis is not None else self.basis(name, kit),
            kit_id=kit,
            **kw,
        )

    def vehicle_status(self):
        return list(
            VehicleStatusEvent.objects.filter(vehicle=self.vehicle).values_list("status", flat=True)
        )

    def picture(self, instant=None):
        picture = fleet_readiness(str(self.org.pk), as_of=instant, now=self.clock.now)
        return next(v for v in picture.vehicles if v.vehicle_id == str(self.vehicle.pk))

    def capture(self, label):
        super().capture(label)
        instant = self.snapshots[-1][1]
        self.resolution_snapshots.append(
            (label, instant, self.picture(instant), self.deficiency_states(instant))
        )

    def deficiency_states(self, instant):
        """Each deficiency's status at the instant, replayed from its events."""
        states = {}
        for event in DeficiencyEvent.objects.filter(
            organization_id=self.org.pk, recorded_at__lte=instant
        ).order_by("deficiency_id", "sequence"):
            states[str(event.deficiency_id)] = event.to_status
        return states

    # -- journey ---------------------------------------------------------------------------

    def lifecycle(self):
        """Assign, start, wait on a dependency, extend: AC-16 preamble, NOT-3 recipients."""
        self.resolution_snapshots = []
        crew = self.crew[0]
        self.check_one, self.d_item = self.failed("suction", crew)
        expect(self.status(self.d_item), "OPEN", "a Failed check reports the deficiency")
        assigned = assign_deficiency(assignee_id=str(crew.pk), **self.args(self.d_item)).body
        expect(notified("deficiency.assigned", assigned["event_id"]), [crew.pk], "assignee told")
        set_deadline(deadline=self.clock.now + timedelta(hours=2), **self.args(self.d_item))
        start_work(**self.args(self.d_item))
        expect(self.status(self.d_item), "IN_PROGRESS", "work started")
        follow_up = self.clock.now + timedelta(days=1)
        enter_dependency(
            explanation="Synthetic part on order.",
            follow_up_at=follow_up,
            **self.args(self.d_item, crew),
        )
        expect(self.status(self.d_item), "AWAITING_EXTERNAL_DEPENDENCY", "dependency entered")
        dashboard = next(
            d for d in self.picture().deficiencies if d["deficiency_id"] == self.d_item
        )
        expect(
            (dashboard["awaiting_dependency"], dashboard["follow_up_at"]),
            (True, follow_up),
            "the dashboard shows the dependency and its follow-up (RPT-5)",
        )
        end_dependency(**self.args(self.d_item, crew))
        expect(self.status(self.d_item), "IN_PROGRESS", "dependency ended back to work")
        asked = request_extension(
            reason="Synthetic vendor delay.",
            proposed_deadline=self.clock.now + timedelta(days=2),
            **self.args(self.d_item, crew),
        ).body["event_id"]
        expect(notified("deficiency.extension_requested", asked), self.supervisors, "supervisors")
        decided = decide_extension(
            request_event_id=asked,
            decision="DIFFERENT_DATE",
            deadline=self.clock.now + timedelta(days=1),
            explanation="Synthetic: one day.",
            **self.args(self.d_item),
        ).body["event_id"]
        expect(notified("deficiency.extension_decided", decided), [crew.pk], "requester told")
        self.capture("T1")

    def verification(self):
        """Correction, scope, a routine Pass that does not count, a concurrent closing race."""
        crew = self.crew[0]
        record_correction(**self.args(self.d_item, crew))
        expect(self.status(self.d_item), "PENDING_VERIFICATION", "repair recorded")
        expect(
            VerificationScopeRow.objects.filter(deficiency_id=self.d_item).count(),
            1,
            "item scope materialized",
        )
        self.routine("suction", self.crew[2])
        expect(self.status(self.d_item), "PENDING_VERIFICATION", "a routine Pass does not verify")
        closing = self.verify(self.d_item, "suction").body
        expect(closing["verification"]["status"], "CLOSED_VERIFIED", "the last Pass closes")
        self.closing_check = closing["check_id"]
        expect(self.events(self.d_item).count("VERIFICATION_CLOSED"), 1, "closed once")

        # AC-16: a compartment-wide deficiency whose two last items pass at the same moment.
        self.d_scope = report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"compartment_id": str(self.compartment.pk)},
            category_id=str(self.category.stable_category_id),
            severity="MEDIUM",
            summary="Synthetic cabinet defect",
            description="Synthetic integration evidence.",
            **self.context(crew),
        ).body["deficiency_id"]
        start_work(**self.args(self.d_scope))
        record_correction(**self.args(self.d_scope, crew))
        rows = list(
            VerificationScopeRow.objects.filter(deficiency_id=self.d_scope).values_list(
                "item_id", "kit_instance_id"
            )
        )
        names = {str(item.pk): name for name, item in self.items.items()}
        jobs = [(names[str(item)], str(kit) if kit else None) for item, kit in rows]
        for name, kit in jobs[:-2]:
            self.verify(self.d_scope, name, kit=kit)
        bases = [self.basis(name, kit) for name, kit in jobs[-2:]]
        barrier = Barrier(2)

        def submit(job):
            close_old_connections()
            try:
                (name, kit), basis = job
                barrier.wait(timeout=15)
                return self.verify(self.d_scope, name, self.crew[2], kit=kit, basis=basis)
            except Exception as error:  # returned to the asserting thread
                return error
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, zip(jobs[-2:], bases, strict=True)))
        expect([o for o in outcomes if isinstance(o, Exception)], [], "both last passes saved")
        expect(self.status(self.d_scope), "CLOSED_VERIFIED", "concurrent last passes close")
        expect(self.events(self.d_scope).count("VERIFICATION_CLOSED"), 1, "exactly once (AC-16)")
        self.capture("T2")

    def reopenings(self):
        """INV-7 reopen, added verification after closure (AC-17), administrator close/reopen."""
        crew = self.crew[0]
        invalidate_chain(
            check_id=self.closing_check, explanation="Synthetic wrong vehicle.", **self.context()
        )
        expect(self.status(self.d_item), "OPEN", "withdrawn evidence reopens to Open (INV-4)")
        reopened = DeficiencyEvent.objects.filter(
            deficiency_id=self.d_item, event_type="VERIFICATION_REOPENED"
        ).get()
        expect(
            notified("deficiency.reopened_verification_invalidated", reopened.pk),
            sorted([crew.pk, *self.supervisors]),
            "assignee and supervisors told",
        )
        start_work(**self.args(self.d_item))
        record_correction(**self.args(self.d_item, crew))
        self.verify(self.d_item, "suction")
        expect(self.status(self.d_item), "CLOSED_VERIFIED", "repaired and verified again")
        due = self.clock.now + timedelta(days=1)
        added = add_verification(
            explanation="Synthetic: also check oxygen.",
            items=[{"item_id": str(self.items["oxygen"].pk)}],
            deadline=due,
            **self.args(self.d_item),
        ).body["event_id"]
        expect(self.status(self.d_item), "OPEN", "added verification reopens")
        expect(
            notified("deficiency.reopened_verification_added", added),
            [crew.pk],
            "only the current assignee is told (AC-17)",
        )
        expect(DeficiencyEvent.objects.get(pk=added).deadline, due, "with the new deadline (AC-17)")
        self.refused(
            CommandDenied,
            lambda: close_administrator(
                explanation="Synthetic.", **self.args(self.d_item, self.supervisor)
            ),
            "a supervisor cannot close without verification",
        )
        close_administrator(explanation="Synthetic closure.", **self.args(self.d_item, self.admin))
        expect(self.status(self.d_item), "CLOSED_ADMINISTRATOR", "administrator closure")
        reopen_administrator(explanation="Synthetic reopen.", **self.args(self.d_item, self.admin))
        expect(self.status(self.d_item), "OPEN", "administrator reopening")
        # INV-3: invalidating a Failed check never closes its deficiency.
        invalidate_chain(
            check_id=self.check_one, explanation="Synthetic duplicate.", **self.context()
        )
        expect(self.status(self.d_item), "OPEN", "invalidation never closes (INV-3)")

    def suspensions_and_status(self):
        """AC-18 suspension by each closing path; AC-19 no automatic status; AC-20 Exempt."""
        _check, by_verification = self.failed("lighting")
        _check, by_administrator = self.failed("monitor")
        error_check, by_error = self.failed("oxygen", self.crew[1])
        for deficiency_id, name in (
            (by_verification, "lighting"),
            (by_administrator, "monitor"),
            (by_error, "oxygen"),
        ):
            suspend_checks(
                items=[{"item_id": str(self.items[name].pk)}], **self.args(deficiency_id)
            )
        tomorrow = [
            r
            for r in self.rows()
            if r.obligation.item_id == str(self.items["lighting"].pk)
            and r.obligation.due_at > self.clock.now + timedelta(hours=12)
        ]
        # Today's obligation keeps the Failed check recorded before the suspension (owner
        # decision on PR 29); tomorrow's are Suspended.
        expect(bool(tomorrow), True, "later lighting obligations exist")
        expect({r.state for r in tomorrow}, {State.SUSPENDED}, "suspended, never Satisfied")
        expect(self.picture().counts["SUSPENDED"] > 0, True, "the dashboard counts Suspended")
        start_work(**self.args(by_verification))
        record_correction(**self.args(by_verification, self.crew[0]))
        self.verify(by_verification, "lighting")
        close_administrator(explanation="Synthetic.", **self.args(by_administrator, self.admin))
        correct_check(
            check_id=error_check,
            expected_tip_check_id=error_check,
            outcome="PASSED",
            **self.context(self.crew[1]),
        )
        for deficiency_id, closing in (
            (by_verification, "VERIFICATION_CLOSED"),
            (by_administrator, "ADMINISTRATOR_CLOSED"),
            (by_error, "REPORTED_IN_ERROR"),
        ):
            event = DeficiencyEvent.objects.get(deficiency_id=deficiency_id, event_type=closing)
            ended = CheckSuspension.objects.filter(
                deficiency_id=deficiency_id, action="END", deficiency_event=event
            ).count()
            expect(ended, 1, f"{closing} ends the suspension (AC-18)")
        expect(self.picture().suspensions, [], "no suspension outlives its deficiency")

        before = self.vehicle_status()
        self.routine(
            "critical",
            outcome="FAILED",
            deficiency={"create": {**self.defect()["create"], "severity": "CRITICAL"}},
        )
        expect(self.vehicle_status(), before, "a Critical Failed check changes no status (AC-19)")
        self.clock.set(self.start + timedelta(hours=23))  # late on day 1: today's checks are late
        expect(any(r.state == State.OVERDUE for r in self.rows()), True, "an item is now overdue")
        notify_overdue(str(self.org.pk))
        expect(self.vehicle_status(), before, "an overdue item changes no status (AC-19)")
        self.capture("T3")
        set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="OUT_OF_SERVICE",
            explanation="Synthetic: in the shop.",
            **self.context(self.supervisor),
        )
        self.clock.set(self.start + timedelta(days=1, hours=12))  # past day 2's due instants
        exempt = [r for r in self.rows() if r.state == State.EXEMPT]
        expect(bool(exempt), True, "obligations due while out of service are Exempt (AC-20)")
        expect(
            any(r.state == State.SATISFIED and r.outcome == "PASSED" for r in exempt),
            False,
            "Exempt, never Passed (AC-20)",
        )
        set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="IN_SERVICE",
            explanation="Synthetic: back.",
            **self.context(self.supervisor),
        )

    def dashboard_and_notifications(self):
        """RPT-7 states shown apart; email intents delivered through the stub, no network."""
        counts = self.picture().counts
        # Suspended was asserted while the suspensions were in force; each ended before any
        # window closed, so no obligation stays Suspended now (evaluate_obligation, min(T, G)).
        for state in ("PASSED", "FAILED", "EXEMPT", "INVALIDATED", "SUPERSEDED"):
            expect(counts[state] > 0, True, f"the dashboard shows {state} on its own (RPT-7)")
        expect("COMPLIANT" in counts, False, "no single compliant flag")
        sender = StubSender()
        pending = NotificationOutbox.objects.filter(channel="EMAIL").count()
        expect(pending > 0, True, "the journey produced email intents")
        sent = 0
        while True:
            report = deliver_pending(str(self.org.pk), sender, base_url="")
            if not report.sent:
                break
            sent += report.sent
        expect(sent, pending, "every email intent is sent once through the stub (AC-28)")
        intended = Counter(
            User.objects.get(pk=row.recipient_user_id).email
            for row in NotificationOutbox.objects.filter(channel="EMAIL")
        )
        expect(Counter(m.to for m in sender.sent), intended, "each intent reaches its recipient")
        try:
            socket.getaddrinfo("m4-no-network.example.invalid", 443)
        except NetworkAccessBlocked:
            pass
        else:
            raise AssertionError("M4 scenario guard allowed external DNS")

    def history(self):
        """AC-26/27: T1, T2, T3 answer exactly after later events, and from a shuffled replay."""
        expect([s[0] for s in self.resolution_snapshots], ["T1", "T2", "T3"], "three observations")
        history = load_projection_history(
            str(self.vehicle.pk),
            self.clock.now,
            organization_id=str(self.org.pk),
            start=self.start,
            end=self.end,
        )
        rng = Random(18)
        shuffled = {
            f.name: tuple(rng.sample(value, len(value)))
            for f in fields(history)
            if isinstance(value := getattr(history, f.name), tuple)
        }
        history = replace(history, **shuffled)
        replay_checks(history, self.clock.now)
        for (label, instant, rows, checklist, _odometer), (_l, _i, picture, states) in zip(
            self.snapshots, self.resolution_snapshots, strict=True
        ):
            expect(self.rows(instant), rows, f"{label} exact historical obligations")
            expect(
                rebuild_vehicle_projection(
                    history, str(self.vehicle.pk), instant, self.start, self.end
                ),
                rows,
                f"{label} shuffled database-free replay",
            )
            expect(self.checklist(instant), checklist, f"{label} historical checklist")
            expect(self.picture(instant), picture, f"{label} historical dashboard")
            expect(self.deficiency_states(instant), states, f"{label} deficiency states")
        expect(
            CheckRecord.objects.filter(purpose="VERIFICATION").exists(),
            True,
            "verification checks retained",
        )

    @staticmethod
    def refused(error_type, call, label, code=None):
        try:
            call()
        except (error_type, DeficiencyError, CheckError) as error:
            if code is not None:
                expect(getattr(error, "code", None), code, label)
            return error
        raise AssertionError(f"{label}: accepted a forbidden operation")
