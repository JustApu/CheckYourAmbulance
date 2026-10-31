"""Deadline extension requests and decisions (issue 150 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/150); SPEC DEF-5, section 3.3, 8.4)."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies import deadlines, service
from cya.deficiencies.commands import assign_deficiency, set_deadline
from cya.deficiencies.extensions import decide_extension, request_extension
from cya.deficiencies.models import DeficiencyEvent
from cya.deficiencies.tests.test_dependency import DependencyCase
from cya.notifications.models import NotificationOutbox
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

REASON = "The vendor delivers the synthetic part late."
WHY = "Reviewed the vendor's delivery note."


class ExtensionCase(DependencyCase):
    def future(self, days):
        return timezone.now() + timedelta(days=days)

    def deadline(self, days=2, **kw):
        when = self.future(days)
        set_deadline(deadline=when, **self.args(**kw))
        return when

    def ask(self, days=5, reason=REASON, **kw):
        return request_extension(
            reason=reason, proposed_deadline=self.future(days), **self.args(**kw)
        )

    def decide(self, request_event_id, decision="APPROVED", text=WHY, deadline=None, **kw):
        return decide_extension(
            request_event_id=request_event_id,
            decision=decision,
            explanation=text,
            deadline=deadline,
            **self.args(**{"actor": self.supervisor, **kw}),
        )

    def state(self, **kw):
        return service.extension_state(str(self.org.pk), self.deficiency_id, **kw)

    def events(self, event_type=None, deficiency_id=None):
        rows = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id or self.deficiency_id)
        if event_type:
            rows = rows.filter(event_type=event_type)
        return rows.order_by("sequence")


class RequestTests(ExtensionCase):
    def test_a_request_writes_the_event_the_receipt_and_an_ids_only_audit(self):
        self.deadline()
        before = self.projection()
        reported_deadline = before.deadline
        when = self.future(5)
        result = request_extension(
            reason=REASON, proposed_deadline=when, **self.args(actor=self.crew)
        )
        self.assertEqual(result.status, 201)
        self.assertEqual(result.body["code"], "DEFICIENCY_EXTENSION_REQUESTED")
        event = self.events("EXTENSION_REQUESTED").get()
        self.assertEqual(result.body["event_id"], event.pk)
        self.assertEqual(
            (event.explanation, event.proposed_deadline, event.deadline, event.decision),
            (REASON, when, None, None),
        )
        self.assertEqual((event.from_status, event.to_status), ("OPEN", "OPEN"))
        self.assertEqual(event.actor_user_id, self.crew.pk)
        receipt = CommandReceipt.objects.get(pk=event.command_id)
        self.assertEqual(receipt.command_type, "DEFICIENCY_EXTENSION_REQUEST")
        projection = self.projection()
        self.assertEqual(projection.last_applied_event_id, event.pk)
        self.assertEqual(
            (projection.status, projection.deadline, projection.assignee_id),
            ("OPEN", reported_deadline, before.assignee_id),
        )
        audit = AuditEvent.objects.get(action="DEFICIENCY_EXTENSION_REQUEST")
        self.assertEqual(audit.outcome, "SUCCEEDED")
        self.assertNotIn(REASON, json.dumps([audit.new_snapshot, audit.prior_snapshot]))
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_a_request_needs_a_deadline_and_a_later_future_date_and_a_reason(self):
        self.refused("NO_DEADLINE", 422, self.ask)
        governing = self.deadline(3)
        self.refused("INVALID_EXTENSION_DATE", 422, self.ask, days=-1)
        self.refused("INVALID_EXTENSION_DATE", 422, self.ask, days=2)
        for bad in ("", "   ", None, 5, "x" * 4001):
            self.refused("MALFORMED_REQUEST", 400, self.ask, reason=bad)
        naive = (timezone.now() + timedelta(days=9)).replace(tzinfo=None)
        self.refused(
            "MALFORMED_REQUEST",
            400,
            request_extension,
            reason=REASON,
            proposed_deadline=naive,
            **self.args(),
        )
        self.refused(
            "MALFORMED_REQUEST",
            400,
            request_extension,
            reason=REASON,
            proposed_deadline=None,
            **self.args(),
        )
        self.assertEqual(self.state()["governing_deadline"], governing)
        self.assertEqual(len(self.events("EXTENSION_REQUESTED")), 0)

    def test_requests_are_unlimited_and_each_is_an_event(self):
        self.deadline()
        ids = [self.ask(days=5 + n, actor=self.crew).body["event_id"] for n in range(3)]
        self.assertEqual(len(set(ids)), 3)
        state = self.state()
        self.assertEqual(
            (state["extension_count"], state["approved_count"]),
            (3, 0),
        )
        self.assertTrue(all(r["status"] == "PENDING" for r in state["requests"]))

    def test_a_closed_deficiency_takes_no_request(self):
        self.deadline()
        from cya.deficiencies.models import DeficiencyProjection

        DeficiencyProjection.objects.filter(deficiency_id=self.deficiency_id).update(
            status="CLOSED_VERIFIED"
        )
        self.refused("DEFICIENCY_NOT_ACTIVE", 422, self.ask)


class DecisionTests(ExtensionCase):
    def setUp(self):
        super().setUp()
        self.base = self.deadline(3)

    def asked(self, days=6, **kw):
        return self.ask(days=days, actor=self.crew, **kw).body["event_id"]

    def test_approval_governs_the_date_that_was_asked_for(self):
        when = self.future(6)
        request = request_extension(
            reason=REASON, proposed_deadline=when, **self.args(actor=self.crew)
        ).body["event_id"]
        result = self.decide(request)
        self.assertEqual(result.body["code"], "DEFICIENCY_EXTENSION_DECIDED")
        event = self.events("EXTENSION_DECIDED").get()
        self.assertEqual(
            (event.decision, event.deadline, event.request_event_id, event.explanation),
            ("APPROVED", when, request, WHY),
        )
        state = self.state()
        self.assertEqual(state["base_deadline"], self.base)
        self.assertEqual(state["governing_deadline"], when)
        self.assertEqual(state["approved_count"], 1)
        self.assertEqual(self.projection().last_applied_event_id, event.pk)
        audit = AuditEvent.objects.get(action="DEFICIENCY_EXTENSION_DECIDE")
        self.assertEqual(audit.new_snapshot["values"]["decision"], "APPROVED")
        self.assertNotIn(WHY, json.dumps(audit.new_snapshot))

    def test_a_different_date_governs_and_a_denial_leaves_the_deadline(self):
        request = self.asked()
        chosen = self.future(8)
        self.decide(request, "DIFFERENT_DATE", deadline=chosen)
        self.assertEqual(self.state()["governing_deadline"], chosen)
        second = self.asked(days=20)
        self.decide(second, "DENIED")
        state = self.state()
        self.assertEqual(state["governing_deadline"], chosen)
        statuses = {r["request_event_id"]: r["status"] for r in state["requests"]}
        self.assertEqual(statuses, {request: "DIFFERENT_DATE", second: "DENIED"})
        self.assertEqual(self.events("EXTENSION_DECIDED").last().deadline, None)

    def test_the_decision_is_checked_before_anything_is_written(self):
        request = self.asked()
        before = self.snapshot()
        for decision in ("MAYBE", "approved", None, 3):
            self.refused("MALFORMED_REQUEST", 400, self.decide, request, decision)
        self.refused("MALFORMED_REQUEST", 400, self.decide, request, "DIFFERENT_DATE")
        self.refused(
            "MALFORMED_REQUEST", 400, self.decide, request, "DENIED", deadline=self.future(9)
        )
        self.refused(
            "MALFORMED_REQUEST", 400, self.decide, request, "APPROVED", deadline=self.future(9)
        )
        self.refused("MALFORMED_REQUEST", 400, self.decide, request, text="  ")
        self.refused("MALFORMED_REQUEST", 400, self.decide, "nope")
        self.refused("REQUEST_NOT_FOUND", 422, self.decide, str(uuid4()))
        self.refused(
            "INVALID_EXTENSION_DATE",
            422,
            self.decide,
            request,
            "DIFFERENT_DATE",
            deadline=self.future(2),
        )
        self.refused(
            "INVALID_EXTENSION_DATE",
            422,
            self.decide,
            request,
            "DIFFERENT_DATE",
            deadline=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(self.snapshot(), before)

    def test_a_request_is_decided_once(self):
        request = self.asked()
        self.decide(request, "DENIED")
        self.refused("REQUEST_ALREADY_DECIDED", 422, self.decide, request, "APPROVED")

    def test_a_request_of_another_deficiency_is_not_found(self):
        other = self.deadline(3, deficiency_id=self.supply_id)
        foreign = self.ask(days=6, deficiency_id=self.supply_id, actor=self.crew).body["event_id"]
        self.assertIsNotNone(other)
        self.refused("REQUEST_NOT_FOUND", 422, self.decide, foreign)

    def test_an_approved_date_that_has_passed_or_been_overtaken_is_refused(self):
        slow = self.asked(days=4)
        fast = self.asked(days=7)
        self.decide(fast)
        # approving the earlier ask would now move the governing deadline backwards
        self.refused("INVALID_EXTENSION_DATE", 422, self.decide, slow)
        self.decide(slow, "DIFFERENT_DATE", deadline=self.future(9))
        self.assertEqual(self.state()["approved_count"], 2)

    def test_a_new_deadline_supersedes_older_requests_and_approvals(self):
        request = self.asked(days=6)
        pending = self.asked(days=7)
        self.decide(request)
        mid = timezone.now()
        replacement = self.deadline(12)
        state = self.state()
        self.assertEqual(
            (state["base_deadline"], state["governing_deadline"]), (replacement, replacement)
        )
        self.assertTrue(all(r["superseded"] for r in state["requests"]))
        by_id = {r["request_event_id"]: r for r in state["requests"]}
        self.assertEqual(
            (by_id[request]["status"], by_id[pending]["status"]), ("APPROVED", "PENDING")
        )
        self.refused("REQUEST_SUPERSEDED", 422, self.decide, pending)
        # the answer for an instant before the new deadline is still the old approved date
        earlier = self.state(as_of=mid)
        self.assertEqual(earlier["governing_deadline"], by_id[request]["decided_deadline"])
        self.assertFalse(any(r["superseded"] for r in earlier["requests"]))
        # a new request is made against the new deadline
        again = self.ask(days=14, actor=self.crew).body["event_id"]
        self.decide(again)
        self.assertEqual(self.state()["governing_deadline"].date(), self.future(14).date())

    def test_the_status_and_the_other_cached_fields_never_move(self):
        projection = self.projection()
        request = self.asked()
        self.decide(request)
        after = self.projection()
        self.assertEqual(
            (after.status, after.deadline, after.assignee_id),
            (projection.status, projection.deadline, projection.assignee_id),
        )


class PermissionTests(ExtensionCase):
    def setUp(self):
        super().setUp()
        self.deadline(3)
        self.deadline(3, deficiency_id=self.supply_id)

    def test_crew_requests_but_cannot_decide(self):
        request = self.ask(actor=self.crew).body["event_id"]
        with self.assertRaises(CommandDenied):
            self.decide(request, actor=self.crew)
        self.decide(request, actor=self.supervisor)
        second = self.ask(days=9, actor=self.supervisor).body["event_id"]
        self.decide(second, "DENIED", actor=self.admin)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="DEFICIENCY_EXTENSION_DECIDE", outcome="DENIED"
            ).count(),
            1,
        )

    def test_a_supply_officer_requests_only_on_a_deficiency_assigned_to_them(self):
        before = self.snapshot()
        for deficiency_id in (self.deficiency_id, self.supply_id):
            with self.assertRaises(CommandDenied):
                self.ask(actor=self.supply, deficiency_id=deficiency_id)
        self.assertEqual(self.snapshot(), before)
        assign_deficiency(
            assignee_id=str(self.supply.pk), **self.args(deficiency_id=self.supply_id)
        )
        self.ask(actor=self.supply, deficiency_id=self.supply_id)
        with self.assertRaises(CommandDenied):
            self.ask(actor=self.supply)  # still unassigned to them
        request = self.events("EXTENSION_REQUESTED", self.supply_id).get().pk
        with self.assertRaises(CommandDenied):
            self.decide(request, deficiency_id=self.supply_id, actor=self.supply)

    def test_a_denial_reveals_nothing_about_the_deficiency(self):
        unknown, seen = str(uuid4()), str(uuid4())
        with self.assertRaises(CommandDenied):
            self.ask(actor=self.supply, deficiency_id=unknown, expected=seen)
        with self.assertRaises(CommandDenied):
            self.decide(str(uuid4()), actor=self.crew, deficiency_id=unknown, expected=seen)
        error = self.refused(
            "NOT_FOUND", 404, self.ask, actor=self.crew, deficiency_id=unknown, expected=seen
        )
        self.assertIsNone(error.path)


class ReplayAndStaleTests(ExtensionCase):
    def setUp(self):
        super().setUp()
        self.deadline(3)

    def test_a_replayed_operation_returns_the_stored_result_and_writes_nothing(self):
        args = self.args(actor=self.crew)
        when = self.future(6)
        first = request_extension(reason=REASON, proposed_deadline=when, **args)
        again = request_extension(reason=REASON, proposed_deadline=when, **args)
        self.assertEqual(again.body, first.body)
        self.assertEqual(len(self.events("EXTENSION_REQUESTED")), 1)
        different = request_extension(reason="Different.", proposed_deadline=when, **args)
        self.assertTrue(different.operation_reused)
        self.assertEqual(len(self.events("EXTENSION_REQUESTED")), 1)

    def test_a_stale_expected_event_is_a_409_and_writes_nothing(self):
        args = self.args(actor=self.crew)
        self.ask(actor=self.crew)  # moves the newest event
        before = self.snapshot()
        error = self.refused(
            "DEFICIENCY_CHANGED",
            409,
            request_extension,
            reason=REASON,
            proposed_deadline=self.future(7),
            **args,
        )
        self.assertEqual(error.path, "expected_event_id")
        self.assertEqual(self.snapshot(), before)

    def test_a_failed_command_rolls_back_its_receipt(self):
        count = CommandReceipt.objects.count()
        self.refused("INVALID_EXTENSION_DATE", 422, self.ask, days=1)
        self.assertEqual(CommandReceipt.objects.count(), count)


class DeadlineStateTests(ExtensionCase):
    def test_overdue_and_age_follow_the_governing_deadline_not_the_base(self):
        base = self.deadline(2)
        request = self.ask(days=6, actor=self.crew).body["event_id"]
        approved = self.future(6)
        self.decide(request, "DIFFERENT_DATE", deadline=approved)
        late_base = base + timedelta(hours=1)
        between = self.state(now=late_base)
        self.assertFalse(between["overdue"])  # the approved date, not the base, governs
        after = self.state(now=approved + timedelta(minutes=1))
        self.assertTrue(after["overdue"])
        reported = after["reported_at"]
        self.assertEqual(
            after["age_seconds"],
            int((approved + timedelta(minutes=1) - reported).total_seconds()),
        )

    def test_a_deficiency_without_a_deadline_is_never_overdue(self):
        state = self.state(now=timezone.now() + timedelta(days=400))
        self.assertEqual(
            (state["governing_deadline"], state["overdue"], state["extension_count"]),
            (None, False, 0),
        )

    def test_a_closed_deficiency_is_not_overdue(self):
        self.assertFalse(
            deadlines.is_overdue(
                timezone.now(), "CLOSED_VERIFIED", timezone.now() + timedelta(days=1)
            )
        )
        self.assertTrue(
            deadlines.is_overdue(timezone.now(), "OPEN", timezone.now() + timedelta(days=1))
        )

    def test_the_as_of_reader_answers_from_the_events_recorded_by_then(self):
        self.deadline(2)
        before_request = timezone.now()
        request = self.ask(days=6, actor=self.crew).body["event_id"]
        between = timezone.now()
        self.decide(request)
        self.assertEqual(self.state(as_of=before_request)["extension_count"], 0)
        asked_only = self.state(as_of=between)
        self.assertEqual(
            (asked_only["extension_count"], asked_only["requests"][0]["status"]),
            (1, "PENDING"),
        )
        self.assertEqual(self.state()["requests"][0]["status"], "APPROVED")
        too_early = self.state_before_report()
        self.assertEqual(too_early, "NOT_FOUND")

    def state_before_report(self):
        try:
            self.state(as_of=timezone.now() - timedelta(days=30))
        except service.DeficiencyError as error:
            return error.code
        return None

    def test_the_history_is_capped_and_refused_not_cut(self):
        self.deadline(2)
        self.ask(actor=self.crew)
        old = service.MAX_DEADLINE_EVENTS
        service.MAX_DEADLINE_EVENTS = 1
        try:
            with self.assertRaises(service.DeficiencyError) as caught:
                self.state()
        finally:
            service.MAX_DEADLINE_EVENTS = old
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("CONFIGURATION_UNAVAILABLE", 503)
        )


class PureDeadlineTests(ExtensionCase):
    def test_a_decision_with_no_pending_request_is_an_inconsistent_history(self):
        from types import SimpleNamespace as E

        now = timezone.now()
        orphan = E(
            event_type="EXTENSION_DECIDED",
            pk="c",
            deadline=now,
            proposed_deadline=None,
            decision="APPROVED",
            request_event_id="missing",
            explanation="e",
            recorded_at=now,
            actor_user_id="v",
        )
        with self.assertRaises(deadlines.InconsistentHistory):
            deadlines.deadline_state([orphan])
        with self.assertRaises(service.DeficiencyError) as caught:
            service.replay_deadlines([orphan])
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("CONFIGURATION_UNAVAILABLE", 503)
        )

    """The function takes events, never the database or the clock."""

    def test_a_reported_deadline_counts_as_the_first_revision(self):
        from types import SimpleNamespace as E

        now = timezone.now()
        events = [
            E(
                event_type="REPORTED",
                pk="a",
                deadline=now,
                proposed_deadline=None,
                decision=None,
                request_event_id=None,
                explanation=None,
                recorded_at=now,
                actor_user_id="u",
            ),
            E(
                event_type="EXTENSION_REQUESTED",
                pk="b",
                deadline=None,
                proposed_deadline=now + timedelta(days=1),
                decision=None,
                request_event_id=None,
                explanation="r",
                recorded_at=now,
                actor_user_id="u",
            ),
            E(
                event_type="EXTENSION_DECIDED",
                pk="c",
                deadline=now + timedelta(days=1),
                proposed_deadline=None,
                decision="APPROVED",
                request_event_id="b",
                explanation="e",
                recorded_at=now,
                actor_user_id="v",
            ),
        ]
        state = deadlines.deadline_state(events)
        self.assertEqual(
            (state["base_event_id"], state["governing_event_id"], state["governing_deadline"]),
            ("a", "c", now + timedelta(days=1)),
        )
        self.assertEqual(deadlines.deadline_state([])["governing_deadline"], None)


class ExtensionGuardTests(ExtensionCase):
    """The database refuses what the commands would not write (migration 0008)."""

    def setUp(self):
        super().setUp()
        self.base = self.deadline(3)
        self.last = self.events().last()

    def receipt(self, command_type, actor=None):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=actor or self.crew,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def event(self, command_type="DEFICIENCY_EXTENSION_REQUEST", **overrides):
        receipt = self.receipt(command_type, overrides.get("actor_user"))
        last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest("sequence")
        fields = {
            "organization_id": self.org.pk,
            "deficiency_id": self.deficiency_id,
            "sequence": last.sequence + 1,
            "predecessor_event": last,
            "event_type": "EXTENSION_REQUESTED",
            "from_status": last.to_status,
            "to_status": last.to_status,
            "explanation": REASON,
            "proposed_deadline": self.base + timedelta(days=3),
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.crew,
            "actor_roles_at_time": ["CREW"],
            **overrides,
        }
        return DeficiencyEvent.objects.create(**fields)

    def refused_event(self, *args, **kwargs):
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.event(*args, **kwargs)

    def decision(self, request, **overrides):
        fields = {
            "event_type": "EXTENSION_DECIDED",
            "explanation": WHY,
            "proposed_deadline": None,
            "decision": "APPROVED",
            "request_event": request,
            "deadline": self.base + timedelta(days=3),
            "actor_user": self.supervisor,
            "actor_roles_at_time": ["SUPERVISOR"],
            **overrides,
        }
        return self.event("DEFICIENCY_EXTENSION_DECIDE", **fields)

    def test_valid_events_are_accepted_so_the_refusals_below_are_meaningful(self):
        with transaction.atomic():
            request = self.event()
        with transaction.atomic():
            self.decision(request)

    def test_the_request_rules_are_repeated_in_the_database(self):
        later = self.base + timedelta(days=3)
        self.refused_event(command_type="DEFICIENCY_ASSIGN")  # not its own command
        self.refused_event(proposed_deadline=self.base)  # not later than the governing deadline
        self.refused_event(proposed_deadline=self.base - timedelta(days=1))
        self.refused_event(proposed_deadline=timezone.now() - timedelta(days=1))
        self.refused_event(explanation=None)
        self.refused_event(explanation="   ")
        self.refused_event(proposed_deadline=None)
        self.refused_event(deadline=later)  # a request carries no decided date
        self.refused_event(decision="APPROVED")
        self.refused_event(to_status="IN_PROGRESS")
        self.refused_event(follow_up_at=later)
        self.refused_event(assignee=self.crew)
        self.refused_event(actor_user=self.supply, actor_roles_at_time=["SUPPLY_OFFICER"])
        self.refused_event(actor_roles_at_time=[])
        with transaction.atomic():
            self.event(proposed_deadline=later)

    def test_a_request_needs_a_deadline_to_extend(self):
        # the supply deficiency has no deadline yet
        last = DeficiencyEvent.objects.filter(deficiency_id=self.supply_id).latest("sequence")
        receipt = self.receipt("DEFICIENCY_EXTENSION_REQUEST")
        with self.assertRaises(DatabaseError), transaction.atomic():
            DeficiencyEvent.objects.create(
                organization_id=self.org.pk,
                deficiency_id=self.supply_id,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type="EXTENSION_REQUESTED",
                from_status=last.to_status,
                to_status=last.to_status,
                explanation=REASON,
                proposed_deadline=timezone.now() + timedelta(days=5),
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.crew,
                actor_roles_at_time=["CREW"],
            )

    def test_the_decision_rules_are_repeated_in_the_database(self):
        with transaction.atomic():
            request = self.event()
        later = self.base + timedelta(days=3)
        self.assertEqual(self.last.event_type, "DEADLINE_SET")
        self.refused_event(
            "DEFICIENCY_EXTENSION_DECIDE",
            event_type="EXTENSION_DECIDED",
            explanation=WHY,
            proposed_deadline=None,
            decision="APPROVED",
            request_event=request,
            deadline=later,
            actor_user=self.crew,
            actor_roles_at_time=["CREW"],
        )
        self.refused_event(
            command_type="DEFICIENCY_EXTENSION_REQUEST",
            event_type="EXTENSION_DECIDED",
            explanation=WHY,
            proposed_deadline=None,
            decision="APPROVED",
            request_event=request,
            deadline=later,
            actor_user=self.supervisor,
            actor_roles_at_time=["SUPERVISOR"],
        )
        for overrides in (
            {"deadline": None},  # an approval without the date
            {"decision": "DENIED"},  # a denial with a date
            {"decision": "SOMETIMES"},
            {"deadline": self.base},  # not later than the governing deadline
            {"deadline": timezone.now() - timedelta(days=1)},
            {"request_event": None},
            {"explanation": None},
            {"proposed_deadline": later},
        ):
            self.refused_event(
                "DEFICIENCY_EXTENSION_DECIDE",
                **{
                    "event_type": "EXTENSION_DECIDED",
                    "explanation": WHY,
                    "proposed_deadline": None,
                    "decision": "APPROVED",
                    "request_event": request,
                    "deadline": later,
                    "actor_user": self.supervisor,
                    "actor_roles_at_time": ["SUPERVISOR"],
                    **overrides,
                },
            )

    def test_an_approval_carries_exactly_the_requested_date(self):
        with transaction.atomic():
            request = self.event()
        asked = request.proposed_deadline
        for other in (asked + timedelta(days=1), asked + timedelta(seconds=1)):
            with self.assertRaises(DatabaseError), transaction.atomic():
                self.decision(request, deadline=other)
        # a different date is the decider's own choice and is allowed any later date
        with transaction.atomic():
            self.decision(request, decision="DIFFERENT_DATE", deadline=asked + timedelta(days=1))

    def test_a_decision_answers_one_undecided_current_request(self):
        with transaction.atomic():
            request = self.event()
        with transaction.atomic():
            self.decision(request)
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.decision(request, decision="DENIED", deadline=None)  # decided twice
        # a request of another deficiency, or one that is not a request, cannot be answered
        other_last = DeficiencyEvent.objects.filter(deficiency_id=self.supply_id).first()
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.decision(other_last)
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.decision(self.last)

    def test_a_later_deadline_supersedes_an_undecided_request_in_the_database(self):
        request = self.ask(days=6, actor=self.crew).body["event_id"]
        request = DeficiencyEvent.objects.get(pk=request)
        newer = self.deadline(9)
        self.assertGreater(newer, self.base)
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.decision(request, deadline=newer + timedelta(days=1))

    def test_only_a_supervisor_or_administrator_may_decide(self):
        with transaction.atomic():
            request = self.event()
        for user, roles in ((self.crew, ["CREW"]), (self.supply, ["SUPPLY_OFFICER"])):
            with self.assertRaises(DatabaseError), transaction.atomic():
                self.decision(request, actor_user=user, actor_roles_at_time=roles)
        with transaction.atomic():
            self.decision(request, actor_user=self.admin, actor_roles_at_time=["ADMINISTRATOR"])

    def test_a_supply_officer_may_request_only_on_a_deficiency_assigned_to_them(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.event(actor_user=self.supply, actor_roles_at_time=["SUPPLY_OFFICER"])
        assign_deficiency(assignee_id=str(self.supply.pk), **self.args())
        with transaction.atomic():
            self.event(actor_user=self.supply, actor_roles_at_time=["SUPPLY_OFFICER"])

    def test_the_migration_cannot_be_reversed_while_extension_events_exist(self):
        from importlib import import_module
        from types import SimpleNamespace

        migration = import_module("cya.deficiencies.migrations.0008_deadline_extensions")
        editor = SimpleNamespace(connection=connection)
        migration._reverse_guard(None, editor)  # nothing to lose yet
        with transaction.atomic():
            self.event()
        with self.assertRaises(RuntimeError):
            migration._reverse_guard(None, editor)

    def test_the_new_columns_belong_to_decisions_alone(self):
        with transaction.atomic():
            request = self.event()
        for overrides in (
            {"event_type": "PROGRESS_NOTE", "proposed_deadline": None, "decision": "APPROVED"},
        ):
            self.refused_event("DEFICIENCY_PROGRESS_NOTE", **overrides)
        self.assertEqual(request.decision, None)

    def test_a_request_event_reference_is_unique_and_a_foreign_key(self):
        with transaction.atomic():
            request = self.event()
        with transaction.atomic():
            self.decision(request)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.table_constraints WHERE "
                "table_name='deficiency_event' AND constraint_type='FOREIGN KEY' "
                "AND constraint_schema=DATABASE()"
            )
            self.assertGreater(cursor.fetchone()[0], 0)


class ExtensionRaceTests(ExtensionCase):
    def test_simultaneous_decisions_on_one_request_record_exactly_one(self):
        self.deadline(3)
        request = self.ask(days=6, actor=self.crew).body["event_id"]
        newest = self.newest()
        outcomes = []

        def decide(decision):
            from django.db import connections

            try:
                return decide_extension(
                    request_event_id=request,
                    decision=decision,
                    explanation=WHY,
                    deadline=None,
                    **self.args(actor=self.supervisor, expected=newest),
                )
            except Exception as error:  # the loser fails with the stale check
                return error
            finally:
                connections.close_all()

        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(decide, ("DENIED", "APPROVED")))
        wins = [o for o in outcomes if not isinstance(o, Exception)]
        losses = [o for o in outcomes if isinstance(o, Exception)]
        self.assertEqual((len(wins), len(losses)), (1, 1))
        self.assertEqual(losses[0].code, "DEFICIENCY_CHANGED")
        self.assertEqual(len(self.events("EXTENSION_DECIDED")), 1)


@override_settings(**real_collaborator_settings())
class ExtensionApiTests(ExtensionCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.crew_client, self.crew_user = self.harness.sign_in("crew-http")
        self.sup_client, self.sup_user = self.harness.sign_in("supervisor-http")
        self.grant(self.sup_user, "SUPERVISOR")
        self.admin_client, self.admin_user = self.harness.sign_in("admin-http")
        self.grant(self.admin_user, "ADMINISTRATOR")
        self.base = f"/api/v1/deficiencies/{self.deficiency_id}"
        self.base_deadline = self.deadline(3)

    def post(self, client, path, body=None, base=None):
        client.get("/")
        token = client.cookies["csrftoken"].value
        if not isinstance(body, (str, bytes)):
            body = {"operation_id": str(uuid4()), "expected_event_id": self.newest()} | (body or {})
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return client.post(
            f"{base or self.base}/{path}",
            data=data,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    def rejected(self, response, status, code, paths=()):
        self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.assertEqual(
            [(e["path"], e["code"]) for e in response.json()["field_errors"]],
            [tuple(p) for p in paths],
        )

    def ask_api(self, client=None, days=6):
        return self.post(
            client or self.crew_client,
            "extension-requests",
            {"reason": REASON, "proposed_deadline": self.future(days).isoformat()},
        )

    def test_crew_asks_a_supervisor_decides_and_the_reader_shows_it(self):
        asked = self.ask_api()
        payload = asked.json()
        self.assertEqual((asked.status_code, payload["status"]), (201, "COMMITTED"))
        result = payload["result"]
        self.assertTrue(result["proposed_deadline"] and result["server_recorded_at"].endswith("Z"))
        request = result["event_id"]
        decided = self.post(
            self.sup_client,
            "extension-decisions",
            {"request_event_id": request, "decision": "APPROVED", "explanation": WHY},
        )
        self.assertEqual(decided.status_code, 201)
        self.assertEqual(decided.json()["result"]["decision"], "APPROVED")
        view = self.crew_client.get(f"{self.base}/deadline").json()  # any member reads
        self.assertEqual(
            (view["extension_count"], view["approved_count"], view["overdue"]), (1, 1, False)
        )
        self.assertEqual(view["requests"][0]["status"], "APPROVED")
        self.assertEqual(view["governing_deadline"], view["requests"][0]["decided_deadline"])
        self.assertNotEqual(view["governing_deadline"], view["base_deadline"])
        self.assertTrue(view["governing_deadline"].endswith("Z"))
        self.assertEqual(view["last_event_id"], decided.json()["result"]["event_id"])
        self.assertIsNone(view["age_level"])
        self.assertFalse(view["long_running"])

    def test_a_decision_carries_a_date_only_when_the_decider_chooses_one(self):
        request = self.ask_api().json()["result"]["event_id"]
        chosen = self.future(9).isoformat()
        decided = self.post(
            self.sup_client,
            "extension-decisions",
            {
                "request_event_id": request,
                "decision": "DIFFERENT_DATE",
                "explanation": WHY,
                "deadline": chosen,
            },
        )
        self.assertEqual(decided.status_code, 201)
        self.assertTrue(decided.json()["result"]["governing_deadline"])
        again = self.ask_api(days=20).json()["result"]["event_id"]
        denied = self.post(
            self.admin_client,
            "extension-decisions",
            {"request_event_id": again, "decision": "DENIED", "explanation": WHY},
        )
        self.assertIsNone(denied.json()["result"].get("governing_deadline"))

    def test_the_reader_answers_as_of_an_instant_and_rejects_bad_parameters(self):
        asked = self.ask_api().json()["result"]
        after = self.crew_client.get(
            f"{self.base}/deadline", {"as_of": asked["server_recorded_at"]}
        ).json()
        self.assertEqual(after["extension_count"], 1)
        before = self.crew_client.get(f"{self.base}/deadline", {"as_of": "2000-01-01T00:00:00Z"})
        self.assertEqual((before.status_code, before.json()["code"]), (404, "NOT_FOUND"))
        for query in ({"as_of": "yesterday"}, {"as_of": "2026-01-01T00:00:00"}, {"x": "1"}):
            self.assertEqual(
                self.crew_client.get(f"{self.base}/deadline", query).status_code, 400, query
            )

    def test_refusals_carry_the_stable_codes(self):
        request = self.ask_api().json()["result"]["event_id"]
        self.rejected(
            self.post(
                self.crew_client,
                "extension-decisions",
                {"request_event_id": request, "decision": "DENIED", "explanation": WHY},
            ),
            403,
            "FORBIDDEN",
        )
        self.rejected(
            self.post(
                self.crew_client,
                "extension-requests",
                {
                    "reason": REASON,
                    "proposed_deadline": self.future(6).isoformat(),
                    "expected_event_id": str(uuid4()),
                },
            ),
            409,
            "DEFICIENCY_CHANGED",
            [("expected_event_id", "DEFICIENCY_CHANGED")],
        )
        self.rejected(
            self.post(
                self.crew_client,
                "extension-requests",
                {"reason": REASON, "proposed_deadline": self.future(1).isoformat()},
            ),
            422,
            "INVALID_EXTENSION_DATE",
            [("proposed_deadline", "INVALID_EXTENSION_DATE")],
        )
        self.rejected(
            self.post(
                self.sup_client,
                "extension-decisions",
                {"request_event_id": str(uuid4()), "decision": "DENIED", "explanation": WHY},
            ),
            422,
            "REQUEST_NOT_FOUND",
            [("request_event_id", "REQUEST_NOT_FOUND")],
        )
        self.post(
            self.sup_client,
            "extension-decisions",
            {"request_event_id": request, "decision": "DENIED", "explanation": WHY},
        )
        self.rejected(
            self.post(
                self.sup_client,
                "extension-decisions",
                {"request_event_id": request, "decision": "DENIED", "explanation": WHY},
            ),
            422,
            "REQUEST_ALREADY_DECIDED",
            [("request_event_id", "REQUEST_ALREADY_DECIDED")],
        )

    def test_malformed_bodies_are_rejected_before_any_write(self):
        count = DeficiencyEvent.objects.count()
        good = {"reason": REASON, "proposed_deadline": self.future(6).isoformat()}
        self.rejected(
            self.post(self.crew_client, "extension-requests", {**good, "extra": 1}),
            400,
            "MALFORMED_REQUEST",
            [("$", "UNKNOWN_FIELD")],
        )
        self.rejected(
            self.post(self.crew_client, "extension-requests", {"reason": REASON}),
            400,
            "MALFORMED_REQUEST",
            [("proposed_deadline", "REQUIRED")],
        )
        self.rejected(
            self.post(
                self.crew_client, "extension-requests", {**good, "proposed_deadline": "soon"}
            ),
            400,
            "MALFORMED_REQUEST",
            [("proposed_deadline", "INVALID")],
        )
        self.rejected(
            self.post(self.crew_client, "extension-requests", "not json"),
            400,
            "MALFORMED_REQUEST",
        )
        self.rejected(
            self.post(
                self.sup_client,
                "extension-decisions",
                {"request_event_id": str(uuid4()), "decision": "MAYBE", "explanation": WHY},
            ),
            400,
            "MALFORMED_REQUEST",
            [("decision", "MALFORMED_REQUEST")],
        )
        self.assertEqual(DeficiencyEvent.objects.count(), count)

    def test_a_replay_is_acknowledged_again_and_signed_out_visitors_are_refused(self):
        from django.test import Client

        body = {
            "operation_id": str(uuid4()),
            "expected_event_id": self.newest(),
            "reason": REASON,
            "proposed_deadline": self.future(6).isoformat(),
        }
        first = self.post(self.crew_client, "extension-requests", body).json()
        events = DeficiencyEvent.objects.count()
        self.assertEqual(self.post(self.crew_client, "extension-requests", body).json(), first)
        self.assertEqual(DeficiencyEvent.objects.count(), events)
        body["reason"] = "Different."
        self.rejected(
            self.post(self.crew_client, "extension-requests", body),
            409,
            "OPERATION_REUSED",
            [("operation_id", "REUSED")],
        )
        for path in ("extension-requests", "extension-decisions"):
            response = Client().post(f"{self.base}/{path}", "{}", content_type="application/json")
            self.assertEqual(response.status_code, 401, path)
        self.assertEqual(Client().get(f"{self.base}/deadline").status_code, 401)
        self.assertEqual(Client().get("/api/v1/deficiency-age-rule").status_code, 401)

    def test_the_administrator_sets_and_everyone_reads_the_age_rule(self):
        rule = {
            "operation_id": str(uuid4()),
            "age_thresholds_days": [7, 30],
            "long_running_age_days": 60,
            "long_running_extensions": None,
        }
        self.rejected(self.sup_post(rule), 403, "FORBIDDEN")
        saved = self.admin_post(rule)
        self.assertEqual((saved.status_code, saved.json()["result"]["revision"]), (201, 1))
        view = self.crew_client.get("/api/v1/deficiency-age-rule").json()
        self.assertEqual(view["rule"]["age_thresholds_days"], [7, 30])
        self.assertEqual(len(view["history"]), 1)
        self.assertTrue(view["rule"]["recorded_at"].endswith("Z"))
        self.rejected(
            self.admin_post(rule | {"operation_id": str(uuid4())}),
            422,
            "AGE_RULE_UNCHANGED",
            [("age_thresholds_days", "AGE_RULE_UNCHANGED")],
        )
        self.rejected(
            self.admin_post(rule | {"operation_id": str(uuid4()), "age_thresholds_days": [5, 5]}),
            400,
            "MALFORMED_REQUEST",
            [("age_thresholds_days", "MALFORMED_REQUEST")],
        )
        self.rejected(
            self.admin_post({"operation_id": str(uuid4())}),
            400,
            "MALFORMED_REQUEST",
            [
                ("age_thresholds_days", "REQUIRED"),
                ("long_running_age_days", "REQUIRED"),
                ("long_running_extensions", "REQUIRED"),
            ],
        )
        # the reader shows the rule in force at an instant
        early = self.crew_client.get(
            "/api/v1/deficiency-age-rule", {"as_of": "2000-01-01T00:00:00Z"}
        )
        self.assertEqual((early.status_code, early.json()["rule"]), (200, None))
        self.assertEqual(
            self.crew_client.get("/api/v1/deficiency-age-rule", {"as_of": "x"}).status_code, 400
        )

    def admin_post(self, body):
        return self.raw_post(self.admin_client, body)

    def sup_post(self, body):
        return self.raw_post(self.sup_client, body)

    def raw_post(self, client, body):
        client.get("/")
        return client.post(
            "/api/v1/deficiency-age-rule",
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )


class ExtensionConcurrencyTests(ExtensionCase):
    """Writers to one deficiency are serialized by its lock; the loser sees a stale event."""

    def race(self, calls):
        from django.db import connections

        def run(call):
            try:
                return call()
            except Exception as error:  # the loser fails with the stale check
                return error
            finally:
                connections.close_all()

        with ThreadPoolExecutor(len(calls)) as pool:
            return list(pool.map(run, calls))

    def test_simultaneous_requests_naming_the_same_event_record_exactly_one(self):
        self.deadline(3)
        newest = self.newest()
        calls = [
            lambda days=days: request_extension(
                reason=REASON,
                proposed_deadline=self.future(days),
                **self.args(actor=self.crew, expected=newest),
            )
            for days in (6, 7)
        ]
        outcomes = self.race(calls)
        losses = [o for o in outcomes if isinstance(o, Exception)]
        self.assertEqual((len(outcomes) - len(losses), len(losses)), (1, 1))
        self.assertEqual(losses[0].code, "DEFICIENCY_CHANGED")
        self.assertEqual(len(self.events("EXTENSION_REQUESTED")), 1)

    def test_a_request_racing_a_new_deadline_is_never_decided_against_the_wrong_one(self):
        self.deadline(3)
        newest = self.newest()
        outcomes = self.race(
            [
                lambda: request_extension(
                    reason=REASON,
                    proposed_deadline=self.future(6),
                    **self.args(actor=self.crew, expected=newest),
                ),
                lambda: set_deadline(deadline=self.future(10), **self.args(expected=newest)),
            ]
        )
        self.assertEqual(sum(isinstance(o, Exception) for o in outcomes), 1)
        state = self.state()
        # whichever won, the chain is a single line and every request belongs to a known revision
        sequences = list(self.events().values_list("sequence", flat=True))
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
        self.assertEqual(state["last_event_id"], self.newest())
