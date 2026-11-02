"""Direct-insert guards, the 30-day maximum cap and races for the M4 lifecycle events (issue #20).

Each insert runs in a transaction that is always rolled back, so one valid baseline can be varied
one rule at a time. The baseline is accepted first, so every refusal below is meaningful.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, connections, transaction

from cya.core.audit_repository import append_receipt
from cya.deficiencies import acknowledgement, closure
from cya.deficiencies.models import DeficiencyAcknowledgementRule, DeficiencyEvent
from cya.deficiencies.tests.test_acknowledgement import AcknowledgementCase


class Rollback(Exception):
    """Raised inside an accepted insert so that nothing it wrote is kept."""


class GuardCase(AcknowledgementCase):
    def receipt(self, command_type, actor):
        return append_receipt(
            organization=self.org,
            operation_id=str(uuid4()),
            actor_user=actor,
            command_type=command_type,
            request_digest="0" * 64,
            response_status=201,
            response_body={"code": "SYNTHETIC"},
            correlation_id=str(uuid4()),
        )

    def attempt(self, *, command_type, actor, until=None, deficiency_id=None, **fields):
        """Insert one event after the deficiency's newest; None if accepted, else the refusal."""
        deficiency_id = deficiency_id or self.deficiency_id
        try:
            with transaction.atomic():
                receipt = self.receipt(command_type, actor)
                last = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest(
                    "sequence"
                )
                values = {
                    "organization_id": self.org.pk,
                    "deficiency_id": deficiency_id,
                    "sequence": last.sequence + 1,
                    "predecessor_event": last,
                    "from_status": last.to_status,
                    "command": receipt,
                    "recorded_at": receipt.recorded_at,
                    "actor_user": actor,
                    **fields,
                }
                if until is not None:
                    values["acknowledgement_until"] = receipt.recorded_at + until
                DeficiencyEvent.objects.create(**values)
                raise Rollback
        except Rollback:
            return None
        except DatabaseError as error:
            return str(error)

    def assert_refused(self, refusal, *expected):
        self.assertIsNotNone(refusal, "the database accepted an invalid row")
        self.assertTrue(any(text in refusal for text in expected), refusal)

    def closed(self, actor=None):
        closure.close_administrator(
            explanation="Synthetic close.", **self.args(actor=actor or self.admin)
        )


class AcknowledgementGuardTests(GuardCase):
    INVALID = "acknowledgement fields or status are invalid"

    def ack(self, until=timedelta(days=2), actor=None, **fields):
        values = {
            "event_type": "ESCALATION_ACKNOWLEDGED",
            "to_status": "OPEN",
            "explanation": "Synthetic review.",
            "action_plan": "Synthetic plan.",
            "actor_roles_at_time": ["SUPERVISOR"],
            **fields,
        }
        command_type = values.pop("command_type", acknowledgement.ACK_COMMAND)
        return self.attempt(
            command_type=command_type, actor=actor or self.supervisor, until=until, **values
        )

    def test_boundaries_are_accepted_and_each_rule_refuses_its_violation(self):
        self.set_maximum(3)
        self.assertIsNone(self.ack())
        self.assertIsNone(self.ack(until=timedelta(days=1)))  # the one-day minimum itself
        self.assertIsNone(self.ack(until=timedelta(days=3)))  # exactly the configured maximum
        self.assertIsNone(self.ack(actor=self.admin, actor_roles_at_time=["ADMINISTRATOR"]))
        cases = {
            "under one day": (
                {"until": timedelta(days=1) - timedelta(seconds=1)},
                self.INVALID,
            ),
            "over the maximum": (
                {"until": timedelta(days=3, seconds=1)},
                "exceeds the configured maximum",
            ),
            "crew role snapshot": (
                {"actor_roles_at_time": ["CREW"]},
                "only a supervisor or administrator may acknowledge",
            ),
            "wrong-command receipt": (
                {"command_type": "DEFICIENCY_ASSIGN"},
                "acknowledgement needs its own command",
                "command",
            ),
            "blank explanation": ({"explanation": "   "}, self.INVALID),
            "missing explanation": ({"explanation": None}, self.INVALID),
            "blank action plan": ({"action_plan": " "}, self.INVALID),
            "missing action plan": ({"action_plan": None}, self.INVALID),
            "status change": ({"to_status": "IN_PROGRESS"}, self.INVALID),
        }
        for name, (fields, *expected) in cases.items():
            with self.subTest(name):
                self.assert_refused(self.ack(**fields), *expected)

    def test_a_missing_receipt_is_refused(self):
        self.set_maximum(3)
        last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest("sequence")
        with self.assertRaises(DatabaseError), transaction.atomic():
            DeficiencyEvent.objects.create(
                organization_id=self.org.pk,
                deficiency_id=self.deficiency_id,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type="ESCALATION_ACKNOWLEDGED",
                from_status="OPEN",
                to_status="OPEN",
                explanation="Synthetic review.",
                action_plan="Synthetic plan.",
                command_id=uuid4(),
                recorded_at=last.recorded_at + timedelta(seconds=1),
                acknowledgement_until=last.recorded_at + timedelta(days=2),
                actor_user=self.supervisor,
                actor_roles_at_time=["SUPERVISOR"],
            )

    def test_an_inactive_deficiency_cannot_be_acknowledged(self):
        self.set_maximum(3)
        self.closed()
        refusal = self.ack(to_status="CLOSED_ADMINISTRATOR")
        self.assert_refused(refusal, self.INVALID)


class MaximumCapTests(GuardCase):
    def rule(self, days):
        """Insert a rule row directly; None if accepted (rolled back), else the refusal."""
        try:
            with transaction.atomic():
                receipt = self.receipt(acknowledgement.RULE_COMMAND, self.admin)
                DeficiencyAcknowledgementRule.objects.create(
                    id=uuid4(),
                    organization=self.org,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    actor_user=self.admin,
                    actor_roles_at_time=["ADMINISTRATOR"],
                    operation_id=receipt.operation_id,
                    revision=DeficiencyAcknowledgementRule.objects.filter(
                        organization=self.org
                    ).count()
                    + 1,
                    maximum_days=days,
                )
                raise Rollback
        except Rollback:
            return None
        except DatabaseError as error:
            return str(error)

    def test_the_command_accepts_30_days_and_refuses_31(self):
        self.set_maximum(5)  # an existing smaller value is unaffected
        self.assertEqual(self.set_maximum(30).body["revision"], 2)
        for value in (31, 365, 2_147_483_647, 0, -1, None, True, "30", 30.0):
            with self.subTest(value=value):
                error = self.refused("MALFORMED_REQUEST", 400, self.set_maximum, value)
                self.assertEqual(error.path, "maximum_days")
        self.assertEqual(
            list(
                DeficiencyAcknowledgementRule.objects.order_by("revision").values_list(
                    "maximum_days", flat=True
                )
            ),
            [5, 30],
        )

    def test_the_database_refuses_more_than_30_days(self):
        self.assertIsNone(self.rule(30))
        self.assertIsNone(self.rule(1))
        self.assert_refused(self.rule(31), "1 to 30 days")
        self.assert_refused(self.rule(0), "1 to 30 days")

    def test_the_table_check_carries_the_cap(self):
        # The trigger fires first on insert, so the CHECK is confirmed from the schema itself.
        with connections["default"].cursor() as cursor:
            cursor.execute(
                "SELECT CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=DATABASE() AND CONSTRAINT_NAME=%s",
                ["defackrule_max_days"],
            )
            (clause,) = cursor.fetchone()
        self.assertIn("<= 30", clause.replace("`", ""))
        self.assertIn(">= 1", clause.replace("`", ""))


class ClosureGuardTests(GuardCase):
    def change(self, event_type, command_type, actor=None, **fields):
        values = {
            "event_type": event_type,
            "explanation": "Synthetic explanation.",
            "actor_roles_at_time": ["ADMINISTRATOR"],
            **fields,
        }
        return self.attempt(command_type=command_type, actor=actor or self.admin, **values)

    def test_administrator_close_rules(self):
        close = ("ADMINISTRATOR_CLOSED", closure.CLOSE_COMMAND)
        self.assertIsNone(self.change(*close, to_status="CLOSED_ADMINISTRATOR"))
        cases = {
            "wrong-command receipt": (
                ("ADMINISTRATOR_CLOSED", closure.REOPEN_COMMAND),
                {"to_status": "CLOSED_ADMINISTRATOR"},
                "command",
            ),
            "supervisor role snapshot": (
                close,
                {
                    "to_status": "CLOSED_ADMINISTRATOR",
                    "actor": self.supervisor,
                    "actor_roles_at_time": ["SUPERVISOR"],
                },
                "only an administrator may close or reopen",
            ),
            "wrong target status": (
                close,
                {"to_status": "CLOSED_VERIFIED"},
                "administrator close must close an active deficiency",
            ),
            "blank explanation": (
                close,
                {"to_status": "CLOSED_ADMINISTRATOR", "explanation": "  "},
                "defevent_admin_close_shape",
                "defevent_explanation",
            ),
            "missing explanation": (
                close,
                {"to_status": "CLOSED_ADMINISTRATOR", "explanation": None},
                "defevent_admin_close_shape",
            ),
            "carries a deadline": (
                close,
                {"to_status": "CLOSED_ADMINISTRATOR", "deadline": self.now + timedelta(days=1)},
                "defevent_admin_close_shape",
            ),
        }
        for name, (kind, fields, *expected) in cases.items():
            with self.subTest(name):
                self.assert_refused(self.change(*kind, **fields), *expected)

    def test_closing_an_already_closed_deficiency_is_refused(self):
        self.closed()
        refusal = self.change(
            "ADMINISTRATOR_CLOSED", closure.CLOSE_COMMAND, to_status="CLOSED_ADMINISTRATOR"
        )
        self.assert_refused(refusal, "administrator close must close an active deficiency")

    def test_administrator_reopen_rules(self):
        reopen = ("ADMINISTRATOR_REOPENED", closure.REOPEN_COMMAND)
        # Reopening an open deficiency is refused.
        self.assert_refused(
            self.change(*reopen, to_status="OPEN"),
            "can be reopened",  # 0011's and 0013's copies of this rule word it differently
        )
        self.closed()
        self.assertIsNone(self.change(*reopen, to_status="OPEN"))
        cases = {
            "wrong target status": ({"to_status": "IN_PROGRESS"}, "can be reopened"),
            "supervisor role snapshot": (
                {
                    "to_status": "OPEN",
                    "actor": self.supervisor,
                    "actor_roles_at_time": ["SUPERVISOR"],
                },
                "only an administrator may close or reopen",
            ),
            "blank explanation": (
                {"to_status": "OPEN", "explanation": " "},
                "defevent_admin_close_shape",
                "defevent_explanation",
            ),
        }
        for name, (fields, *expected) in cases.items():
            with self.subTest(name):
                self.assert_refused(self.change(*reopen, **fields), *expected)
        self.assert_refused(
            self.change("ADMINISTRATOR_REOPENED", closure.CLOSE_COMMAND, to_status="OPEN"),
            "command",
        )

    def test_empty_scope_close_rules_and_reopen_from_unverifiable(self):
        empty = ("EMPTY_SCOPE_CLOSED", closure.EMPTY_SCOPE_CLOSE_COMMAND)
        supervisor = {"actor": self.supervisor, "actor_roles_at_time": ["SUPERVISOR"]}
        self.assertIsNone(self.change(*empty, to_status="CLOSED_UNVERIFIABLE", **supervisor))
        self.assertIsNone(self.change(*empty, to_status="CLOSED_UNVERIFIABLE"))  # administrator
        cases = {
            "crew role snapshot": (
                {
                    "to_status": "CLOSED_UNVERIFIABLE",
                    "actor": self.crew,
                    "actor_roles_at_time": ["CREW"],
                },
                "only a supervisor or administrator may close an empty-scope deficiency",
            ),
            "wrong-command receipt": (
                {"to_status": "CLOSED_UNVERIFIABLE", "command_type": closure.CLOSE_COMMAND},
                "command",
            ),
            "wrong target status": (
                {"to_status": "CLOSED_ADMINISTRATOR", **supervisor},
                "empty-scope close must close an active deficiency",
            ),
            "blank explanation": (
                {"to_status": "CLOSED_UNVERIFIABLE", "explanation": "  ", **supervisor},
                "defevent_admin_close_shape",
                "defevent_explanation",
            ),
        }
        for name, (fields, *expected) in cases.items():
            fields = dict(fields)
            command_type = fields.pop("command_type", empty[1])
            with self.subTest(name):
                self.assert_refused(self.change(empty[0], command_type, **fields), *expected)

        # Keep one empty-scope closure, then an administrator may reopen it to Open.
        with transaction.atomic():
            receipt = self.receipt(closure.EMPTY_SCOPE_CLOSE_COMMAND, self.supervisor)
            last = DeficiencyEvent.objects.filter(deficiency_id=self.deficiency_id).latest(
                "sequence"
            )
            DeficiencyEvent.objects.create(
                organization_id=self.org.pk,
                deficiency_id=self.deficiency_id,
                sequence=last.sequence + 1,
                predecessor_event=last,
                event_type="EMPTY_SCOPE_CLOSED",
                from_status=last.to_status,
                to_status="CLOSED_UNVERIFIABLE",
                explanation="Synthetic empty scope.",
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.supervisor,
                actor_roles_at_time=["SUPERVISOR"],
            )
        self.assertIsNone(
            self.change("ADMINISTRATOR_REOPENED", closure.REOPEN_COMMAND, to_status="OPEN")
        )
        self.assert_refused(
            self.change(*empty, to_status="CLOSED_UNVERIFIABLE", **supervisor),
            "empty-scope close must close an active deficiency",
        )


class AcknowledgementRaceTests(GuardCase):
    """Writers are serialized by the deficiency and organization locks (issue #20)."""

    def race(self, calls):
        def run(call):
            try:
                return call()
            except Exception as error:  # the loser fails with a refusal
                return error
            finally:
                connections.close_all()

        with ThreadPoolExecutor(len(calls)) as pool:
            return list(pool.map(run, calls))

    def test_two_acknowledgements_naming_the_same_event_record_exactly_one(self):
        self.set_maximum(5)
        newest = self.newest()
        outcomes = self.race(
            [lambda d=days: self.acknowledge(d, expected_event_id=newest) for days in (2, 3)]
        )
        losses = [o for o in outcomes if isinstance(o, Exception)]
        self.assertEqual((len(outcomes) - len(losses), len(losses)), (1, 1))
        self.assertEqual((losses[0].code, losses[0].status), ("DEFICIENCY_CHANGED", 409))
        self.assertEqual(
            DeficiencyEvent.objects.filter(event_type="ESCALATION_ACKNOWLEDGED").count(), 1
        )

    def test_an_acknowledgement_racing_a_lower_maximum_never_exceeds_the_rule_in_force(self):
        self.set_maximum(5)
        outcomes = self.race([lambda: self.acknowledge(5), lambda: self.set_maximum(2)])
        self.assertFalse(isinstance(outcomes[1], Exception), outcomes[1])  # the setter wins
        if isinstance(outcomes[0], Exception):
            self.assertEqual(outcomes[0].code, "ACKNOWLEDGEMENT_PERIOD_TOO_LONG")
        rules = list(DeficiencyAcknowledgementRule.objects.order_by("revision"))
        for event in DeficiencyEvent.objects.filter(event_type="ESCALATION_ACKNOWLEDGED"):
            in_force = [r for r in rules if r.recorded_at <= event.recorded_at][-1]
            self.assertLessEqual(
                event.acknowledgement_until - event.recorded_at,
                timedelta(days=in_force.maximum_days),
            )
