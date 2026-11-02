"""Reversing the M4 lifecycle migrations (issue #20).

With lifecycle data present, each reverse guard of 0008-0013 refuses. With none, stepping down
from the current leaf to 0007 restores at each step the event trigger the earlier migration
installed and drops what the reversed one added; reapplying restores the schema exactly.
"""

from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from cya.deficiencies import age, closure, extensions
from cya.deficiencies.commands import record_correction, set_deadline, start_work
from cya.deficiencies.tests.test_acknowledgement import AcknowledgementCase

APP = "cya_deficiencies"
GUARDS = {
    "0008_deadline_extensions": "_reverse_guard",
    "0009_age_rule": "refuse_reverse_with_data",
    "0010_escalation_acknowledgement": "refuse_reverse_with_data",
    "0011_administrator_closure_reopen": "refuse_reverse_with_data",
    "0012_correction_verification_scope": "refuse_reverse_with_data",
    "0013_empty_scope_closure": "refuse_reverse_with_data",
}


def guard(name):
    return getattr(import_module(f"cya.deficiencies.migrations.{name}"), GUARDS[name])


def run_guard(name):
    guard(name)(None, SimpleNamespace(connection=connection))


class PopulatedReversalTests(AcknowledgementCase):
    """Each guard refuses while its migration's data exists (the guard runs before any DDL)."""

    def assert_refuses(self, name, message):
        with self.assertRaisesRegex(RuntimeError, message):
            run_guard(name)

    def test_every_guard_passes_before_its_data_exists(self):
        for name in GUARDS:
            with self.subTest(name):
                run_guard(name)

    def test_extension_age_rule_and_acknowledgement_data_refuse_reversal(self):
        set_deadline(deadline=timezone.now() + timedelta(days=2), **self.args(actor=self.admin))
        extensions.request_extension(
            reason="Synthetic delay.",
            proposed_deadline=timezone.now() + timedelta(days=5),
            **self.args(actor=self.crew),
        )
        self.assert_refuses("0008_deadline_extensions", "extension events")
        age.set_age_rule(
            age_thresholds_days=[7],
            long_running_age_days=30,
            long_running_extensions=2,
            **self.context(self.admin),
        )
        self.assert_refuses("0009_age_rule", "age-highlight rules")
        self.set_maximum(3)
        self.assert_refuses("0010_escalation_acknowledgement", "maximum setting")

    def test_closure_correction_and_empty_scope_data_refuse_reversal(self):
        closure.close_administrator(explanation="Synthetic close.", **self.args(actor=self.admin))
        self.assert_refuses("0011_administrator_closure_reopen", "administrator closure")
        start_work(**self.args(actor=self.admin, deficiency_id=self.supply_id))
        record_correction(**self.args(actor=self.crew, deficiency_id=self.supply_id))
        self.assert_refuses("0012_correction_verification_scope", "correction")

    def test_empty_scope_closure_refuses_reversal(self):
        with mock.patch("cya.deficiencies.service.correction_scope", return_value=[]):
            closure.close_empty_scope(
                explanation="Synthetic empty scope.", **self.args(actor=self.supervisor)
            )
        self.assert_refuses("0013_empty_scope_closure", "empty-scope closure")


def event_trigger_text(name):
    """The deficiency_event insert trigger a migration installs, or None if it leaves it alone."""
    module = import_module(f"cya.deficiencies.migrations.{name}")
    for attribute in ("event_insert", "EVENT_INSERT"):
        value = getattr(module, attribute, None)
        if value is not None:
            return value() if callable(value) else value
    return None


class EmptyReversalTests(TransactionTestCase):
    """No lifecycle data: reversal restores the previous triggers and constraints."""

    def schema(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TRIGGER_NAME, ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() ORDER BY TRIGGER_NAME"
            )
            triggers = dict(cursor.fetchall())
            cursor.execute(
                "SELECT TABLE_NAME, CONSTRAINT_NAME, CHECK_CLAUSE "
                "FROM information_schema.CHECK_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() "
                "ORDER BY TABLE_NAME, CONSTRAINT_NAME"
            )
            checks = {(table, name): clause for table, name, clause in cursor.fetchall()}
            tables = set(connection.introspection.table_names(cursor))
        return triggers, checks, tables

    def test_stepwise_reversal_to_0007_restores_each_previous_state_and_reapplies(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        names = sorted(
            name for app, name in executor.loader.graph.nodes if app == APP and "0007" <= name[:4]
        )
        self.assertEqual(names[0], "0007_dependency_and_progress_notes")
        start = self.schema()
        installed = {}  # the newest event trigger text at or below each migration
        current = None
        for name in names:
            current = event_trigger_text(name) or current
            installed[name] = current
        try:
            above = start
            for previous, name in reversed(list(zip(names, names[1:], strict=False))):
                MigrationExecutor(connection).migrate([(APP, previous)])
                below = self.schema()
                with self.subTest(reversed=name):
                    trigger = below[0]["cya_deficiency_event_bi"]
                    self.assertIn(trigger, installed[previous])
                    for table in above[2] - below[2]:  # a dropped table leaves no CHECK behind
                        self.assertFalse(any(t == table for t, _ in below[1]), table)
                    if name.startswith("0018"):
                        clause = below[1][
                            ("deficiency_acknowledgement_rule", "defackrule_max_days")
                        ]
                        self.assertNotIn("30", clause)  # back to the one-sided 0010 CHECK
                above = below
            # At 0007 nothing from the later lifecycle remains.
            trigger = above[0]["cya_deficiency_event_bi"]
            for event_type in (
                "EXTENSION_REQUESTED",
                "ESCALATION_ACKNOWLEDGED",
                "ADMINISTRATOR_CLOSED",
                "EMPTY_SCOPE_CLOSED",
                "CHECKS_SUSPENDED",
            ):
                self.assertNotIn(event_type, trigger)
            for table in ("deficiency_age_rule", "deficiency_acknowledgement_rule"):
                self.assertNotIn(table, above[2])
        finally:
            MigrationExecutor(connection).migrate(leaves)
        self.assertEqual(self.schema(), start)
