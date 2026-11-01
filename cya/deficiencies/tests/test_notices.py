"""Deficiency notifications: recipients, overrides, rollback and the overdue job (issue 14).

SPEC 13 NOT-1, NOT-3, NOT-5, 8.4, 8.7 and AC-17. Recipients come from the documented defaults in
cya/notifications/default_recipients.json and administrator overrides; the actor is never told
about their own action, and an inactive person is skipped.
"""

from datetime import timedelta
from unittest import mock

from django.db import DatabaseError
from django.utils import timezone

from cya.core.audit_commands import CommandDenied
from cya.core.models import CommandReceipt
from cya.deficiencies import acknowledgement, overdue
from cya.deficiencies.closure import close_administrator, reopen_administrator
from cya.deficiencies.commands import assign_deficiency, set_deadline
from cya.deficiencies.expansion import add_verification
from cya.deficiencies.extensions import decide_extension, request_extension
from cya.deficiencies.models import DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.tests.test_assignment import LifecycleCase
from cya.deficiencies.tests.test_verification import VerificationBase
from cya.notifications.commands import set_recipient_rule
from cya.notifications.models import NotificationOutbox
from cya.notifications.rules import NotificationError
from cya.testing.notifications import notified

REASON = "Synthetic: the replacement part is back-ordered."


class NoticeCase(LifecycleCase):
    def setUp(self):
        super().setUp()
        self.fixer = self.make_user("fixer")  # holds Crew; a different person from the reporter

    def reporter(self):
        return DeficiencyEvent.objects.get(
            deficiency_id=self.deficiency_id, sequence=1
        ).actor_user_id

    def last(self):
        return DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest("sequence")

    def rule(self, event_type, recipient, enabled, actor=None):
        return set_recipient_rule(
            event_type=event_type,
            recipient=recipient,
            enabled=enabled,
            **self.context(actor or self.admin),
        )


class AssignmentAndClosureTests(NoticeCase):
    def test_assignment_notifies_the_new_assignee_in_app_and_by_email(self):
        self.assign(self.fixer)
        event = self.last()
        rows = NotificationOutbox.objects.filter(source_event_id=str(event.pk))
        self.assertEqual(
            sorted((row.recipient_user_id, row.channel) for row in rows),
            sorted([(self.fixer.pk, "EMAIL"), (self.fixer.pk, "IN_APP")]),
        )
        row = rows.get(channel="IN_APP")
        self.assertEqual(
            (row.event_type, row.template_key, row.source_event_type, row.deep_link),
            (
                "deficiency.assigned",
                "deficiency.assigned",
                "deficiency_event",
                f"/deficiencies/{self.deficiency_id}/",
            ),
        )
        self.assertEqual(row.command_id, event.command_id)  # the same transaction
        self.assertEqual(
            row.payload,
            {
                "deficiency_id": self.deficiency_id,
                "vehicle_id": str(self.vehicle.pk),
                "event": "ASSIGNED",
                "status": "OPEN",
            },
        )

    def test_assigning_yourself_notifies_nobody(self):
        self.assign(self.supervisor)
        self.assertEqual(notified("deficiency.assigned"), [])

    def test_administrator_closure_and_reopening_tell_the_assignee_and_the_reporter(self):
        self.assign(self.fixer)
        close_administrator(explanation="Synthetic closure.", **self.args(actor=self.admin))
        closed = self.last()
        expected = sorted([self.fixer.pk, self.reporter()])
        self.assertEqual(notified("deficiency.administrator_closed", closed.pk), expected)
        reopen_administrator(explanation="Synthetic reopening.", **self.args(actor=self.admin))
        reopened = self.last()
        self.assertEqual(notified("deficiency.reopened_by_administrator", reopened.pk), expected)

    def test_an_inactive_assignee_is_skipped_rather_than_failing_the_command(self):
        self.assign(self.fixer)
        type(self.fixer).objects.filter(pk=self.fixer.pk).update(active=False)
        close_administrator(explanation="Synthetic closure.", **self.args(actor=self.admin))
        self.assertEqual(notified("deficiency.administrator_closed"), [self.reporter()])


class ExtensionTests(NoticeCase):
    def setUp(self):
        super().setUp()
        self.assign(self.fixer)
        set_deadline(deadline=timezone.now() + timedelta(days=2), **self.args())

    def test_request_goes_to_supervisors_and_the_decision_to_requester_and_assignee(self):
        request = request_extension(
            reason=REASON,
            proposed_deadline=timezone.now() + timedelta(days=6),
            **self.args(actor=self.fixer),
        ).body["event_id"]
        self.assertEqual(notified("deficiency.extension_requested", request), [self.supervisor.pk])
        decided = decide_extension(
            request_event_id=request,
            decision="APPROVED",
            explanation="Synthetic approval.",
            deadline=None,
            **self.args(actor=self.supervisor),
        ).body["event_id"]
        # The requester is also the assignee: one notification each, never two (de-duplicated).
        self.assertEqual(notified("deficiency.extension_decided", decided), [self.fixer.pk])
        row = NotificationOutbox.objects.get(source_event_id=decided, channel="IN_APP")
        self.assertEqual(row.payload["decision"], "APPROVED")

    def test_decision_reaches_a_requester_who_is_not_the_assignee(self):
        request = request_extension(
            reason=REASON,
            proposed_deadline=timezone.now() + timedelta(days=6),
            **self.args(actor=self.crew),
        ).body["event_id"]
        decided = decide_extension(
            request_event_id=request,
            decision="DENIED",
            explanation="Synthetic denial.",
            deadline=None,
            **self.args(actor=self.supervisor),
        ).body["event_id"]
        self.assertEqual(
            notified("deficiency.extension_decided", decided), sorted([self.crew.pk, self.fixer.pk])
        )


class OverrideTests(NoticeCase):
    def test_an_administrator_changes_recipients_row_by_row(self):
        saved = self.rule("deficiency.assigned", "ASSIGNEE", False)
        self.assertEqual(saved.body["recipients"], [])
        saved = self.rule("deficiency.assigned", "SUPERVISORS", True)
        self.assertEqual(saved.body["recipients"], ["SUPERVISORS"])
        assign_deficiency(assignee_id=str(self.fixer.pk), **self.args(actor=self.admin))
        self.assertEqual(notified("deficiency.assigned"), [self.supervisor.pk])

    def test_only_administrators_change_recipients_and_input_is_checked(self):
        with self.assertRaises(CommandDenied):
            self.rule("deficiency.assigned", "SUPERVISORS", True, actor=self.supervisor)
        for event, recipient, enabled, code in (
            ("deficiency.unknown", "ASSIGNEE", True, "NOT_FOUND"),
            ("Deficiency.Assigned", "ASSIGNEE", True, "MALFORMED_REQUEST"),
            ("vehicle.status_changed", "ASSIGNEE", True, "MALFORMED_REQUEST"),
            ("deficiency.assigned", "EVERYONE", True, "MALFORMED_REQUEST"),
            ("deficiency.assigned", "ASSIGNEE", "yes", "MALFORMED_REQUEST"),
        ):
            with (
                self.subTest(event=event, recipient=recipient),
                self.assertRaises(NotificationError) as caught,
            ):
                self.rule(event, recipient, enabled)
            self.assertEqual(caught.exception.code, code)


class RollbackTests(NoticeCase):
    def test_an_outbox_write_failure_rolls_back_the_causing_command(self):
        before = (
            self.last().pk,
            DeficiencyProjection.objects.get(deficiency_id=self.deficiency_id),
        )
        receipts = CommandReceipt.objects.count()
        with (
            mock.patch.object(
                NotificationOutbox.objects, "bulk_create", side_effect=DatabaseError("synthetic")
            ),
            self.assertRaises(DatabaseError),
        ):
            self.assign(self.fixer)
        self.assertEqual(self.last().pk, before[0])
        self.assertIsNone(
            DeficiencyProjection.objects.get(deficiency_id=self.deficiency_id).assignee_id
        )
        self.assertEqual(CommandReceipt.objects.count(), receipts)
        self.assertFalse(NotificationOutbox.objects.exists())


class AddedVerificationTests(VerificationBase):
    def test_reopening_by_added_verification_tells_only_the_current_assignee(self):
        """AC-17: the current assignee only, not supervisors, the reporter or earlier assignees."""
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        earlier, current = self.make_user("earlier"), self.make_user("current")
        for person in (earlier, current):
            assign_deficiency(
                deficiency_id=deficiency_id,
                assignee_id=str(person.pk),
                expected_event_id=str(self.last_event(deficiency_id).pk),
                **self.context(self.supervisor),
            )
        self.work(deficiency_id)
        self.correct(deficiency_id)
        self.verify(deficiency_id)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        add_verification(
            deficiency_id=deficiency_id,
            expected_event_id=str(self.last_event(deficiency_id).pk),
            explanation="Synthetic: verify the mount too.",
            items=[{"item_id": str(self.masks.pk)}],
            deadline=timezone.now() + timedelta(days=2),
            **self.context(self.supervisor),
        )
        event = self.last_event(deficiency_id)
        self.assertEqual(event.event_type, "VERIFICATION_ADDED")
        self.assertEqual(notified("deficiency.reopened_verification_added", event.pk), [current.pk])


class OverdueTests(NoticeCase):
    def setUp(self):
        super().setUp()
        self.assign(self.fixer)
        self.due = timezone.now() + timedelta(hours=1)
        set_deadline(deadline=self.due, **self.args())

    def run_pass(self, hours=2, **kw):
        return overdue.notify_overdue(
            str(self.org.pk), now=timezone.now() + timedelta(hours=hours), **kw
        )

    def test_notified_once_per_governing_deadline_to_assignee_and_supervisors(self):
        self.assertEqual(self.run_pass(hours=0).notified, 0)  # not yet overdue
        report = self.run_pass()
        self.assertEqual((report.notified, report.deficiency_ids), (1, [self.deficiency_id]))
        governing = self.last().pk
        expected = sorted([self.fixer.pk, self.supervisor.pk])
        self.assertEqual(notified("deficiency.deadline_overdue", governing), expected)
        receipt = CommandReceipt.objects.get(command_type="DEFICIENCY_OVERDUE_NOTICE")
        self.assertEqual(
            (receipt.actor_user_id, receipt.service_identity),
            (None, "worker.deficiency_overdue"),
        )
        again = self.run_pass(hours=3)
        self.assertEqual((again.notified, again.already), (0, 1))
        self.assertEqual(len(notified("deficiency.deadline_overdue")), 2)
        # A new governing deadline that passes in turn is a new notice.
        set_deadline(deadline=timezone.now() + timedelta(hours=4), **self.args())
        self.assertEqual(self.run_pass(hours=3).notified, 0)
        self.assertEqual(self.run_pass(hours=5).notified, 1)
        self.assertEqual(notified("deficiency.deadline_overdue", self.last().pk), expected)

    def test_an_active_acknowledgement_suppresses_it_until_it_expires(self):
        acknowledgement.set_acknowledgement_maximum(maximum_days=3, **self.context(self.admin))
        acknowledgement.acknowledge_escalation(
            explanation="Synthetic review.",
            action_plan="Synthetic plan.",
            period_days=1,
            **self.args(),
        )
        report = self.run_pass()
        self.assertEqual((report.notified, report.suppressed), (0, 1))
        self.assertFalse(CommandReceipt.objects.filter(command_type="DEFICIENCY_OVERDUE_NOTICE"))
        self.assertEqual(self.run_pass(hours=30).notified, 1)

    def test_a_closed_deficiency_and_a_failed_write_are_not_notified(self):
        with mock.patch.object(
            NotificationOutbox.objects, "bulk_create", side_effect=DatabaseError("synthetic")
        ):
            report = self.run_pass()
        self.assertEqual((report.notified, report.errors), (0, 1))
        self.assertFalse(CommandReceipt.objects.filter(command_type="DEFICIENCY_OVERDUE_NOTICE"))
        close_administrator(explanation="Synthetic closure.", **self.args(actor=self.admin))
        self.assertEqual(self.run_pass().notified, 0)
        self.assertEqual(notified("deficiency.deadline_overdue"), [])

    def test_an_extension_past_now_never_holds_back_a_later_overdue_deficiency(self):
        # PR 30 review: an extended deficiency's base deadline has passed but its governing one
        # has not; it is not a candidate, so it cannot fill a page ahead of a later overdue one.
        request = request_extension(
            reason=REASON,
            proposed_deadline=timezone.now() + timedelta(hours=10),
            **self.args(actor=self.fixer),
        ).body["event_id"]
        decide_extension(
            request_event_id=request,
            decision="APPROVED",
            explanation="Synthetic approval.",
            deadline=None,
            **self.args(),
        )
        later = timezone.now() + timedelta(hours=1, minutes=30)
        set_deadline(deadline=later, **self.args(deficiency_id=self.supply_id))
        with mock.patch.object(overdue, "PAGE", 1):
            report = self.run_pass(limit=1)
        self.assertEqual(
            (report.notified, report.deficiency_ids, report.already, report.skipped),
            (1, [self.supply_id], 0, 0),
        )
        self.assertEqual(self.run_pass(hours=11).deficiency_ids, [self.deficiency_id])

    def test_already_notified_deficiencies_never_hold_back_a_newly_overdue_one(self):
        self.assertEqual(self.run_pass().deficiency_ids, [self.deficiency_id])
        later = timezone.now() + timedelta(hours=3)
        set_deadline(deadline=later, **self.args(deficiency_id=self.supply_id))
        with mock.patch.object(overdue, "PAGE", 1):
            report = self.run_pass(hours=4, limit=1)
        self.assertEqual(
            (report.notified, report.already, report.deficiency_ids), (1, 1, [self.supply_id])
        )

    def test_the_limit_is_checked(self):
        for limit in (0, 501, True, "5"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.run_pass(limit=limit)
