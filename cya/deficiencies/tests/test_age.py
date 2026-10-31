"""Age highlighting rule revisions and what they mark (issue 150 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/150); SPEC DEF-5, DEF-6, AUD-4)."""

import json
from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies import deadlines, service
from cya.deficiencies.age import set_age_rule
from cya.deficiencies.models import DeficiencyAgeRule
from cya.deficiencies.tests.test_extensions import ExtensionCase


class AgeCase(ExtensionCase):
    def rule(self, thresholds=(7, 30), age=60, extensions=3, **kw):
        context = self.context(kw.pop("actor", self.admin))
        context.update(kw)
        return set_age_rule(
            age_thresholds_days=list(thresholds),
            long_running_age_days=age,
            long_running_extensions=extensions,
            **context,
        )

    def rules(self):
        return DeficiencyAgeRule.objects.filter(organization=self.org).order_by("revision")

    def marks(self, days, **kw):
        at = timezone.now() + timedelta(days=days)
        state = self.state(now=at, **kw)
        return state["age_level"], state["long_running"], state["age_rule_revision"]


class SetAgeRuleTests(AgeCase):
    def test_an_administrator_records_a_revision_and_the_audit_holds_the_numbers_only(self):
        result = self.rule()
        self.assertEqual((result.status, result.body["revision"]), (201, 1))
        row = self.rules().get()
        self.assertEqual(
            (row.age_thresholds_days, row.long_running_age_days, row.long_running_extensions),
            ([7, 30], 60, 3),
        )
        self.assertEqual(row.actor_user_id, self.admin.pk)
        self.assertEqual(
            CommandReceipt.objects.get(pk=row.command_id).command_type, "DEFICIENCY_AGE_RULE_SET"
        )
        audit = AuditEvent.objects.get(action="DEFICIENCY_AGE_RULE_SET")
        self.assertEqual(audit.outcome, "SUCCEEDED")
        self.assertEqual(audit.new_snapshot["values"]["age_thresholds_days"], [7, 30])
        self.assertEqual(json.loads(json.dumps(audit.prior_snapshot))["values"], {"revision": 0})

    def test_only_an_administrator_may_set_it(self):
        for actor in (self.crew, self.supervisor, self.supply):
            with self.assertRaises(CommandDenied):
                self.rule(actor=actor)
        self.assertEqual(self.rules().count(), 0)

    def test_every_revision_is_kept_and_an_identical_one_is_refused(self):
        self.rule()
        self.refused("AGE_RULE_UNCHANGED", 422, self.rule)
        self.rule(thresholds=(10,), age=None, extensions=None)
        self.rule(thresholds=(), age=None, extensions=None)  # switches highlighting off
        self.refused("AGE_RULE_UNCHANGED", 422, self.rule, thresholds=(), age=None, extensions=None)
        self.assertEqual([r.revision for r in self.rules()], [1, 2, 3])

    def test_nothing_to_switch_off_is_refused_before_any_rule_exists(self):
        self.refused("AGE_RULE_UNCHANGED", 422, self.rule, thresholds=(), age=None, extensions=None)

    def test_malformed_input_is_refused_before_anything_is_written(self):
        for kw in (
            {"thresholds": (5, 5)},
            {"thresholds": (9, 3)},
            {"thresholds": (0,)},
            {"thresholds": (3651,)},
            {"thresholds": (1, 2, 3, 4, 5, 6)},
            {"thresholds": ("7",)},
            {"thresholds": (True,)},
            {"age": 0},
            {"age": 3651},
            {"age": "30"},
            {"extensions": 0},
            {"extensions": 1001},
            {"extensions": 1.5},
        ):
            self.refused("MALFORMED_REQUEST", 400, self.rule, **kw)
        self.refused(
            "MALFORMED_REQUEST",
            400,
            set_age_rule,
            age_thresholds_days="7",
            long_running_age_days=None,
            long_running_extensions=None,
            **self.context(self.admin),
        )
        self.assertEqual(self.rules().count(), 0)

    def test_a_replay_returns_the_stored_result(self):
        operation_id = str(uuid4())
        first = self.rule(operation_id=operation_id)
        again = self.rule(operation_id=operation_id)
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.rules().count(), 1)
        reused = self.rule(thresholds=(9,), operation_id=operation_id)
        self.assertTrue(reused.operation_reused)
        self.assertEqual(self.rules().count(), 1)


class HighlightTests(AgeCase):
    def test_with_no_rule_nothing_is_highlighted_but_age_and_count_are_facts(self):
        self.deadline(2)
        self.ask(days=6, actor=self.crew)
        state = self.state(now=timezone.now() + timedelta(days=400))
        self.assertEqual(
            (state["age_level"], state["long_running"], state["age_rule_revision"]),
            (None, False, None),
        )
        self.assertGreaterEqual(state["age_seconds"], 400 * 86400)
        self.assertEqual(state["extension_count"], 1)

    def test_thresholds_and_the_long_running_age_mark_a_case(self):
        self.rule(thresholds=(7, 30), age=60, extensions=None)
        self.assertEqual(self.marks(1), (0, False, 1))
        self.assertEqual(self.marks(8), (1, False, 1))
        self.assertEqual(self.marks(31), (2, False, 1))
        self.assertEqual(self.marks(61), (2, True, 1))

    def test_the_extension_count_marks_a_case_regardless_of_age(self):
        self.rule(thresholds=(), age=None, extensions=2)
        self.deadline(2)
        self.ask(days=6, actor=self.crew)
        self.assertEqual(self.marks(0)[1], False)
        self.ask(days=7, actor=self.crew)
        self.assertEqual(self.marks(0), (0, True, 1))

    def test_the_rule_in_force_at_the_instant_decides_and_the_past_is_not_rewritten(self):
        before = timezone.now()
        self.rule(thresholds=(1,), age=None, extensions=None)
        middle = timezone.now()
        self.rule(thresholds=(1000,), age=None, extensions=None)
        # a later revision does not change what the first one marked at an instant in its span
        first = self.state(as_of=middle, now=None)
        self.assertEqual(first["age_rule_revision"], 1)
        self.assertEqual(self.state(as_of=before)["age_rule_revision"], None)
        self.assertEqual(self.state()["age_rule_revision"], 2)

    def test_switching_the_rule_off_stops_the_highlighting(self):
        self.rule(thresholds=(1,), age=1, extensions=None)
        self.assertEqual(self.marks(5), (1, True, 1))
        self.rule(thresholds=(), age=None, extensions=None)
        self.assertEqual(self.marks(5), (0, False, 2))

    def test_the_pure_function_needs_no_database(self):
        from types import SimpleNamespace as R

        rule = R(
            revision=4,
            age_thresholds_days=[2, 4],
            long_running_age_days=5,
            long_running_extensions=3,
        )
        self.assertEqual(
            deadlines.highlight(rule, 5 * 86400, 0),
            {"age_rule_revision": 4, "age_level": 2, "long_running": True},
        )
        self.assertEqual(deadlines.highlight(rule, 86400, 3)["long_running"], True)
        self.assertEqual(deadlines.highlight(None, 10**9, 99)["age_level"], None)


class MalformedRuleTests(AgeCase):
    def test_a_stored_rule_no_command_could_have_written_is_refused_as_unavailable(self):
        from types import SimpleNamespace as R

        bad = R(
            revision=9,
            age_thresholds_days=[7, "bad"],
            long_running_age_days=None,
            long_running_extensions=None,
        )
        with self.assertRaises(service.DeficiencyError) as caught:
            service.highlight_of(bad, 86400, 0)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("CONFIGURATION_UNAVAILABLE", 503)
        )


class AgeRuleGuardTests(AgeCase):
    """The database refuses what the command would not write (migration 0009)."""

    def receipt(self, operation_id, command_type="DEFICIENCY_AGE_RULE_SET", actor=None):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=operation_id,
                actor_user=actor or self.admin,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def row(self, revision=1, command_type="DEFICIENCY_AGE_RULE_SET", **overrides):
        operation_id = str(uuid4())
        receipt = self.receipt(operation_id, command_type)
        return DeficiencyAgeRule.objects.create(
            **{
                "organization": self.org,
                "command": receipt,
                "recorded_at": receipt.recorded_at,
                "actor_user": self.admin,
                "actor_roles_at_time": ["ADMINISTRATOR"],
                "operation_id": operation_id,
                "revision": revision,
                "age_thresholds_days": [7],
                **overrides,
            }
        )

    def refused_row(self, **kw):
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.row(**kw)

    def test_a_valid_row_is_accepted_so_the_refusals_below_are_meaningful(self):
        with transaction.atomic():
            self.row()
        # the limits of what the command accepts are accepted by the database too
        for revision, thresholds in enumerate(([1, 2, 3, 4, 5], [3650], [], [7, 30, 100]), 2):
            with transaction.atomic():
                self.row(revision=revision, age_thresholds_days=thresholds)

    def test_the_trigger_and_checks_repeat_the_command_rules(self):
        self.refused_row(command_type="DEFICIENCY_ASSIGN")  # not its own command
        self.refused_row(revision=2)  # not the next revision
        self.refused_row(revision=0)
        self.refused_row(age_thresholds_days={"a": 1})
        self.refused_row(age_thresholds_days=[1, 2, 3, 4, 5, 6])
        for bad in (
            [7, "bad"],
            ["7"],
            [7, 7],
            [9, 3],
            [0],
            [-1],
            [3651],
            [7.5],
            [True],
            [None],
            [[7]],
        ):
            self.refused_row(age_thresholds_days=bad)
        self.refused_row(long_running_age_days=0)
        self.refused_row(long_running_age_days=3651)
        self.refused_row(long_running_extensions=0)
        self.refused_row(actor_roles_at_time=["SUPER_USER"])
        with transaction.atomic():
            self.row()
        self.refused_row(revision=1)  # a revision number is used once
        with transaction.atomic():
            self.row(revision=2)

    def test_a_revision_can_be_neither_changed_nor_removed(self):
        with transaction.atomic():
            row = self.row()
        for sql in (
            "UPDATE deficiency_age_rule SET revision=9 WHERE id=%s",
            "DELETE FROM deficiency_age_rule WHERE id=%s",
        ):
            with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as c:
                c.execute(sql, [row.pk])
