"""Administrator closure and reopening preserve an explicit, attributed history (issue 152 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/152))."""

import json
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.db import connection

from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies import service
from cya.deficiencies.closure import close_administrator, close_empty_scope, reopen_administrator
from cya.deficiencies.models import DeficiencyEvent
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.tests.test_assignment import LifecycleCase

EXPLANATION = "The replacement part is unavailable and the administrator accepted the risk."


class AdministratorClosureTests(LifecycleCase):
    def close(self, actor=None, **overrides):
        args = self.args(actor=actor or self.admin)
        args.update(overrides)
        args.setdefault("explanation", EXPLANATION)
        return close_administrator(**args)

    def reopen(self, actor=None, **overrides):
        args = self.args(actor=actor or self.admin)
        args.update(overrides)
        args.setdefault("explanation", "The replacement part arrived; work can resume.")
        return reopen_administrator(**args)

    def close_empty(self, actor=None, **overrides):
        args = self.args(actor=actor or self.supervisor)
        args.update(overrides)
        args.setdefault("explanation", "The current checklist no longer contains this item.")
        return close_empty_scope(**args)

    def test_close_and_reopen_are_explicit_events_and_history_keeps_both(self):
        original = self.newest()
        closed = self.close()
        close_event = DeficiencyEvent.objects.get(pk=closed.body["event_id"])
        self.assertEqual(
            (close_event.event_type, close_event.from_status, close_event.to_status),
            ("ADMINISTRATOR_CLOSED", "OPEN", "CLOSED_ADMINISTRATOR"),
        )
        self.assertEqual(close_event.explanation, EXPLANATION)
        self.assertEqual(close_event.actor_user_id, self.admin.pk)
        self.assertEqual(close_event.suspensions_ended, [])
        self.assertEqual(self.projection().status, "CLOSED_ADMINISTRATOR")
        receipt = CommandReceipt.objects.get(pk=close_event.command_id)
        self.assertEqual(receipt.command_type, "DEFICIENCY_ADMINISTRATOR_CLOSE")
        audit = AuditEvent.objects.get(command=receipt)
        self.assertNotIn(EXPLANATION, json.dumps([audit.prior_snapshot, audit.new_snapshot]))

        reopened = self.reopen()
        reopen_event = DeficiencyEvent.objects.get(pk=reopened.body["event_id"])
        self.assertEqual(
            (reopen_event.event_type, reopen_event.from_status, reopen_event.to_status),
            ("ADMINISTRATOR_REOPENED", "CLOSED_ADMINISTRATOR", "OPEN"),
        )
        self.assertEqual(self.projection().status, "OPEN")
        history = service.assignment_state(str(self.org.pk), self.deficiency_id)["history"]
        self.assertEqual(
            [event["event_type"] for event in history],
            ["ADMINISTRATOR_CLOSED", "ADMINISTRATOR_REOPENED"],
        )
        self.assertEqual(history[0]["event_id"], str(close_event.pk))
        self.assertEqual(history[0]["suspensions_ended"], [])
        self.assertNotEqual(original, str(close_event.pk))

    def test_every_nonadministrator_role_is_denied_and_denials_are_audited(self):
        before = DeficiencyEvent.objects.count()
        for actor in (self.crew, self.supervisor, self.supply):
            with self.assertRaises(CommandDenied):
                self.close(actor=actor)
        self.assertEqual(DeficiencyEvent.objects.count(), before)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="DEFICIENCY_ADMINISTRATOR_CLOSE", outcome="DENIED"
            ).exists()
        )

    def test_explanation_and_expected_event_are_required(self):
        for explanation in ("", " ", None):
            with self.assertRaises(DeficiencyError) as caught:
                self.close(explanation=explanation)
            self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        with self.assertRaises(DeficiencyError) as caught:
            self.close(expected_event_id=str(uuid4()))
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("DEFICIENCY_CHANGED", 409)
        )

    def test_only_administrator_closed_deficiencies_can_be_reopened(self):
        with self.assertRaises(DeficiencyError) as caught:
            self.reopen()
        self.assertEqual(caught.exception.code, "INVALID_TRANSITION")
        self.close()
        self.reopen()
        with self.assertRaises(DeficiencyError) as caught:
            self.reopen()
        self.assertEqual(caught.exception.code, "INVALID_TRANSITION")

    def test_empty_scope_close_is_distinct_and_supervisor_can_reopen_only_as_admin(self):
        with mock.patch("cya.deficiencies.service.correction_scope", return_value=[]):
            result = self.close_empty()
        event = DeficiencyEvent.objects.get(pk=result.body["event_id"])
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status),
            ("EMPTY_SCOPE_CLOSED", "OPEN", "CLOSED_UNVERIFIABLE"),
        )
        self.assertEqual(event.actor_user_id, self.supervisor.pk)
        self.assertEqual(self.projection().status, "CLOSED_UNVERIFIABLE")
        self.assertEqual(result.body["code"], "DEFICIENCY_EMPTY_SCOPE_CLOSED")
        self.assertFalse(
            DeficiencyEvent.objects.filter(
                deficiency_id=self.deficiency_id, event_type="CORRECTION_RECORDED"
            ).exists()
        )
        with self.assertRaises(CommandDenied):
            self.reopen(actor=self.supervisor)
        self.reopen(actor=self.admin)
        self.assertEqual(self.projection().status, "OPEN")

    def test_empty_scope_close_refuses_when_verification_items_remain(self):
        with mock.patch(
            "cya.deficiencies.service.correction_scope", return_value=[("item", "def", None)]
        ):
            with self.assertRaises(DeficiencyError) as caught:
                self.close_empty()
        self.assertEqual(caught.exception.code, "SCOPE_AVAILABLE")

    def test_empty_scope_close_denies_crew_and_supply_officer(self):
        with mock.patch("cya.deficiencies.service.correction_scope", return_value=[]):
            for actor in (self.crew, self.supply):
                with self.assertRaises(CommandDenied):
                    self.close_empty(actor=actor)

    def test_administrator_can_use_the_empty_scope_event_without_changing_general_close(self):
        with mock.patch("cya.deficiencies.service.correction_scope", return_value=[]):
            result = self.close_empty(actor=self.admin)
        event = DeficiencyEvent.objects.get(pk=result.body["event_id"])
        self.assertEqual(event.event_type, "EMPTY_SCOPE_CLOSED")
        self.assertEqual(event.actor_user_id, self.admin.pk)

    def test_migration_refuses_reversal_after_an_empty_scope_closure(self):
        with mock.patch("cya.deficiencies.service.correction_scope", return_value=[]):
            self.close_empty()
        migration = import_module("cya.deficiencies.migrations.0013_empty_scope_closure")
        editor = SimpleNamespace(connection=connection)
        with self.assertRaisesRegex(RuntimeError, "cannot reverse empty-scope closure"):
            migration.refuse_reverse_with_data(None, editor)

    def test_operation_replay_returns_original_close_event(self):
        operation_id = str(uuid4())
        args = {
            **self.context(self.admin),
            "deficiency_id": self.deficiency_id,
            "expected_event_id": self.newest(),
            "explanation": EXPLANATION,
            "operation_id": operation_id,
        }
        first = close_administrator(**args)
        replay = close_administrator(**args)
        self.assertTrue(replay.replayed)
        self.assertFalse(replay.operation_reused)
        self.assertEqual(first.body["event_id"], replay.body["event_id"])


class AdministratorClosureRaceTests(LifecycleCase):
    def run_threads(self, jobs):
        def run(job):
            try:
                return job()
            except Exception as error:
                return error
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            return [
                future.result(timeout=120) for future in [pool.submit(run, job) for job in jobs]
            ]

    def test_concurrent_close_commands_from_one_view_append_exactly_one_event(self):
        expected = self.newest()
        outcomes = self.run_threads(
            [
                lambda: close_administrator(
                    deficiency_id=self.deficiency_id,
                    expected_event_id=expected,
                    explanation=EXPLANATION,
                    **self.context(self.admin),
                ),
                lambda: close_administrator(
                    deficiency_id=self.deficiency_id,
                    expected_event_id=expected,
                    explanation="A separate administrator decision.",
                    **self.context(self.admin),
                ),
            ]
        )
        committed = [outcome for outcome in outcomes if hasattr(outcome, "body")]
        changed = [outcome for outcome in outcomes if isinstance(outcome, DeficiencyError)]
        self.assertEqual((len(committed), len(changed)), (1, 1), outcomes)
        self.assertEqual(changed[0].code, "DEFICIENCY_CHANGED")
        self.assertEqual(
            DeficiencyEvent.objects.filter(
                deficiency_id=self.deficiency_id, event_type="ADMINISTRATOR_CLOSED"
            ).count(),
            1,
        )

    def test_concurrent_close_and_reopen_cannot_skip_the_close_view(self):
        expected = self.newest()
        outcomes = self.run_threads(
            [
                lambda: close_administrator(
                    deficiency_id=self.deficiency_id,
                    expected_event_id=expected,
                    explanation=EXPLANATION,
                    **self.context(self.admin),
                ),
                lambda: reopen_administrator(
                    deficiency_id=self.deficiency_id,
                    expected_event_id=expected,
                    explanation="Reopen based on the stale view.",
                    **self.context(self.admin),
                ),
            ]
        )
        committed = [outcome for outcome in outcomes if hasattr(outcome, "body")]
        rejected = [outcome for outcome in outcomes if isinstance(outcome, DeficiencyError)]
        self.assertEqual((len(committed), len(rejected)), (1, 1), outcomes)
        self.assertIn(rejected[0].code, {"DEFICIENCY_CHANGED", "INVALID_TRANSITION"})
        self.assertEqual(
            DeficiencyEvent.objects.filter(
                deficiency_id=self.deficiency_id,
                event_type__in=("ADMINISTRATOR_CLOSED", "ADMINISTRATOR_REOPENED"),
            ).count(),
            1,
        )
