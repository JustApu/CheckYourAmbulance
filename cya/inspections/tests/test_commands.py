"""Session command, audit, pinning and state tests (issue 78 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) Part A)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import mock

from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext

from cya.accounts.models import Organization, User
from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistTemplate, Vehicle
from cya.checklists.tests.support import ChecklistCase, ids
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.inspections import commands
from cya.inspections.commands import SessionError
from cya.inspections.models import (
    InspectionSession,
    SessionEvent,
    SessionParticipant,
    SessionProjection,
)
from cya.inspections.projections import (
    current_state,
    fold_session,
    read_session,
    rebuild_session_projection,
)
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.tests.support import ObligationCase


class SessionCase(ChecklistCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.published_version()

    @property
    def vehicle(self):
        return self.vehicles[0]

    def open(self, *, actor=None, vehicle=None, template=None, separate=False, **context):
        return commands.open_or_join_session(
            vehicle_id=str((vehicle or self.vehicle).pk),
            template_id=str((template or self.template).pk),
            separate=separate,
            **(context or self.context(actor or self.crew)),
        )

    def reopen(self, session_id, *, actor=None, **context):
        return commands.reopen_session(
            session_id=session_id, **(context or self.context(actor or self.crew))
        )

    def end(self, session_id, *, actor=None, state="CLOSED", **context):
        return commands.end_session(
            session_id=session_id, state=state, **(context or self.context(actor or self.crew))
        )


class SessionCommandsTests(SessionCase):
    def test_default_open_does_not_scan_closed_session_histories(self):
        def close_sessions(count):
            for _ in range(count):
                session_id = self.open(separate=True).body["session_id"]
                self.end(session_id)

        close_sessions(2)
        with CaptureQueriesContext(connection) as first_queries:
            first = self.open()
        self.end(first.body["session_id"])
        close_sessions(10)
        with CaptureQueriesContext(connection) as later_queries:
            later = self.open()
        self.assertEqual(later.body["code"], "SESSION_OPENED")
        self.assertEqual(len(later_queries), len(first_queries))

    def test_default_join_finds_older_open_session_past_closed_newer_one(self):
        older = self.open()
        newer = self.open(separate=True)
        self.end(newer.body["session_id"])
        joined = self.open(actor=self.supervisor)
        self.assertEqual(joined.body["session_id"], older.body["session_id"])

    def test_simultaneous_default_opens_share_one_session(self):
        meeting = Barrier(2)
        contexts = [self.context(self.crew), self.context(self.supervisor)]

        def open_after_barrier(context):
            meeting.wait(timeout=10)
            return self.open(**context)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(open_after_barrier, contexts))
        self.assertEqual({r.body["session_id"] for r in results}, {results[0].body["session_id"]})
        self.assertEqual(InspectionSession.objects.count(), 1)
        self.assertEqual(SessionParticipant.objects.count(), 2)
        self.assertEqual(SessionEvent.objects.count(), 2)

    def test_open_join_and_explicit_parallel_session(self):
        opened = self.open()
        session_id = opened.body["session_id"]
        session = InspectionSession.objects.get(pk=session_id)
        self.assertEqual(session.checklist_version_id, self.v1)
        self.assertEqual(session.vehicle_id, self.vehicle.pk)
        self.assertEqual(current_state(session).state, "OPEN")
        first_activity = current_state(session).last_activity_at

        joined = self.open(actor=self.supervisor)
        self.assertEqual(joined.body["session_id"], session_id)
        self.assertEqual(joined.body["code"], "SESSION_JOINED")
        self.assertEqual(SessionParticipant.objects.filter(session=session).count(), 2)
        self.assertEqual(
            [
                e.event_type
                for e in SessionEvent.objects.filter(session=session).order_by("sequence")
            ],
            ["OPEN", "JOIN"],
        )
        self.assertEqual(current_state(session).last_activity_at, first_activity)

        already = self.open(actor=self.supervisor)
        self.assertEqual(already.body["code"], "ALREADY_PARTICIPANT")
        self.assertEqual(SessionParticipant.objects.filter(session=session).count(), 2)
        self.assertEqual(SessionEvent.objects.filter(session=session).count(), 2)

        separate = self.open(actor=self.supervisor, separate=True)
        self.assertNotEqual(separate.body["session_id"], session_id)
        self.assertEqual(InspectionSession.objects.filter(vehicle=self.vehicle).count(), 2)
        self.assertEqual(
            self.open(actor=self.admin).body["session_id"], separate.body["session_id"]
        )

    def test_new_session_can_start_while_another_is_open_closed_or_incomplete(self):
        for prior_state in ("OPEN", "CLOSED", "INCOMPLETE"):
            with self.subTest(prior_state=prior_state):
                first = self.open(separate=True)
                if prior_state != "OPEN":
                    self.end(first.body["session_id"], state=prior_state)
                second = self.open(actor=self.supervisor, separate=True)
                self.assertNotEqual(first.body["session_id"], second.body["session_id"])
                self.assertEqual(
                    current_state(InspectionSession.objects.get(pk=first.body["session_id"])).state,
                    prior_state,
                )

    def test_default_join_is_vehicle_scoped_even_when_requested_template_differs(self):
        other_template = ChecklistTemplate.objects.create(
            organization=self.org, label="Synthetic alternate checklist"
        )
        other_version = checklist_commands.save_template_draft(
            template_id=str(other_template.pk), items=self.base_items(), **self.context(self.admin)
        ).body["version_id"]
        checklist_commands.publish_template_version(
            version_id=other_version, **self.context(self.admin)
        )
        first = self.open()
        joined = self.open(template=other_template, actor=self.supervisor)
        self.assertEqual(joined.body["session_id"], first.body["session_id"])
        self.assertEqual(joined.body["checklist_version_id"], self.v1)

        separate = self.open(template=other_template, separate=True)
        self.assertNotEqual(separate.body["session_id"], first.body["session_id"])
        self.assertEqual(separate.body["checklist_version_id"], other_version)

    def test_reopen_keeps_original_pin_and_refreshes_current_obligations(self):
        session_id = self.open().body["session_id"]
        self.open(actor=self.supervisor)
        self.end(session_id, state="INCOMPLETE")
        self.assertEqual(
            current_state(InspectionSession.objects.get(pk=session_id)).state, "INCOMPLETE"
        )
        v2 = self.published_version()
        self.assertNotEqual(v2, self.v1)

        resumed = self.reopen(session_id, actor=self.supervisor)
        self.assertEqual(resumed.body["checklist_version_id"], self.v1)
        self.assertTrue(resumed.body["refresh_requirements"])
        session = InspectionSession.objects.get(pk=session_id)
        self.assertEqual(session.checklist_version_id, self.v1)
        self.assertEqual(current_state(session).state, "OPEN")
        self.assertEqual(SessionParticipant.objects.filter(session=session).count(), 2)
        reopen_event = SessionEvent.objects.get(session=session, event_type="REOPEN")
        self.assertEqual(reopen_event.actor_user_id, self.supervisor.pk)
        self.assertEqual(reopen_event.persisted_activity_command_id, reopen_event.command_id)
        self.assertEqual(current_state(session).last_activity_at, reopen_event.effective_at)
        self.assertTrue(
            AuditEvent.objects.filter(action="SESSION_REOPEN", outcome="SUCCEEDED").exists()
        )
        with mock.patch(
            "cya.inspections.projections.get_vehicle_obligations_as_of", return_value=("fresh",)
        ) as projection:
            observed, state, obligations = read_session(str(self.org.pk), session_id)
        self.assertEqual(observed.checklist_version_id, self.v1)
        self.assertEqual(state.state, "OPEN")
        self.assertEqual(obligations, ("fresh",))
        self.assertEqual(projection.call_args.kwargs["organization_id"], str(self.org.pk))

    def test_explicit_end_is_informational_and_reopen_closed(self):
        session_id = self.open().body["session_id"]
        self.end(session_id)
        session = InspectionSession.objects.get(pk=session_id)
        self.assertEqual(current_state(session).state, "CLOSED")
        self.assertFalse(hasattr(session, "state"))
        self.assertFalse(hasattr(session, "completed"))
        self.reopen(session_id)
        self.assertEqual(current_state(session).state, "OPEN")
        self.assertEqual(InspectionSession.objects.count(), 1)

    def test_replay_is_stable_and_does_not_append(self):
        context = self.context(self.crew)
        first = self.open(**context)
        second = self.open(**context)
        self.assertTrue(second.replayed)
        self.assertEqual(second.body, first.body)
        self.assertEqual(SessionEvent.objects.count(), 1)
        self.assertEqual(SessionParticipant.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(action="SESSION_OPEN_OR_JOIN", outcome="SUCCEEDED").count(), 1
        )

    def test_denial_and_invalid_intent_leave_no_session_writes(self):
        disabled = self.make_user("disabled")
        User.objects.filter(pk=disabled.pk).update(active=False)
        with self.assertRaises(CommandDenied):
            self.open(actor=disabled)
        self.assertEqual(
            AuditEvent.objects.filter(action="SESSION_OPEN_OR_JOIN", outcome="DENIED").count(), 1
        )
        self.assertFalse(InspectionSession.objects.exists())

        draft_only = ChecklistTemplate.objects.create(organization=self.org, label="Draft only")
        with self.assertRaises(SessionError):
            self.open(template=draft_only)
        self.assertFalse(InspectionSession.objects.exists())

    def test_wrong_vehicle_template_and_actor_scope_fail_closed(self):
        other_org = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign_vehicle = Vehicle.objects.create(organization=other_org, display_name="Foreign")
        foreign_template = ChecklistTemplate.objects.create(organization=other_org, label="Foreign")
        for kwargs in (
            {"vehicle": foreign_vehicle},
            {"template": foreign_template},
        ):
            with self.assertRaises(SessionError):
                self.open(**kwargs)
        self.assertFalse(InspectionSession.objects.exists())
        with self.assertRaises(User.DoesNotExist):
            commands.open_or_join_session(
                vehicle_id=str(self.vehicle.pk),
                template_id=str(self.template.pk),
                organization_id=str(other_org.pk),
                actor_id=str(self.crew.pk),
                **ids(),
            )

    def test_unavailable_and_nonparticipant_sessions_have_the_same_denial(self):
        session_id = self.open().body["session_id"]
        for target in (session_id, "00000000-0000-4000-8000-000000000000"):
            with self.assertRaises(CommandDenied):
                self.reopen(target, actor=self.supervisor)
        denied = AuditEvent.objects.filter(action="SESSION_REOPEN", outcome="DENIED")
        self.assertEqual(denied.count(), 2)
        self.assertFalse(
            AuditEvent.objects.filter(action="SESSION_REOPEN", outcome="FAILED").exists()
        )

    def test_only_participant_may_end_and_invalid_transitions_are_audited(self):
        session_id = self.open().body["session_id"]
        with self.assertRaises(CommandDenied):
            self.end(session_id, actor=self.supervisor)
        self.end(session_id)
        with self.assertRaises(CommandDenied):
            self.reopen(session_id, actor=self.supervisor)
        self.reopen(session_id)
        with self.assertRaises(SessionError):
            self.reopen(session_id)
        self.end(session_id)
        with self.assertRaises(SessionError):
            self.end(session_id)
        self.assertEqual(SessionEvent.objects.filter(session_id=session_id).count(), 4)
        self.assertEqual(
            AuditEvent.objects.filter(action="SESSION_END", outcome="DENIED").count(), 1
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="SESSION_REOPEN", outcome="DENIED").count(), 1
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="SESSION_REOPEN", outcome="FAILED").count(), 1
        )

    def test_projection_is_rebuildable_from_immutable_history(self):
        session_id = self.open().body["session_id"]
        self.end(session_id)
        session = InspectionSession.objects.get(pk=session_id)
        history = list(SessionEvent.objects.filter(session=session).order_by("sequence"))
        self.assertEqual(fold_session(history), current_state(session))
        SessionProjection.objects.filter(session=session).delete()
        self.assertFalse(SessionProjection.objects.filter(session=session).exists())
        with transaction.atomic():
            Vehicle.objects.select_for_update().get(pk=self.vehicle.pk)
            rebuilt = rebuild_session_projection(session)
        self.assertEqual(rebuilt.state, "CLOSED")
        self.assertEqual(SessionProjection.objects.get(session=session).state, "CLOSED")

    def test_header_and_evidence_are_database_immutable(self):
        session_id = self.open().body["session_id"]
        session = InspectionSession.objects.get(pk=session_id)
        for write in (
            lambda: InspectionSession.objects.filter(pk=session_id).update(
                vehicle_id=self.vehicles[1].pk
            ),
            lambda: SessionEvent.objects.filter(session=session).update(event_type="REOPEN"),
            lambda: SessionParticipant.objects.filter(session=session).delete(),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                write()
        self.assertEqual(current_state(session).state, "OPEN")
        self.assertEqual(SessionParticipant.objects.filter(session=session).count(), 1)

    def test_session_rows_require_matching_receipt_command_type(self):
        session_id = self.open().body["session_id"]
        self.open(actor=self.supervisor)
        session = InspectionSession.objects.get(pk=session_id)
        original = session.command
        prior = SessionEvent.objects.filter(session=session).order_by("-sequence").first()

        def wrong_receipt(actor):
            return CommandReceipt.objects.create(
                organization=self.org,
                operation_id=ids()["operation_id"],
                actor_user=actor,
                command_type="ROLE_CHANGE",
                request_digest="0" * 64,
                result_schema_version=original.result_schema_version,
                response_status=original.response_status,
                response_body=original.response_body,
                recorded_at=prior.recorded_at,
                correlation_id=ids()["correlation_id"],
            )

        wrong = wrong_receipt(self.supervisor)
        wrong_participant = wrong_receipt(self.admin)

        def refused(write, message):
            with self.assertRaises(DatabaseError) as caught, transaction.atomic():
                write()
            self.assertIn(message, str(caught.exception))

        refused(
            lambda: InspectionSession.objects.create(
                organization=self.org,
                vehicle=self.vehicle,
                checklist_version_id=self.v1,
                opened_at=session.opened_at,
                command=wrong,
                recorded_at=wrong.recorded_at,
                actor_user=self.supervisor,
                actor_roles_at_time=["SUPERVISOR"],
            ),
            "session receipt attribution mismatch",
        )
        refused(
            lambda: SessionParticipant.objects.create(
                organization=self.org,
                session=session,
                user=self.admin,
                command=wrong_participant,
                recorded_at=wrong_participant.recorded_at,
                actor_user=self.admin,
                actor_roles_at_time=["ADMINISTRATOR"],
            ),
            "participant attribution mismatch",
        )
        refused(
            lambda: SessionEvent.objects.create(
                organization=self.org,
                session=session,
                sequence=3,
                prior_event=prior,
                event_type="JOIN",
                effective_at=wrong.recorded_at,
                command=wrong,
                recorded_at=wrong.recorded_at,
                actor_user=self.supervisor,
                actor_roles_at_time=["SUPERVISOR"],
            ),
            "session event receipt mismatch",
        )


class SessionComplianceTests(ObligationCase):
    def test_explicit_close_does_not_satisfy_unchecked_item_obligations(self):
        self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])
        before = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), self.clock.now, organization_id=str(self.org.pk)
        )
        self.assertTrue(before)

        opened = commands.open_or_join_session(
            vehicle_id=str(self.vehicle.pk),
            template_id=str(self.template.pk),
            **self.context(self.crew),
        )
        commands.end_session(session_id=opened.body["session_id"], **self.context(self.crew))
        after = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), self.clock.now, organization_id=str(self.org.pk)
        )
        self.assertEqual(
            [(r.obligation.id, r.state) for r in after],
            [(r.obligation.id, r.state) for r in before],
        )
        self.assertEqual(
            current_state(InspectionSession.objects.get(pk=opened.body["session_id"])).state,
            "CLOSED",
        )

    def test_auto_close_does_not_satisfy_unchecked_item_obligations(self):
        """AC-2 for the job: an Incomplete session leaves every obligation as it was."""
        self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])
        instant = self.clock.now
        before = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), instant, organization_id=str(self.org.pk)
        )
        self.assertTrue(before)

        opened = commands.open_or_join_session(
            vehicle_id=str(self.vehicle.pk),
            template_id=str(self.template.pk),
            **self.context(self.crew),
        )
        self.clock.set(self.clock.now + timedelta(hours=7))
        self.assertEqual(commands.auto_close_inactive_sessions()["closed"], 1)
        after = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), instant, organization_id=str(self.org.pk)
        )
        self.assertEqual(
            [(r.obligation.id, r.state) for r in after],
            [(r.obligation.id, r.state) for r in before],
        )
        self.assertEqual(
            current_state(InspectionSession.objects.get(pk=opened.body["session_id"])).state,
            "INCOMPLETE",
        )
