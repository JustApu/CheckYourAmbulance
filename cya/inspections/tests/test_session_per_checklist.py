"""One session in progress per vehicle checklist (SPEC 6.0.0 SES-7, SES-3; AC-41 (1)-(3)).

Issue #256 Part D. AC-41 (2), two simultaneous starts producing one session, is
test_commands.SessionCommandsTests.test_simultaneous_default_opens_share_one_session; the
start-racing-a-reopen case is here.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, local
from unittest import mock

from django.db import DatabaseError
from django.utils import timezone

from cya.checklists.models import Vehicle
from cya.core.models import AuditEvent
from cya.inspections import commands
from cya.inspections.commands import SessionError
from cya.inspections.models import InspectionSession, SessionEvent
from cya.inspections.projections import current_state
from cya.inspections.tests import test_activity, test_commands

IN_PROGRESS = "SESSION_IN_PROGRESS"


def nothing_in_progress(*_args, **_kwargs):
    return iter(())


class SessionPerChecklistTests(test_commands.SessionCase):
    def in_progress(self):
        return [
            str(session.pk)
            for session, _state in commands._in_progress(
                str(self.org.pk), self.vehicle.pk, self.template.pk, timezone.now()
            )
        ]

    def test_any_role_joins_the_session_in_progress_even_when_it_pins_an_earlier_version(self):
        opened = self.open()
        session_id = opened.body["session_id"]
        v2 = self.published_version()
        self.assertNotEqual(v2, self.v1)
        for actor in (self.supervisor, self.admin, self.crew):
            with self.subTest(actor=actor.idp_subject):
                joined = self.open(actor=actor)
                self.assertEqual(joined.body["session_id"], session_id)
                self.assertEqual(joined.body["checklist_version_id"], self.v1)
        self.assertEqual(InspectionSession.objects.count(), 1)
        self.assertEqual(self.in_progress(), [session_id])

    def test_reopen_is_refused_while_another_session_is_in_progress(self):
        ended = self.open().body["session_id"]
        self.end(ended, state="INCOMPLETE")
        current = self.open(actor=self.supervisor)
        self.assertEqual(current.body["code"], "SESSION_OPENED")
        events = SessionEvent.objects.count()
        with self.assertRaises(SessionError) as refused:
            self.reopen(ended)
        self.assertEqual(refused.exception.code, IN_PROGRESS)
        # A refused reopen changes nothing and is audited as a refused command (SES-3).
        self.assertEqual(SessionEvent.objects.count(), events)
        self.assertEqual(current_state(InspectionSession.objects.get(pk=ended)).state, "INCOMPLETE")
        self.assertTrue(
            AuditEvent.objects.filter(action="SESSION_REOPEN", outcome="FAILED").exists()
        )
        # Once that session ends, the participant may reopen theirs.
        self.end(current.body["session_id"], actor=self.supervisor)
        self.assertEqual(self.reopen(ended).body["code"], "SESSION_REOPENED")
        self.assertEqual(self.in_progress(), [ended])

    def test_another_checklists_session_does_not_block_a_reopen(self):
        other_template, _version = self.other_checklist()
        ended = self.open().body["session_id"]
        self.end(ended)
        self.open(template=other_template, actor=self.supervisor)
        self.assertEqual(self.reopen(ended).body["code"], "SESSION_REOPENED")

    def other_checklist(self):
        from cya.checklists import commands as checklist_commands
        from cya.checklists.models import ChecklistTemplate

        template = ChecklistTemplate.objects.create(
            organization=self.org, label="Synthetic other checklist"
        )
        version = checklist_commands.save_template_draft(
            template_id=str(template.pk), items=self.base_items(), **self.context(self.admin)
        ).body["version_id"]
        checklist_commands.publish_template_version(version_id=version, **self.context(self.admin))
        return template, version

    def test_a_start_racing_a_reopen_leaves_one_in_progress(self):
        ended = self.open().body["session_id"]
        self.end(ended)
        meeting = Barrier(2)

        def reopen():
            meeting.wait(timeout=10)
            try:
                return self.reopen(ended).body["session_id"]
            except SessionError as error:
                return error.code

        def start():
            meeting.wait(timeout=10)
            return self.open(actor=self.supervisor).body["session_id"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            reopened, started = pool.submit(reopen), pool.submit(start)
            reopened, started = reopened.result(), started.result()
        in_progress = self.in_progress()
        self.assertEqual(len(in_progress), 1)
        if reopened == IN_PROGRESS:  # the start won: a new session; the reopen was refused
            self.assertNotEqual(started, ended)
            self.assertEqual(in_progress, [started])
        else:  # the reopen won: the start joined it
            self.assertEqual((reopened, started, in_progress), (ended, ended, [ended]))

    def test_the_database_refuses_a_second_session_in_progress(self):
        session_id = self.open().body["session_id"]
        # A writer that skips the command's check: the trigger refuses an OPEN...
        with (
            mock.patch.object(commands, "_in_progress", nothing_in_progress),
            self.assertRaises(DatabaseError),
        ):
            self.open(actor=self.supervisor)
        self.assertEqual(InspectionSession.objects.count(), 1)
        # ...and a REOPEN of another session of the same checklist.
        self.end(session_id)
        other = self.open(actor=self.supervisor).body["session_id"]
        with (
            mock.patch.object(commands, "_in_progress", nothing_in_progress),
            self.assertRaises(DatabaseError),
        ):
            self.reopen(session_id)
        self.assertEqual(
            current_state(InspectionSession.objects.get(pk=session_id)).state, "CLOSED"
        )
        self.assertEqual(self.in_progress(), [other])

    def test_the_database_serializes_concurrent_writers_that_skip_the_commands(self):
        # Two ended sessions of one checklist, reopened at once by writers that neither check
        # for a session in progress nor take the vehicle lock: only the trigger stands between
        # them, and its own vehicle lock makes the second see the first's committed REOPEN.
        first = self.open().body["session_id"]
        self.end(first)
        second = self.open(actor=self.supervisor).body["session_id"]
        self.end(second, actor=self.supervisor)
        built, inserted = Barrier(2), Barrier(2)
        waited = local()
        original_event = commands._event

        def event_after_both_built(session, *args, **kwargs):
            # Both writers insert their REOPEN while the other's transaction is still open:
            # each waits for the other to have inserted too before it may commit. Without the
            # trigger's vehicle lock both inserts pass and both commit. With it the second
            # insert blocks inside the trigger, so the first stops waiting after a second,
            # commits, and the second then sees its REOPEN and is refused.
            first_time = not getattr(waited, "done", False)
            waited.done = True
            if first_time:
                built.wait(timeout=10)
            event = original_event(session, *args, **kwargs)
            if first_time:
                try:
                    inserted.wait(timeout=1)
                except BrokenBarrierError:
                    pass
            return event

        def unlocked_vehicle(org, vehicle_id):
            return Vehicle.objects.filter(organization_id=org, pk=vehicle_id).first()

        def reopen(session_id, actor):
            try:
                return self.reopen(session_id, actor=actor).body["code"]
            except DatabaseError:
                return "REFUSED"

        with (
            mock.patch.object(commands, "_in_progress", nothing_in_progress),
            mock.patch.object(commands, "_vehicle", unlocked_vehicle),
            mock.patch.object(commands, "_event", event_after_both_built),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = [
                pool.submit(reopen, first, self.crew),
                pool.submit(reopen, second, self.supervisor),
            ]
            outcomes = sorted(result.result() for result in results)
        self.assertEqual(outcomes, ["REFUSED", "SESSION_REOPENED"])
        self.assertEqual(len(self.in_progress()), 1)

    def test_the_database_allows_sessions_of_different_checklists_together(self):
        other_template, _version = self.other_checklist()
        self.open()
        with mock.patch.object(commands, "_in_progress", nothing_in_progress):
            opened = self.open(template=other_template, actor=self.supervisor)
        self.assertEqual(opened.body["code"], "SESSION_OPENED")


class IdleSessionTests(test_activity.ActivityCase):
    """A session idle for the inactivity limit has ended (SES-1): it is no longer in progress."""

    def test_an_idle_session_neither_absorbs_a_start_nor_blocks_a_reopen(self):
        self.advance(hours=6)
        # The job has not run: the idle session still reads Open in its history.
        self.assertEqual(self.events()[-1].event_type, "OPEN")
        started = self.open(actor=self.supervisor)
        self.assertEqual(started.body["code"], "SESSION_OPENED")
        self.assertNotEqual(started.body["session_id"], self.session_id)
        commands.end_session(session_id=started.body["session_id"], **self.context(self.supervisor))
        reopened = commands.reopen_session(session_id=self.session_id, **self.context(self.crew))
        self.assertEqual(reopened.body["code"], "SESSION_REOPENED")

    def test_the_database_judges_idleness_from_activity_including_checks(self):
        self.advance(hours=5)
        self.check()  # activity: the session stays in progress six hours from here
        self.advance(hours=2)
        with (
            mock.patch.object(commands, "_in_progress", nothing_in_progress),
            self.assertRaises(DatabaseError),
        ):
            self.open(actor=self.supervisor)
        self.advance(hours=4)
        with mock.patch.object(commands, "_in_progress", nothing_in_progress):
            self.assertEqual(self.open(actor=self.supervisor).body["code"], "SESSION_OPENED")
