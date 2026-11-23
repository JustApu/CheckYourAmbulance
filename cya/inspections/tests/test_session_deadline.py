"""Session resume limit and deadline (SPEC 6.0.0 SES-2, SES-8, SES-9; AC-6, AC-41 (4)-(6), (8)).

Issue #256 Part E. The end instant is E = min(A + 6 h, R*) with R* = max(opened_at + captured
limit, A); A is the last persisted activity (the latest OPEN or REOPEN, or the latest check in
the session; JOIN and END are not activity).
"""

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, transaction
from django.urls import reverse
from django.utils import timezone

from cya.attention.checklist import vehicle_checklist
from cya.checks.models import CheckRecord
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.inspections import commands, projections, resume_limit
from cya.inspections.commands import SessionError, auto_close_session
from cya.inspections.models import InspectionSession, SessionEvent, SessionResumeLimit
from cya.inspections.projections import resume_deadline
from cya.inspections.tests.test_activity import HOUR, ActivityCase
from cya.inspections.tests.test_crew_ui import CrewUiCase

TOKEN = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


class DeadlineCase(ActivityCase):
    resume_limit_hours = None  # the organization's default, 6 hours

    def session(self, session_id=None):
        return InspectionSession.objects.get(pk=session_id or self.session_id)

    def opened_at(self, session_id=None):
        return self.session(session_id).opened_at

    def reopen(self, session_id=None, actor=None):
        return commands.reopen_session(
            session_id=session_id or self.session_id, **self.context(actor or self.crew)
        )

    def end(self, session_id=None, actor=None):
        return commands.end_session(
            session_id=session_id or self.session_id, **self.context(actor or self.crew)
        )

    def refused_reopen(self, code, session_id=None):
        with self.assertRaises(SessionError) as caught:
            self.reopen(session_id)
        self.assertEqual(caught.exception.code, code)

    def refused_check(self, code, **kwargs):
        from cya.checks.commands import CheckError

        before = CheckRecord.objects.count()
        with self.assertRaises(CheckError) as caught:
            self.check(**kwargs)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, 409))
        self.assertEqual(CheckRecord.objects.count(), before)


class DefaultLimitTests(DeadlineCase):
    """AC-41(4): with the default 6-hour limit an Open session ends at opened_at + 6 h."""

    def test_the_captured_limit_and_deadline_are_on_the_session(self):
        session = self.session()
        self.assertEqual(session.resume_limit_hours, 6)
        self.assertEqual(self.state().resume_deadline_at, session.opened_at + 6 * HOUR)

    def test_an_active_session_ends_at_its_deadline_without_waiting_for_the_job(self):
        deadline = self.opened_at() + 6 * HOUR
        for _ in range(5):  # in active use: a check every hour
            self.advance(hours=1)
            self.assertEqual(self.check().status, 201)
        self.advance(minutes=59)
        self.assertEqual(self.check(item=self.masks, quantity=6).status, 201)  # just before R
        self.clock.set(deadline)
        state = self.state()
        self.assertEqual(state.state, "OPEN")  # the job has not run ...
        self.assertTrue(projections.is_expired(state, deadline))  # ... but it reads as ended
        self.assertEqual(state.ends_at, deadline)
        self.refused_check("SESSION_DEADLINE_PASSED")
        # a start of the checklist does not join it: it opens a new session
        started = self.open(actor=self.supervisor)
        self.assertEqual(started.body["code"], "SESSION_OPENED")
        self.assertNotEqual(started.body["session_id"], self.session_id)
        # the job runs late, twice, and records one ending at exactly R
        self.advance(hours=3)
        self.assertEqual(self.close_job()["at_deadline"], 1)
        self.assertEqual(self.close_job()["closed"], 0)
        endings = [e for e in self.events() if e.event_type == "DEADLINE_INCOMPLETE"]
        self.assertEqual(len(endings), 1)
        self.assertEqual(endings[0].effective_at, deadline)
        self.assertIsNone(endings[0].actor_user_id)
        self.assertEqual(self.state().state, "INCOMPLETE")
        self.refused_reopen("SESSION_DEADLINE_PASSED")
        self.assertTrue(
            AuditEvent.objects.filter(action="SESSION_REOPEN", outcome="FAILED").exists()
        )

    def test_reopen_and_join_are_refused_from_the_deadline_by_anyone(self):
        self.end()
        self.advance(hours=6)
        self.refused_reopen("SESSION_DEADLINE_PASSED")
        self.assertEqual(self.state().state, "CLOSED")  # a refusal changes nothing

    def test_only_a_session_ended_by_hand_can_be_reopened_and_never_past_r(self):
        self.advance(hours=2)
        self.end()
        self.advance(hours=1)
        self.assertEqual(self.reopen().body["code"], "SESSION_REOPENED")
        reopened = self.state()
        # the reopen's six-hour window is cut at R (SES-2)
        self.assertEqual(reopened.ends_at, self.opened_at() + 6 * HOUR)
        self.advance(hours=3)  # R
        self.assertEqual(self.close_job()["at_deadline"], 1)
        self.assertEqual(self.events()[-1].event_type, "DEADLINE_INCOMPLETE")

    def test_an_untouched_session_closes_for_inactivity_exactly_at_r(self):
        # A + 6 h equals R: the inactivity close applies (SES-9: A + 6 h <= R*)
        self.advance(hours=6, minutes=1)
        self.assertEqual(
            self.close_job(), {"closed": 1, "at_deadline": 0, "skipped": 0, "failed": 0}
        )
        self.assertEqual(self.events()[-1].event_type, "AUTO_INCOMPLETE")
        self.assertEqual(self.events()[-1].effective_at, self.opened_at() + 6 * HOUR)
        self.refused_reopen("SESSION_DEADLINE_PASSED")

    def test_after_the_deadline_a_new_session_shows_the_earlier_checks(self):
        """AC-41(8): a new session shows the earlier checks through current obligations."""
        recorded = self.check().body["check_id"]
        before = list(CheckRecord.objects.filter(session_id=self.session_id).values())
        self.advance(hours=6)
        new_id = self.open().body["session_id"]
        self.assertNotEqual(new_id, self.session_id)
        rows = vehicle_checklist(
            str(self.org.pk), str(self.vehicle.pk), session=self.session(new_id)
        ).rows
        suction = next(r for r in rows if r.item_id == str(self.suction.pk))
        self.assertEqual(suction.latest_check_id, recorded)
        # the earlier session's records are unchanged and readable
        self.assertEqual(
            list(CheckRecord.objects.filter(session_id=self.session_id).values()), before
        )
        self.assertEqual(self.state().state, "OPEN")  # history: the job has not written yet

    def test_the_job_dry_run_lists_due_sessions_and_writes_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        self.advance(hours=7)
        out = StringIO()
        call_command("close_inactive_sessions", "--dry-run", stdout=out)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(lines[-1], "due=1")
        self.assertIn(f"session={self.session_id}", lines[0])
        self.assertEqual(self.events()[-1].event_type, "OPEN")


class LongLimitTests(DeadlineCase):
    """AC-41(6): a 12-hour limit; idle from 07:00 closes at 13:00, resumable until 19:00."""

    resume_limit_hours = 12

    def test_an_idle_closed_session_is_resumable_until_r_and_its_window_ends_there(self):
        seven = self.opened_at()
        self.advance(hours=6, minutes=30)
        self.assertEqual(self.close_job()["closed"], 1)
        self.assertEqual(self.events()[-1].event_type, "AUTO_INCOMPLETE")
        self.assertEqual(self.events()[-1].effective_at, seven + 6 * HOUR)  # 13:00
        self.clock.set(seven + 11 * HOUR)  # 18:00
        self.assertEqual(self.reopen().body["code"], "SESSION_REOPENED")
        self.assertEqual(self.state().ends_at, seven + 12 * HOUR)  # 19:00, not 00:00
        self.clock.set(seven + 12 * HOUR - HOUR / 60)  # 18:59
        self.assertEqual(self.check().status, 201)
        self.clock.set(seven + 12 * HOUR)  # 19:00
        self.refused_check("SESSION_DEADLINE_PASSED")
        self.assertEqual(self.close_job()["at_deadline"], 1)
        self.assertEqual(self.events()[-1].effective_at, seven + 12 * HOUR)
        self.refused_reopen("SESSION_DEADLINE_PASSED")

    def test_before_r_a_stale_session_cannot_be_reopened_while_another_is_in_progress(self):
        self.advance(hours=7)
        other = self.open(actor=self.supervisor).body["session_id"]
        self.assertNotEqual(other, self.session_id)
        self.refused_reopen("SESSION_IN_PROGRESS")


class ResumeLimitSettingTests(DeadlineCase):
    """AC-41(5): the setting, its permission, its range and capture at open."""

    def test_a_change_never_moves_an_existing_deadline_and_applies_to_new_sessions(self):
        before = resume_deadline(self.session())
        result = self.set_resume_limit(8)
        self.assertEqual(
            result.body, {"code": "SESSION_RESUME_LIMIT_SET", "revision": 1, "hours": 8}
        )
        self.assertEqual(resume_deadline(self.session()), before)
        self.assertEqual(self.state().resume_deadline_at, before)
        other = self.open(vehicle=self.vehicles[1]).body
        self.assertEqual(self.session(other["session_id"]).resume_limit_hours, 8)
        self.assertEqual(
            other["resume_deadline_at"],
            (self.opened_at(other["session_id"]) + 8 * HOUR).isoformat(),
        )
        self.assertEqual(resume_limit.resume_limit_hours(str(self.org.pk)), 8)

    def test_only_an_administrator_may_set_it(self):
        for actor in (self.crew, self.supervisor):
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                resume_limit.set_resume_limit(hours=8, **self.context(actor))
        self.assertFalse(SessionResumeLimit.objects.exists())
        self.assertEqual(
            AuditEvent.objects.filter(action="SESSION_RESUME_LIMIT_SET", outcome="DENIED").count(),
            2,
        )

    def test_values_outside_one_to_24_whole_hours_are_refused(self):
        for bad in (0, 25, -1, "6", 1.5, True, None):
            with self.subTest(bad=bad), self.assertRaises(resume_limit.ResumeLimitError) as caught:
                self.set_resume_limit(bad)
            self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        with self.assertRaises(resume_limit.ResumeLimitError) as same:
            self.set_resume_limit(6)
        self.assertEqual(same.exception.code, "NO_CHANGE")
        self.assertEqual(self.set_resume_limit(1).body["revision"], 1)
        self.assertEqual(self.set_resume_limit(24).body["revision"], 2)

    def test_the_limit_table_is_append_only_and_numbered_in_order(self):
        self.set_resume_limit(8)
        row = SessionResumeLimit.objects.get()
        for write in (
            lambda: SessionResumeLimit.objects.filter(pk=row.pk).update(hours=9),
            lambda: SessionResumeLimit.objects.filter(pk=row.pk).delete(),
            lambda: SessionResumeLimit.objects.create(
                organization=self.org,
                revision=3,
                hours=9,
                command=row.command,
                recorded_at=row.recorded_at,
                actor_user=row.actor_user,
                actor_roles_at_time=row.actor_roles_at_time,
            ),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                write()
        self.assertEqual(SessionResumeLimit.objects.get().hours, 8)

    def test_a_change_waits_for_an_open_in_flight_and_the_next_open_sees_it(self):
        """The open holds the organization row in share mode; the change takes it exclusively."""
        self.end()
        reading, release = Event(), Event()
        real = commands.resume_limit_hours

        def read_then_hold(org):
            value = real(org)
            reading.set()
            self.assertTrue(release.wait(timeout=20))
            return value

        changed = Event()

        def run_open():
            from unittest import mock

            with mock.patch.object(commands, "resume_limit_hours", read_then_hold):
                return self.open(actor=self.supervisor).body

        def run_change():
            self.assertTrue(reading.wait(timeout=20))
            result = self.set_resume_limit(9)
            changed.set()
            return result.body

        with ThreadPoolExecutor(max_workers=2) as pool:
            opened, change = pool.submit(run_open), pool.submit(run_change)
            self.assertTrue(reading.wait(timeout=20))
            self.assertFalse(changed.wait(timeout=1.5))  # blocked behind the open's share lock
            release.set()
            first = opened.result(timeout=60)
            self.assertEqual(change.result(timeout=60)["hours"], 9)
        self.assertEqual(self.session(first["session_id"]).resume_limit_hours, 6)
        self.end(first["session_id"], actor=self.supervisor)
        second = self.open(actor=self.supervisor).body
        self.assertEqual(self.session(second["session_id"]).resume_limit_hours, 9)


class ResumeLimitPageTests(CrewUiCase):
    def test_only_administrators_set_the_limit(self):
        url = reverse("inspection-session-settings")
        self.assertEqual(self.client.get(url).status_code, 403)  # a crew member
        admin, user = self.harness.sign_in("resume-admin")
        self.grant(user, "ADMINISTRATOR")
        page = admin.get(url)
        self.assertContains(page, 'value="6"')
        self.assertContains(admin.get(reverse("administration")), "Inspection sessions")
        token = TOKEN.search(page.content.decode()).group(1)
        saved = admin.post(
            url, {"csrfmiddlewaretoken": token, "operation_id": str(uuid4()), "hours": "8"}
        )
        self.assertEqual(saved["Location"], url + "?saved=1")
        self.assertContains(admin.get(saved["Location"]), 'value="8"')
        self.assertEqual(resume_limit.resume_limit_hours(str(self.org.pk)), 8)
        bad = admin.post(
            url, {"csrfmiddlewaretoken": token, "operation_id": str(uuid4()), "hours": "25"}
        )
        self.assertContains(bad, "Enter a whole number of hours from 1 to 24.", status_code=400)


class ExpiredSessionPageTests(CrewUiCase):
    """The session page is read-only from the deadline; the vehicle page offers Resume until R."""

    def test_an_ended_session_offers_reopen_until_r_then_reads_only(self):
        session_id = self.open().json()["result"]["session_id"]
        page_url = reverse("inspection-session", args=[session_id])
        vehicle_url = reverse("inspection-vehicle", args=[self.vehicle.pk])
        self.assertContains(self.client.get(page_url), 'id="session-deadline"')
        ended = self.post(
            reverse("api-inspection-session-end", args=[session_id]),
            {"operation_id": str(uuid4()), "state": "INCOMPLETE"},
        )
        self.assertEqual(ended.status_code, 200, ended.content)
        page = self.client.get(page_url)
        self.assertContains(page, 'id="reopen-session"')
        self.assertContains(self.client.get(vehicle_url), "can be resumed until")
        # The pages read the clock through their own module; the sign-in keeps the real one.
        later = timezone.now() + 7 * HOUR
        with mock.patch("cya.inspections.views.timezone") as clock:
            clock.now.return_value = later
            page = self.client.get(page_url)
            self.assertContains(page, 'id="session-deadline-passed"')
            self.assertNotContains(page, 'id="reopen-session"')
            self.assertNotContains(page, 'id="odometer-form"')
            self.assertNotContains(self.client.get(vehicle_url), "Resume incomplete")

    def test_a_reopen_past_the_deadline_is_refused_on_the_wire(self):
        session_id = self.open().json()["result"]["session_id"]
        self.post(
            reverse("api-inspection-session-end", args=[session_id]), {"operation_id": str(uuid4())}
        )
        # The command's own deadline is tested with a controlled clock (DefaultLimitTests);
        # here only the wire mapping of its refusal.
        with mock.patch("cya.inspections.commands.deadline_passed", return_value=True):
            refused = self.post(
                reverse("api-inspection-session-reopen", args=[session_id]),
                {"operation_id": str(uuid4())},
            )
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "SESSION_DEADLINE_PASSED")


class DeadlineDatabaseGuardTests(DeadlineCase):
    """A writer that skips the commands still cannot work past R* (triggers)."""

    def refused(self, message, fn):
        with self.assertRaises(DatabaseError) as caught:
            fn()
        self.assertIn(message, str(caught.exception))

    def test_no_check_is_stored_at_or_after_the_deadline(self):
        from unittest import mock

        self.advance(hours=5)
        self.check()  # A = +5 h: the inactivity limit is far, the deadline is not
        self.advance(hours=1, minutes=1)
        with mock.patch("cya.checks.commands._session_for_check", return_value=self.session()):
            self.refused("passed its resume deadline", self.check)

    def test_no_reopen_or_join_is_stored_at_or_after_the_deadline(self):
        from unittest import mock

        self.end()
        self.advance(hours=6)
        with mock.patch.object(commands, "deadline_passed", return_value=False):
            self.refused("reached its resume deadline", self.reopen)
        self.assertEqual(self.state().state, "CLOSED")

    def test_no_join_is_stored_at_or_after_the_deadline(self):
        from unittest import mock

        self.advance(hours=5)
        self.check()
        self.advance(hours=1, minutes=1)
        with mock.patch.object(commands, "is_expired", return_value=False):
            self.refused("reached its resume deadline", lambda: self.open(actor=self.supervisor))

    def test_the_deadline_close_is_only_at_r_star_and_only_when_it_comes_first(self):
        from unittest import mock

        self.advance(hours=6, minutes=1)  # untouched: the inactivity close is due at R
        with mock.patch.object(projections, "INACTIVITY", 7 * HOUR):  # claims R comes first
            self.refused(
                "deadline close instant",
                lambda: auto_close_session(
                    organization_id=str(self.org.pk), session_id=self.session_id
                ),
            )
        self.assertEqual(self.state().state, "OPEN")

    def test_an_inactivity_close_after_r_star_is_refused(self):
        from unittest import mock

        self.advance(hours=5)
        self.check()
        self.advance(hours=1, minutes=1)
        with mock.patch.object(
            projections.SessionState, "deadline_reached_first", property(lambda self: False)
        ):
            self.refused(
                "auto-close instant",
                lambda: auto_close_session(
                    organization_id=str(self.org.pk), session_id=self.session_id
                ),
            )

    def test_a_session_must_capture_the_current_limit(self):
        from unittest import mock

        self.end()
        with mock.patch.object(commands, "resume_limit_hours", return_value=7):
            self.refused(
                "resume limit is not the current limit", lambda: self.open(actor=self.supervisor)
            )

    def test_a_session_past_its_deadline_is_not_in_progress_for_the_backstop(self):
        """Part D's trigger judges another session by its end instant E, not A + 6 h."""
        self.advance(hours=5)
        self.check()  # A + 6 h is +11 h, but R is +6 h
        self.advance(hours=1, minutes=1)
        started = self.open(actor=self.supervisor)  # the trigger would refuse under A + 6 h
        self.assertEqual(started.body["code"], "SESSION_OPENED")
        self.assertEqual(SessionEvent.objects.filter(event_type="OPEN").count(), 2)
