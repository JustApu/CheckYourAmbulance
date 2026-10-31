"""Persisted activity and the six-hour auto-close (issue 78 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) Part B; SPEC 6.3; AC-2, AC-6)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Event
from unittest import mock

from django.core.management import call_command
from django.db import DatabaseError, transaction

from cya.checks.commands import CheckError
from cya.checks.models import CheckRecord
from cya.checks.tests.support import CheckCase
from cya.core.models import AuditEvent
from cya.inspections import commands
from cya.inspections.commands import SessionError, auto_close_inactive_sessions, auto_close_session
from cya.inspections.models import InspectionSession, SessionEvent, SessionProjection
from cya.inspections.projections import current_state, fold_session, read_session
from cya.obligations.tests.support import Clock

HOUR = timedelta(hours=1)


class ActivityCase(CheckCase):
    """A published checklist, a controllable server clock and helpers for sessions with checks."""

    def setUp(self):
        super().setUp()
        self.clock = Clock(self.now + timedelta(minutes=5))
        patcher = mock.patch("django.utils.timezone.now", side_effect=self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session_id = self.open().body["session_id"]

    def open(self, *, actor=None, vehicle=None, **kw):
        return commands.open_or_join_session(
            vehicle_id=str((vehicle or self.vehicle).pk),
            template_id=str(self.template.pk),
            **kw,
            **self.context(actor or self.crew),
        )

    def advance(self, **delta):
        self.clock.set(self.clock.now + timedelta(**delta))

    def state(self, session_id=None):
        return current_state(InspectionSession.objects.get(pk=session_id or self.session_id))

    def check(self, *, session_id=None, **kw):
        return self.record(session_id=session_id or self.session_id, **kw)

    def close_job(self):
        return auto_close_inactive_sessions()

    def events(self, session_id=None):
        return list(
            SessionEvent.objects.filter(session_id=session_id or self.session_id).order_by(
                "sequence"
            )
        )


class PersistedActivityTests(ActivityCase):
    def test_a_committed_check_in_the_session_is_activity_and_resets_the_timer(self):
        opened_at = self.state().last_activity_at
        self.advance(hours=5)
        checked = self.check()
        row = CheckRecord.objects.get(pk=checked.body["check_id"])
        self.assertEqual(row.session_id, self.session_id)
        self.assertEqual(self.state().last_activity_at, row.recorded_at)
        self.assertEqual(self.state().last_activity_command_id, row.command_id)
        projection = SessionProjection.objects.get(session_id=self.session_id)
        self.assertEqual(projection.last_activity_at, row.recorded_at)
        self.advance(hours=5, minutes=30)  # 10.5 h after open, 5.5 h after the check
        self.assertEqual(self.close_job(), {"closed": 0, "skipped": 0, "failed": 0})
        self.assertEqual(self.state().state, "OPEN")
        self.assertGreater(row.recorded_at, opened_at)

    def test_a_check_outside_any_session_and_viewing_are_not_session_activity(self):
        first = self.state().last_activity_at
        self.advance(hours=1)
        self.record(item=self.masks, quantity=6)  # no session_id
        read_session(self.org.pk, self.session_id)  # viewing
        self.assertEqual(self.state().last_activity_at, first)

    def test_six_hours_of_viewing_only_closes_as_incomplete_with_checks_intact(self):
        self.check()
        stamp = self.state().last_activity_at
        for _ in range(6):  # a heartbeat or a view every hour never counts
            self.advance(hours=1)
            read_session(self.org.pk, self.session_id)
            self.open(actor=self.crew)  # an existing participant "joining" again is not activity
        self.advance(minutes=1)
        self.assertEqual(self.close_job()["closed"], 1)
        state = self.state()
        self.assertEqual(state.state, "INCOMPLETE")
        self.assertEqual(self.events()[-1].event_type, "AUTO_INCOMPLETE")
        self.assertEqual(self.events()[-1].effective_at, stamp + 6 * HOUR)
        self.assertEqual(CheckRecord.objects.filter(session_id=self.session_id).count(), 1)

    def test_a_delayed_job_stamps_the_activity_based_instant_not_its_wake_time(self):
        self.advance(hours=2)
        checked = self.check()
        activity = CheckRecord.objects.get(pk=checked.body["check_id"]).recorded_at
        self.advance(hours=30)  # the job is very late
        self.assertEqual(self.close_job()["closed"], 1)
        last = self.events()[-1]
        self.assertEqual(last.effective_at, activity + 6 * HOUR)
        self.assertGreater(last.recorded_at, last.effective_at + 20 * HOUR)
        self.assertIsNone(last.actor_user_id)
        self.assertEqual(last.command.service_identity, commands.SERVICE_IDENTITY)
        audit = AuditEvent.objects.get(action=commands.AUTO_CLOSE_COMMAND)
        self.assertEqual((audit.outcome, audit.actor_user_id), ("SUCCEEDED", None))

    def test_the_job_is_idempotent_and_leaves_a_session_that_is_not_due(self):
        self.assertEqual(self.close_job(), {"closed": 0, "skipped": 0, "failed": 0})
        self.advance(hours=7)
        self.assertEqual(self.close_job()["closed"], 1)
        before = len(self.events())
        self.assertEqual(self.close_job(), {"closed": 0, "skipped": 0, "failed": 0})
        self.assertEqual(len(self.events()), before)
        with self.assertRaises(SessionError):
            auto_close_session(organization_id=str(self.org.pk), session_id=self.session_id)

    def test_reopen_is_logged_starts_a_new_window_and_keeps_the_original_version(self):
        self.advance(hours=7)
        self.close_job()  # the job ran: INCOMPLETE
        reopened = commands.reopen_session(session_id=self.session_id, **self.context(self.crew))
        self.assertEqual(reopened.body["code"], "SESSION_REOPENED")
        self.assertEqual(self.events()[-1].event_type, "REOPEN")

    def test_a_session_idle_for_six_hours_is_reopened_without_waiting_for_the_job(self):
        self.advance(hours=7)  # the job has not run
        reopened = commands.reopen_session(session_id=self.session_id, **self.context(self.crew))
        self.assertEqual(reopened.body["code"], "SESSION_REOPENED")
        types = [e.event_type for e in self.events()]
        self.assertEqual(types, ["OPEN", "AUTO_INCOMPLETE", "REOPEN"])
        reopen = self.events()[-1]
        self.assertEqual(self.state().last_activity_at, reopen.effective_at)
        self.assertEqual(
            InspectionSession.objects.get(pk=self.session_id).checklist_version_id, self.version_id
        )
        self.assertTrue(AuditEvent.objects.filter(action="SESSION_REOPEN").exists())
        # the new window runs from the reopen instant
        self.advance(hours=5, minutes=59)
        self.assertEqual(self.close_job()["closed"], 0)
        self.advance(minutes=2)
        self.assertEqual(self.close_job()["closed"], 1)

    def test_default_join_does_not_resurrect_an_idle_session(self):
        self.advance(hours=7)
        joined = self.open(actor=self.supervisor)
        self.assertEqual(joined.body["code"], "SESSION_OPENED")
        self.assertNotEqual(joined.body["session_id"], self.session_id)

    def test_projection_rebuilds_from_history_including_checks(self):
        self.advance(hours=1)
        self.check()
        session = InspectionSession.objects.get(pk=self.session_id)
        projection = SessionProjection.objects.get(session=session)
        SessionProjection.objects.filter(pk=projection.pk).update(last_activity_at=self.now)
        with transaction.atomic():
            from cya.inspections.projections import rebuild_session_projection

            rebuilt = rebuild_session_projection(session)
        self.assertEqual(
            SessionProjection.objects.get(pk=projection.pk).last_activity_at,
            rebuilt.last_activity_at,
        )
        # history alone (no checks) would put the clock earlier: the check is what moved it
        self.assertLess(fold_session(self.events()).last_activity_at, rebuilt.last_activity_at)


class CheckInSessionTests(ActivityCase):
    def rejected(self, code, **kwargs):
        before = CheckRecord.objects.count()
        with self.assertRaises(CheckError) as caught:
            self.check(**kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(CheckRecord.objects.count(), before)
        return caught.exception

    def test_a_participant_records_into_an_open_session_on_its_vehicle_and_version(self):
        commands.open_or_join_session(
            vehicle_id=str(self.vehicle.pk),
            template_id=str(self.template.pk),
            **self.context(self.supervisor),
        )
        row = CheckRecord.objects.get(pk=self.check(actor=self.supervisor).body["check_id"])
        self.assertEqual((row.session_id, row.actor_user_id), (self.session_id, self.supervisor.pk))

    def test_an_unknown_session_and_one_the_actor_never_joined_answer_alike(self):
        other = self.open(actor=self.supervisor, vehicle=self.vehicles[1]).body["session_id"]
        unknown = self.rejected("NOT_FOUND", session_id="00000000-0000-4000-8000-000000000000")
        stranger = self.rejected("NOT_FOUND", session_id=other, actor=self.admin)
        self.assertEqual((unknown.status, unknown.path), (stranger.status, stranger.path))
        self.assertEqual(str(unknown), str(stranger))

    def test_the_session_must_match_the_vehicle_and_the_checklist_version(self):
        elsewhere = self.open(vehicle=self.vehicles[1]).body["session_id"]
        self.rejected(
            "SESSION_MISMATCH", session_id=elsewhere
        )  # vehicle A check, vehicle B session
        newer = self.published_version(self.items)  # a different, later version
        self.rejected("SESSION_MISMATCH", checklist_version_id=newer)

    def publish_override(self, entries):
        from cya.checklists import commands as checklist_commands

        draft = checklist_commands.save_override_draft(
            vehicle_id=str(self.vehicle.pk),
            template_version_id=self.version_id,
            entries=entries,
            **self.context(),
        ).body["override_version_id"]
        checklist_commands.publish_override_version(
            override_version_id=draft, effective_at=None, **self.context()
        )

    def test_a_session_keeps_recording_against_its_pinned_version_after_a_newer_one(self):
        self.published_version(self.items)  # takes effect now; the session opened before it
        self.advance(minutes=1)
        with self.assertRaises(CheckError) as outside:
            self.record()  # no session: the current checklist governs
        self.assertEqual(outside.exception.code, "CHECKLIST_VERSION_NOT_CURRENT")
        self.assertEqual(self.check().status, 201)
        commands.end_session(session_id=self.session_id, **self.context(self.crew))
        commands.reopen_session(session_id=self.session_id, **self.context(self.crew))
        self.advance(minutes=1)
        self.assertEqual(self.check(item=self.masks, quantity=6).status, 201)
        self.assertEqual(CheckRecord.objects.filter(session_id=self.session_id).count(), 2)

    def test_a_session_ignores_an_override_published_after_it_opened(self):
        self.publish_override(
            [
                {"item_id": str(self.suction.pk), "action": "REMOVE"},
            ]
        )
        self.advance(minutes=1)
        with self.assertRaises(CheckError) as outside:
            self.record()
        self.assertEqual(outside.exception.code, "ITEM_NOT_IN_CHECKLIST")
        self.assertEqual(self.check().status, 201)  # the pinned checklist still has the item

    def test_a_probe_cannot_tell_a_real_pinned_session_from_an_unknown_one(self):
        self.publish_override([{"item_id": str(self.suction.pk), "action": "REMOVE"}])
        self.advance(minutes=1)
        real = self.rejected("ITEM_NOT_IN_CHECKLIST", session_id=self.session_id, actor=self.admin)
        unknown = self.rejected(
            "ITEM_NOT_IN_CHECKLIST",
            session_id="00000000-0000-4000-8000-000000000000",
            actor=self.admin,
        )
        self.assertEqual((real.status, real.path), (unknown.status, unknown.path))
        self.assertEqual(self.check().status, 201)  # the participant still gets the pinned item

    def test_a_session_opened_under_an_override_keeps_it_after_the_override_changes(self):
        commands.end_session(session_id=self.session_id, **self.context(self.crew))
        self.publish_override([{"item_id": str(self.suction.pk), "action": "REMOVE"}])
        self.advance(minutes=1)
        pinned = self.open().body["session_id"]  # opened without the removed item
        self.publish_override([{"item_id": str(self.siren.pk), "action": "REMOVE"}])
        self.advance(minutes=1)
        self.rejected("ITEM_NOT_IN_CHECKLIST", session_id=pinned)
        self.assertEqual(self.check(session_id=pinned, item=self.siren).status, 201)

    def test_an_ended_session_refuses_checks_until_reopened(self):
        commands.end_session(session_id=self.session_id, **self.context(self.crew))
        self.rejected("SESSION_NOT_OPEN")
        commands.reopen_session(session_id=self.session_id, **self.context(self.crew))
        self.assertEqual(self.check().status, 201)

    def test_an_idle_session_refuses_a_late_check_even_before_the_job_runs(self):
        self.advance(hours=6, minutes=1)
        error = self.rejected("SESSION_NOT_OPEN")
        self.assertEqual(error.status, 409)
        self.assertEqual(self.state().state, "OPEN")  # the job has not closed it; the rule applies
        self.assertEqual(self.close_job()["closed"], 1)

    def test_a_stale_projection_can_delay_a_close_but_never_erase_work(self):
        self.advance(hours=5)
        self.check()  # activity at +5 h
        SessionProjection.objects.filter(session_id=self.session_id).update(
            last_activity_at=self.now - HOUR
        )  # a stale cache claims the session is long idle
        self.advance(hours=2)  # +7 h: idle 2 h in truth
        self.assertEqual(self.close_job(), {"closed": 0, "skipped": 1, "failed": 0})
        self.assertEqual(self.state().state, "OPEN")

    def test_a_check_racing_the_close_loses_cleanly_when_the_close_commits_first(self):
        self.advance(hours=6, minutes=1)
        holding, release = Event(), Event()
        real = commands._lock_vehicle

        def hold_after_locking(org, vehicle_id):
            vehicle = real(org, vehicle_id)
            holding.set()
            self.assertTrue(release.wait(timeout=20))
            return vehicle

        def run_job():
            with mock.patch.object(commands, "_lock_vehicle", hold_after_locking):
                return auto_close_session(
                    organization_id=str(self.org.pk), session_id=self.session_id
                )

        def run_check():
            self.assertTrue(holding.wait(timeout=20))
            release.set()  # let the job finish; the check then waits for the vehicle lock
            try:
                self.check()
            except CheckError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            job, racer = pool.submit(run_job), pool.submit(run_check)
            self.assertEqual(job.result(timeout=60).body["code"], "SESSION_AUTO_CLOSED")
            self.assertEqual(racer.result(timeout=60), "SESSION_NOT_OPEN")
        self.assertEqual(CheckRecord.objects.filter(session_id=self.session_id).count(), 0)
        self.assertEqual(self.state().state, "INCOMPLETE")


class DatabaseGuardTests(ActivityCase):
    """A writer that skips the command still cannot break the rules (triggers and CHECKs)."""

    def refused(self, message, fn):
        # The commands own their transaction, so none may wrap them.
        with self.assertRaises(DatabaseError) as caught:
            fn()
        self.assertIn(message, str(caught.exception))

    def test_a_check_cannot_be_stored_in_a_session_that_has_ended(self):
        commands.end_session(session_id=self.session_id, **self.context(self.crew))
        session = InspectionSession.objects.get(pk=self.session_id)
        with mock.patch("cya.checks.commands._session_for_check", return_value=session):
            self.refused("check session is not open", self.check)

    def test_a_check_cannot_be_stored_in_a_session_idle_for_six_hours(self):
        self.advance(hours=6, minutes=1)
        session = InspectionSession.objects.get(pk=self.session_id)
        with mock.patch("cya.checks.commands._session_for_check", return_value=session):
            self.refused("idle for the inactivity limit", self.check)

    def test_a_check_cannot_name_a_session_of_another_vehicle_or_a_nonparticipant(self):
        other = self.open(actor=self.supervisor, vehicle=self.vehicles[1]).body["session_id"]
        elsewhere = InspectionSession.objects.get(pk=other)
        with mock.patch("cya.checks.commands._session_for_check", return_value=elsewhere):
            self.refused("not this vehicle and version", self.check)
        mine = InspectionSession.objects.get(pk=self.session_id)
        with mock.patch("cya.checks.commands._session_for_check", return_value=mine):
            self.refused("not a participant", lambda: self.check(actor=self.admin))

    def test_the_auto_close_instant_must_be_the_last_activity_plus_six_hours(self):
        self.advance(hours=5, minutes=30)
        with mock.patch.object(commands, "INACTIVITY", timedelta(hours=5)):
            self.refused(
                "auto-close instant",
                lambda: auto_close_session(
                    organization_id=str(self.org.pk), session_id=self.session_id
                ),
            )
        self.assertEqual(self.state().state, "OPEN")

    def test_only_the_job_may_write_an_actorless_event_and_it_may_write_nothing_else(self):
        template = dict(
            organization_id=self.org.pk,
            session_id=self.session_id,
            sequence=2,
            prior_event=self.events()[-1],
            effective_at=self.clock.now,
            recorded_at=self.clock.now,
            command=self.events()[-1].command,
        )
        for kind in ("JOIN", "END_CLOSED", "AUTO_INCOMPLETE"):
            with self.subTest(kind):
                self.refused(
                    "",
                    lambda kind=kind: SessionEvent.objects.create(
                        event_type=kind, actor_user=None, **template
                    ),
                )

    def test_the_management_command_reports_counts_and_validates_its_limit(self):
        self.advance(hours=7)
        out = StringIO()
        call_command("close_inactive_sessions", stdout=out)
        self.assertEqual(out.getvalue().strip(), "closed=1 skipped=0 failed=0")
        for bad in (0, 10_001):
            with self.subTest(bad), self.assertRaises(ValueError):
                auto_close_inactive_sessions(limit=bad)
