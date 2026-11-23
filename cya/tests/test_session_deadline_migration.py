"""Reverse and reapply the resume-limit migrations (issue #256 Part E; SPEC 18.3 item 3, AC-41(7)).

cya_inspections 0007 adds the captured limit (every existing session gets 6 hours), the
projection's end instant and the deadline guards; cya_checks 0019 adds the check guard. A session
that existed before them, with a check recorded after its opened_at + 6 h under the earlier rules,
reads as ended at that check's instant (R* = A) once they are applied, and the job records that
same instant.
"""

from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import tag

from cya.inspections.models import InspectionSession, SessionEvent, SessionProjection
from cya.inspections.projections import current_state
from cya.inspections.tests.test_activity import HOUR, ActivityCase

BEFORE = [
    ("cya_inspections", "0006_one_session_per_checklist"),
    ("cya_checks", "0018_verification_place"),
]


def trigger_text(name):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s",
            [name],
        )
        row = cursor.fetchone()
    return None if row is None else row[0]


def columns(table):
    with connection.cursor() as cursor:
        return {c.name for c in connection.introspection.get_table_description(cursor, table)}


@tag("migration_rollback")
class SessionDeadlineMigrationTests(ActivityCase):
    # A long limit lets this test record a check after opened_at + 6 h, as the rules before
    # 6.0.0 allowed; reversing the migration then forgets the limit, as if it never existed.
    resume_limit_hours = 24

    def migrate_back_and_forward(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate(BEFORE)
            self.assertNotIn("resume_limit_hours", columns("inspection_session"))
            self.assertNotIn("ends_at", columns("session_projection"))
            self.assertNotIn("DEADLINE_INCOMPLETE", trigger_text("cya_session_event_bi"))
            self.assertNotIn("resume deadline", trigger_text("cya_check_record_bi"))
            self.assertNotIn("session_resume_limit", connection.introspection.table_names())
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_each_rewrite_only_changes_what_it_states(self):
        checks_before = import_module("cya.checks.migrations.0018_verification_place")
        checks_after = import_module("cya.checks.migrations.0019_check_session_deadline")
        self.assertEqual(
            checks_after.check_trigger().replace(checks_after.DEADLINE_GUARD, ""),
            checks_before.check_trigger(),
        )
        events = import_module("cya.inspections.migrations.0007_session_resume_limit")
        text = events.event_insert(6)
        for fragment in ("DEADLINE_INCOMPLETE", "v_deadline", "reached its resume deadline"):
            self.assertIn(fragment, text)
        self.assertIn("resume_limit_hours", events.session_insert())

    def test_a_session_from_before_reads_as_ended_at_its_late_check_and_the_job_agrees(self):
        """AC-41(7)."""
        opened_at = InspectionSession.objects.get(pk=self.session_id).opened_at
        self.advance(hours=5)
        self.check()  # activity keeps the session open past opened_at + 6 h ...
        self.advance(hours=4)
        late = self.check()  # ... so this check 9 h after opening was allowed before 6.0.0
        from cya.checks.models import CheckRecord

        checked_at = CheckRecord.objects.get(pk=late.body["check_id"]).recorded_at
        self.migrate_back_and_forward()
        session = InspectionSession.objects.get(pk=self.session_id)
        self.assertEqual(session.resume_limit_hours, 6)  # SPEC 18.3 item 3
        state = current_state(session)
        self.assertEqual(state.resume_deadline_at, checked_at)  # R* = max(R, A) = A
        self.assertEqual(state.ends_at, checked_at)
        self.assertGreater(checked_at, opened_at + 6 * HOUR)
        self.assertEqual(SessionProjection.objects.get(session=session).ends_at, checked_at)
        self.advance(minutes=1)
        self.assertEqual(self.close_job()["at_deadline"], 1)
        self.assertEqual(self.close_job()["closed"], 0)
        ending = SessionEvent.objects.get(session=session, event_type="DEADLINE_INCOMPLETE")
        self.assertEqual(ending.effective_at, checked_at)

    def test_reversing_stops_while_a_deadline_ending_exists(self):
        self.set_resume_limit(6)
        self.end()
        session_id = self.open().body["session_id"]
        self.advance(hours=5)
        self.check(session_id=session_id)
        self.advance(hours=2)
        self.assertEqual(self.close_job()["at_deadline"], 1)
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            with self.assertRaises(RuntimeError):
                MigrationExecutor(connection).migrate(BEFORE)
        finally:
            MigrationExecutor(connection).migrate(latest)
        self.assertTrue(SessionEvent.objects.filter(event_type="DEADLINE_INCOMPLETE").exists())

    def end(self):
        from cya.inspections import commands

        return commands.end_session(session_id=self.session_id, **self.context(self.crew))
