"""Escalation acknowledgements and the administrator maximum (issue 151 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/151); DEF-7)."""

import json
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from cya.authorization.commands import change_role
from cya.checklists.tests.support import ids
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies import acknowledgement
from cya.deficiencies.models import DeficiencyAcknowledgementRule, DeficiencyEvent
from cya.deficiencies.tests.test_assignment import LifecycleCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

EXPLANATION = "The supervisor reviewed the repair plan."
ACTION_PLAN = "The assigned mechanic will replace the switch."


class AcknowledgementCase(LifecycleCase):
    def setUp(self):
        super().setUp()
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(self.supply.pk),
            role="CREW",
            action="REVOKE",
            **ids(),
        )

    def set_maximum(self, days=3, actor=None):
        return acknowledgement.set_acknowledgement_maximum(
            maximum_days=days,
            **self.context(actor or self.admin),
        )

    def acknowledge(self, days=2, actor=None, **overrides):
        args = {
            "explanation": EXPLANATION,
            "action_plan": ACTION_PLAN,
            "period_days": days,
            **self.args(actor=actor or self.supervisor),
        }
        args.update(overrides)
        return acknowledgement.acknowledge_escalation(**args)


class AcknowledgementCommandTests(AcknowledgementCase):
    def test_acknowledgement_requires_a_configured_maximum_and_respects_it(self):
        before = self.snapshot()
        error = self.refused("ACKNOWLEDGEMENT_MAXIMUM_UNCONFIGURED", 422, self.acknowledge)
        self.assertEqual(error.path, "period_days")
        self.assertEqual(self.snapshot(), before)
        self.set_maximum(2)
        before = self.snapshot()
        error = self.refused("ACKNOWLEDGEMENT_PERIOD_TOO_LONG", 422, self.acknowledge, 3)
        self.assertEqual(error.path, "period_days")
        self.assertEqual(self.snapshot(), before)

    def test_supervisor_acknowledgement_is_audited_and_contains_no_free_text_in_audit(self):
        self.set_maximum(3)
        result = self.acknowledge(2)
        event = DeficiencyEvent.objects.get(pk=result.body["event_id"])
        self.assertEqual(result.status, 201)
        self.assertEqual(result.body["code"], "DEFICIENCY_ESCALATION_ACKNOWLEDGED")
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status),
            ("ESCALATION_ACKNOWLEDGED", "OPEN", "OPEN"),
        )
        self.assertEqual(event.explanation, EXPLANATION)
        self.assertEqual(event.action_plan, ACTION_PLAN)
        self.assertGreater(event.acknowledgement_until, event.recorded_at)
        receipt = CommandReceipt.objects.get(pk=event.command_id)
        self.assertEqual(receipt.command_type, acknowledgement.ACK_COMMAND)
        audit = AuditEvent.objects.get(command=receipt)
        snapshot = json.dumps([audit.prior_snapshot, audit.new_snapshot])
        self.assertNotIn(EXPLANATION, snapshot)
        self.assertNotIn(ACTION_PLAN, snapshot)
        self.assertEqual(self.projection().last_applied_event_id, event.pk)

    def test_only_supervisors_and_administrators_can_acknowledge(self):
        self.set_maximum()
        with self.assertRaises(CommandDenied):
            self.acknowledge(actor=self.crew)
        with self.assertRaises(CommandDenied):
            self.acknowledge(actor=self.supply)
        self.assertEqual(
            self.acknowledge(actor=self.admin).body["code"],
            "DEFICIENCY_ESCALATION_ACKNOWLEDGED",
        )

    def test_explanation_plan_and_period_are_required_and_bounded(self):
        self.set_maximum(4)
        for field, value in (
            ("explanation", ""),
            ("explanation", " "),
            ("explanation", "x" * 4001),
            ("action_plan", ""),
            ("action_plan", None),
            ("action_plan", "x" * 4001),
            ("period_days", 0),
            ("period_days", True),
            ("period_days", "2"),
        ):
            self.refused("MALFORMED_REQUEST", 400, self.acknowledge, **{field: value})
        self.set_maximum(2_147_483_647)
        self.refused("ACKNOWLEDGEMENT_PERIOD_OUT_OF_RANGE", 422, self.acknowledge, 2_147_483_647)

    def test_operation_replay_returns_the_original_acknowledgement(self):
        self.set_maximum()
        arguments = {
            "explanation": EXPLANATION,
            "action_plan": ACTION_PLAN,
            "period_days": 2,
            **self.args(),
        }
        first = acknowledgement.acknowledge_escalation(**arguments)
        second = acknowledgement.acknowledge_escalation(**arguments)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertFalse(second.operation_reused)
        self.assertEqual(first.body, second.body)
        self.assertEqual(
            DeficiencyEvent.objects.filter(
                deficiency_id=self.deficiency_id, event_type="ESCALATION_ACKNOWLEDGED"
            ).count(),
            1,
        )

    def test_renewals_are_audited_and_as_of_replay_derives_active_expired_and_renewed(self):
        self.set_maximum(3)
        first_now = timezone.now()
        with mock.patch("cya.deficiencies.acknowledgement.timezone.now", return_value=first_now):
            first = self.acknowledge(1)
        first_event = DeficiencyEvent.objects.get(pk=first.body["event_id"])
        second_now = first_event.acknowledgement_until + timedelta(hours=1)
        with mock.patch("cya.deficiencies.acknowledgement.timezone.now", return_value=second_now):
            second = self.acknowledge(2)
        second_event = DeficiencyEvent.objects.get(pk=second.body["event_id"])
        between = acknowledgement.acknowledgement_state(
            str(self.org.pk), self.deficiency_id, first_event.acknowledgement_until
        )
        self.assertEqual(between["acknowledgement"]["status"], "EXPIRED")
        current = acknowledgement.acknowledgement_state(
            str(self.org.pk), self.deficiency_id, second_now
        )
        self.assertEqual(current["acknowledgement"]["status"], "ACTIVE")
        self.assertEqual([event["state"] for event in current["history"]], ["RENEWED", "ACTIVE"])
        third_now = second_event.recorded_at + timedelta(hours=1)
        with mock.patch("cya.deficiencies.acknowledgement.timezone.now", return_value=third_now):
            self.assertEqual(self.acknowledge(1).status, 201)  # renewal while active
        self.assertEqual(
            DeficiencyEvent.objects.filter(
                deficiency_id=self.deficiency_id, event_type="ESCALATION_ACKNOWLEDGED"
            ).count(),
            3,
        )
        self.assertGreater(second_event.acknowledgement_until, first_event.acknowledgement_until)

    def test_stale_expected_event_and_closed_deficiency_fail_without_appending(self):
        self.set_maximum()
        stale = self.newest()
        self.acknowledge()
        before = DeficiencyEvent.objects.count()
        self.refused("DEFICIENCY_CHANGED", 409, self.acknowledge, expected_event_id=stale)
        from cya.deficiencies.models import DeficiencyProjection

        DeficiencyProjection.objects.filter(deficiency_id=self.deficiency_id).update(
            status="CLOSED_VERIFIED"
        )
        self.refused("DEFICIENCY_NOT_ACTIVE", 422, self.acknowledge)
        self.assertEqual(DeficiencyEvent.objects.count(), before)


class AcknowledgementMaximumTests(AcknowledgementCase):
    def test_setting_maximum_is_versioned_audited_and_admin_only(self):
        with self.assertRaises(CommandDenied):
            self.set_maximum(3, self.supervisor)
        first = self.set_maximum(3)
        second = self.set_maximum(4)
        self.assertEqual((first.body["revision"], second.body["revision"]), (1, 2))
        self.assertEqual(DeficiencyAcknowledgementRule.objects.count(), 2)
        audit = AuditEvent.objects.filter(action=acknowledgement.RULE_COMMAND).latest("recorded_at")
        self.assertEqual(audit.new_snapshot["values"]["maximum_days"], 4)
        self.refused("ACKNOWLEDGEMENT_MAXIMUM_UNCHANGED", 422, self.set_maximum, 4)
        for value in (None, 0, True, "3", -1):
            self.refused("MALFORMED_REQUEST", 400, self.set_maximum, value)

    def test_maximum_reader_reconstructs_the_rule_as_of_a_time(self):
        self.set_maximum(3)
        recorded_at = DeficiencyAcknowledgementRule.objects.get(revision=1).recorded_at
        self.set_maximum(5)
        historical = acknowledgement.acknowledgement_maximum(str(self.org.pk), recorded_at)
        self.assertEqual(historical["maximum"]["maximum_days"], 3)
        self.assertEqual(len(historical["history"]), 1)


class AcknowledgementDatabaseGuardTests(AcknowledgementCase):
    def test_direct_insert_cannot_exceed_the_configured_maximum(self):
        self.set_maximum(1)
        event = DeficiencyEvent.objects.latest("sequence")
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                receipt = CommandReceipt.objects.create(
                    organization=self.org,
                    operation_id=uuid4(),
                    actor_user=self.supervisor,
                    command_type=acknowledgement.ACK_COMMAND,
                    request_digest="0" * 64,
                    result_schema_version=1,
                    response_status=201,
                    response_body={},
                    recorded_at=timezone.now(),
                    correlation_id=uuid4(),
                )
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO deficiency_event
                    (id, organization_id, command_id, recorded_at, actor_user_id,
                     actor_roles_at_time, deficiency_id, sequence, predecessor_event_id,
                     event_type, from_status, to_status, explanation, action_plan,
                     acknowledgement_until)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ESCALATION_ACKNOWLEDGED',
                     'OPEN','OPEN','reason','plan',DATE_ADD(%s, INTERVAL 2 DAY))""",
                        [
                            str(uuid4()),
                            str(self.org.pk),
                            str(receipt.pk),
                            receipt.recorded_at,
                            str(self.supervisor.pk),
                            '["SUPERVISOR"]',
                            self.deficiency_id,
                            event.sequence + 1,
                            str(event.pk),
                            receipt.recorded_at,
                        ],
                    )


@override_settings(**real_collaborator_settings())
class AcknowledgementApiTests(AcknowledgementCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client, self.user = self.harness.sign_in("supervisor-http")
        self.grant(self.user, "SUPERVISOR")
        self.set_maximum(3)

    def post(self, path, body):
        self.client.get("/")
        token = self.client.cookies["csrftoken"].value
        return self.client.post(
            path,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    def test_http_acknowledgement_and_maximum_routes_follow_the_json_contract(self):
        response = self.post(
            f"/api/v1/deficiencies/{self.deficiency_id}/escalation-acknowledgements",
            {
                "operation_id": str(uuid4()),
                "expected_event_id": self.newest(),
                "explanation": EXPLANATION,
                "action_plan": ACTION_PLAN,
                "period_days": 2,
            },
        )
        self.assertEqual((response.status_code, response.json()["status"]), (201, "COMMITTED"))
        read = self.client.get(
            f"/api/v1/deficiencies/{self.deficiency_id}/escalation-acknowledgement"
        )
        self.assertEqual(read.json()["acknowledgement"]["status"], "ACTIVE")
        self.assertEqual(
            self.client.get("/api/v1/deficiency-acknowledgement-maximum").json()["maximum"][
                "maximum_days"
            ],
            3,
        )
