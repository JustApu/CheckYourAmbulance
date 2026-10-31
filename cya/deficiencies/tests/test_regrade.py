"""Re-grading a deficiency's severity (issue 123 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/123); SPEC DEF-8, section 3.3)."""

import json
from datetime import timedelta
from unittest import mock
from uuid import uuid1, uuid4

from django.db import DatabaseError, transaction
from django.test import override_settings

from cya.checks.commands import correct_check, invalidate_chain
from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies import service
from cya.deficiencies.commands import add_link, regrade_severity
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyLink,
    DeficiencyProjection,
    DeficiencySeverity,
)
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.tests.support import DeficiencyCase
from cya.operations.models import MAX_TEXT, VehicleStatusEvent
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


class RegradeCase(DeficiencyCase):
    def setUp(self):
        super().setUp()
        self.deficiency_id = self.failed().body["deficiency_ids"][0]  # reported as HIGH

    def last_event(self, deficiency_id=None):
        return DeficiencyEvent.objects.filter(
            deficiency_id=deficiency_id or self.deficiency_id
        ).latest("sequence")

    def newest(self, deficiency_id=None):
        """The newest event's id, or a stranger's when the deficiency has none (malformed id)."""
        try:
            return self.last_event(deficiency_id).pk
        except DeficiencyEvent.DoesNotExist:
            return str(uuid4())

    def regrade(self, severity="LOW", *, actor=None, expected=None, deficiency_id=None, **extra):
        args = {
            "deficiency_id": deficiency_id or self.deficiency_id,
            "severity": severity,
            "explanation": "Synthetic reason for the new grade.",
            "expected_event_id": expected or self.newest(deficiency_id),
            **self.context(actor or self.supervisor),
            **extra,
        }
        return regrade_severity(**args)

    def snapshot(self):
        """Everything a re-grade must leave alone."""
        return {
            "header": list(Deficiency.objects.order_by("pk").values()),
            "evidence": DeficiencyEvidence.objects.count(),
            "links": DeficiencyLink.objects.count(),
            "vehicle_status": VehicleStatusEvent.objects.count(),
            "status": list(
                DeficiencyProjection.objects.order_by("pk").values_list(
                    "status", "assignee", "deadline"
                )
            ),
        }

    def retire(self, code, revision=2):
        old = self.level(code)
        return DeficiencySeverity.objects.create(
            organization=self.org,
            code=code,
            revision=revision,
            label=old.label,
            description=old.description,
            rank=old.rank,
            active=False,
            effective_at=self.now - timedelta(hours=1),
        )

    def refused(self, code, status, severity="LOW", **kwargs):
        before = (self.counts(), self.snapshot())
        with self.assertRaises(DeficiencyError) as caught:
            self.regrade(severity, **kwargs)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.assertEqual((self.counts(), self.snapshot()), before)
        return caught.exception


class RegradeCommandTests(RegradeCase):
    def test_a_supervisor_re_grades_and_the_event_keeps_both_levels_the_actor_and_the_instant(self):
        reported = self.last_event()
        before = self.snapshot()
        result = self.regrade("LOW")
        self.assertEqual(result.status, 201)
        event = self.last_event()
        self.assertEqual(result.body["event_id"], event.pk)
        self.assertEqual(result.body["previous_severity"], "HIGH")
        self.assertEqual(result.body["severity"], "LOW")
        self.assertEqual(
            (
                event.sequence,
                event.event_type,
                event.predecessor_event_id,
                event.from_status,
                event.to_status,
            ),
            (2, "SEVERITY_CHANGED", reported.pk, "OPEN", "OPEN"),
        )
        self.assertEqual(event.previous_severity_version, self.level("HIGH"))
        self.assertEqual(event.severity_version, self.level("LOW"))
        self.assertEqual(event.explanation, "Synthetic reason for the new grade.")
        self.assertEqual(
            (event.actor_user_id, event.actor_roles_at_time),
            (self.supervisor.pk, ["CREW", "SUPERVISOR"]),
        )
        receipt = CommandReceipt.objects.get(pk=event.command_id)
        self.assertEqual(
            (receipt.command_type, event.recorded_at), ("DEFICIENCY_REGRADE", receipt.recorded_at)
        )
        projection = DeficiencyProjection.objects.get(deficiency_id=self.deficiency_id)
        self.assertEqual(
            (projection.severity_version, projection.last_applied_event_id),
            (self.level("LOW"), event.pk),
        )
        # the original grade is never overwritten, and nothing else changed
        self.assertEqual(self.snapshot(), before)
        audit = AuditEvent.objects.get(action="DEFICIENCY_REGRADE")
        self.assertEqual(audit.outcome, "SUCCEEDED")
        self.assertNotIn("Synthetic reason", json.dumps(audit.new_snapshot))

    def test_only_supervisors_and_administrators_may_re_grade(self):
        before = (self.counts(), self.snapshot())
        for denied in (self.crew, self.supply):
            with self.assertRaises(CommandDenied):
                self.regrade("LOW", actor=denied)
        self.assertEqual((self.counts(), self.snapshot()), before)
        self.regrade("LOW", actor=self.supervisor)
        self.regrade("MEDIUM", actor=self.admin)
        self.assertEqual(
            list(
                DeficiencyEvent.objects.filter(event_type="SEVERITY_CHANGED")
                .order_by("sequence")
                .values_list("actor_roles_at_time", flat=True)
            ),
            [["CREW", "SUPERVISOR"], ["ADMINISTRATOR", "CREW"]],
        )

    def test_a_replay_returns_the_stored_answer_and_a_changed_request_is_reused(self):
        operation_id = str(uuid4())
        expected = self.last_event().pk
        first = self.regrade("LOW", operation_id=operation_id, expected=expected)
        counts = self.counts()
        again = self.regrade("LOW", operation_id=operation_id, expected=expected)
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.counts(), counts)
        different = self.regrade("MEDIUM", operation_id=operation_id, expected=expected)
        self.assertTrue(different.operation_reused)
        self.assertEqual(self.counts(), counts)

    def test_an_expected_event_that_is_not_the_newest_is_refused_as_stale(self):
        first = self.last_event().pk
        self.regrade("LOW")
        error = self.refused("DEFICIENCY_CHANGED", 409, "MEDIUM", expected=first)
        self.assertEqual(error.path, "expected_event_id")
        self.refused("DEFICIENCY_CHANGED", 409, "MEDIUM", expected=str(uuid4()))
        self.regrade("MEDIUM")  # naming the newest event works

    def test_a_stale_check_also_covers_another_kind_of_event_arriving_first(self):
        seen = self.last_event().pk
        self.failed(self.siren, deficiency={"existing_id": self.deficiency_id})
        self.regrade("LOW")  # evidence is not an event, so the deficiency is unchanged
        self.refused("DEFICIENCY_CHANGED", 409, "MEDIUM", expected=seen)

    def test_unknown_retired_future_and_unchanged_levels_are_refused(self):
        self.refused("INVALID_SEVERITY", 422, "NO_SUCH_LEVEL")
        self.refused("INVALID_SEVERITY", 422, "low")
        self.retire("LOW")
        self.refused("INVALID_SEVERITY", 422, "LOW")
        DeficiencySeverity.objects.create(
            organization=self.org,
            code="URGENT",
            revision=1,
            label="Urgent",
            description="Synthetic future level.",
            rank=5,
            active=True,
            effective_at=self.now + timedelta(days=1),
        )
        self.refused("INVALID_SEVERITY", 422, "URGENT")
        self.refused("SEVERITY_UNCHANGED", 422, "HIGH")

    def test_a_reworded_revision_of_the_current_level_is_still_the_same_level(self):
        old = self.level("HIGH")
        DeficiencySeverity.objects.create(
            organization=self.org,
            code="HIGH",
            revision=2,
            label="High",
            description="Synthetic reworded criteria.",
            rank=old.rank,
            active=True,
            effective_at=self.now - timedelta(hours=1),
        )
        self.refused("SEVERITY_UNCHANGED", 422, "HIGH")

    def test_an_administrator_added_level_can_be_chosen(self):
        DeficiencySeverity.objects.create(
            organization=self.org,
            code="URGENT",
            revision=1,
            label="Urgent",
            description="Synthetic added level.",
            rank=5,
            active=True,
            effective_at=self.now - timedelta(hours=1),
        )
        self.regrade("URGENT")
        self.assertEqual(self.last_event().severity_version.code, "URGENT")

    def test_malformed_requests_are_refused_without_a_write(self):
        good = {"severity": "LOW"}
        cases = (
            ({"deficiency_id": "not-an-id"}, "deficiency_id"),
            ({"expected_event_id": "NOT-AN-ID"}, "expected_event_id"),
            ({"expected_event_id": str(uuid4()).upper()}, "expected_event_id"),
            ({"severity": ""}, "severity"),
            ({"severity": 3}, "severity"),
            ({"severity": "X" * 31}, "severity"),
            ({"explanation": ""}, "explanation"),
            ({"explanation": "   "}, "explanation"),
            ({"explanation": None}, "explanation"),
            ({"explanation": "x" * (MAX_TEXT + 1)}, "explanation"),
            ({"operation_id": "nope"}, "operation_id"),
            ({"operation_id": str(uuid1())}, "operation_id"),
        )
        for extra, path in cases:
            with self.subTest(extra=extra):
                error = self.refused("MALFORMED_REQUEST", 400, **{**good, **extra})
                self.assertEqual(error.path, path)

    def test_an_unknown_deficiency_is_not_found(self):
        self.refused("NOT_FOUND", 404, deficiency_id=str(uuid4()), expected=str(uuid4()))

    def test_a_closed_or_cancelled_deficiency_cannot_be_re_graded(self):
        root = self.failed().body["check_id"]
        deficiency_id = DeficiencyEvidence.objects.get(check_record_id=root).deficiency_id
        correct_check(
            check_id=root,
            expected_tip_check_id=root,
            outcome="PASSED",
            **self.context(self.crew),
        )
        self.assertEqual(self.last_event(deficiency_id).event_type, "REPORTED_IN_ERROR")
        self.refused("DEFICIENCY_NOT_ACTIVE", 422, deficiency_id=deficiency_id)

    def test_a_re_grade_does_not_block_a_cancellation_or_its_reopening(self):
        first = self.failed(self.siren)
        deficiency_id = first.body["deficiency_ids"][0]
        self.regrade("LOW", deficiency_id=deficiency_id)
        root = first.body["check_id"]
        correct_check(
            check_id=root,
            expected_tip_check_id=root,
            outcome="PASSED",
            **self.context(self.crew),
        )
        self.assertEqual(self.last_event(deficiency_id).event_type, "REPORTED_IN_ERROR")
        invalidate_chain(check_id=root, explanation="Synthetic reason.", **self.context(self.admin))
        self.assertEqual(self.last_event(deficiency_id).event_type, "REOPENED")

    def test_a_re_grade_changes_no_link_assignee_deadline_or_vehicle_status(self):
        other = self.failed(self.siren, actor=self.supervisor).body["deficiency_ids"][0]
        add_link(
            deficiency_id=self.deficiency_id, related_deficiency_id=other, **self.context(self.crew)
        )
        before = self.snapshot()
        self.regrade("MEDIUM")
        self.regrade("CRITICAL")
        self.assertEqual(self.snapshot(), before)

    def test_a_failure_while_writing_rolls_everything_back_and_the_operation_id_is_reusable(self):
        operation_id = str(uuid4())
        expected = self.last_event().pk
        before = (self.counts(), self.snapshot())
        with (
            mock.patch(
                "cya.live_updates.publish.publish_vehicle_update", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.regrade("LOW", operation_id=operation_id, expected=expected)
        self.assertEqual((self.counts(), self.snapshot()), before)
        self.assertFalse(
            AuditEvent.objects.filter(action="DEFICIENCY_REGRADE", outcome="SUCCEEDED").exists()
        )
        self.assertEqual(
            DeficiencyProjection.objects.get(deficiency_id=self.deficiency_id).severity_version,
            self.level("HIGH"),
        )
        self.regrade("LOW", operation_id=operation_id, expected=expected)
        self.assertEqual(self.last_event().event_type, "SEVERITY_CHANGED")


class SeverityHistoryTests(RegradeCase):
    def test_the_history_is_replayed_from_the_events_and_the_projection_is_its_cache(self):
        self.regrade("LOW")
        self.regrade("CRITICAL")
        view = service.severity_history(str(self.org.pk), self.deficiency_id)
        self.assertEqual(view["severity"]["code"], "CRITICAL")
        self.assertEqual(
            [g["severity"]["code"] for g in view["history"]], ["HIGH", "LOW", "CRITICAL"]
        )
        self.assertEqual(
            [g["previous"] and g["previous"]["code"] for g in view["history"]],
            [None, "HIGH", "LOW"],
        )
        self.assertEqual(view["history"][0]["explanation"], None)
        self.assertEqual(view["history"][2]["explanation"], "Synthetic reason for the new grade.")
        self.assertEqual(
            (view["deficiency_status"], view["last_event_id"]), ("OPEN", self.last_event().pk)
        )
        projection = DeficiencyProjection.objects.get(deficiency_id=self.deficiency_id)
        self.assertEqual(projection.severity_version.code, view["severity"]["code"])
        self.assertEqual(projection.severity_version.revision, view["severity"]["revision"])

    def test_a_deficiency_never_re_graded_reads_as_its_reported_level(self):
        view = service.severity_history(str(self.org.pk), self.deficiency_id)
        self.assertEqual([g["severity"]["code"] for g in view["history"]], ["HIGH"])
        self.assertEqual(view["severity"]["label"], "High")

    def test_an_unknown_deficiency_and_an_oversized_history_are_refused(self):
        with self.assertRaises(DeficiencyError) as caught:
            service.severity_history(str(self.org.pk), str(uuid4()))
        self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))
        self.regrade("LOW")
        with (
            mock.patch.object(service, "MAX_REGRADES", 0),
            self.assertRaises(DeficiencyError) as caught,
        ):
            service.severity_history(str(self.org.pk), self.deficiency_id)
        self.assertEqual(
            (caught.exception.code, caught.exception.status), ("CONFIGURATION_UNAVAILABLE", 503)
        )

    def test_the_match_suggestions_show_the_current_level(self):
        self.regrade("LOW")
        rows = service.suggest_related(str(self.org.pk), str(self.vehicle.pk))
        self.assertEqual(
            [r["severity"] for r in rows if r["deficiency_id"] == self.deficiency_id], ["LOW"]
        )


class RegradeGuardTests(RegradeCase):
    """The database refuses what the command would not write (migration 0005)."""

    def setUp(self):
        super().setUp()
        self.last = self.last_event()

    def receipt(self, command_type="DEFICIENCY_REGRADE", *, actor=None):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=actor or self.supervisor,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def event(self, command_type="DEFICIENCY_REGRADE", **overrides):
        receipt = self.receipt(command_type)
        fields = {
            "organization_id": self.org.pk,
            "deficiency_id": self.deficiency_id,
            "sequence": self.last.sequence + 1,
            "predecessor_event": self.last,
            "event_type": "SEVERITY_CHANGED",
            "from_status": "OPEN",
            "to_status": "OPEN",
            "explanation": "Synthetic reason.",
            "previous_severity_version": self.level("HIGH"),
            "severity_version": self.level("LOW"),
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.supervisor,
            "actor_roles_at_time": ["SUPERVISOR"],
            **overrides,
        }
        return DeficiencyEvent.objects.create(**fields)

    def refused(self, *args, **kwargs):
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.event(*args, **kwargs)

    def test_a_valid_event_is_accepted_so_the_refusals_below_are_meaningful(self):
        with transaction.atomic():
            self.event()

    def test_the_command_receipt_and_the_levels_must_fit(self):
        self.refused("CHECK_CORRECTION")
        self.refused("DEFICIENCY_LINK_ADD")
        # the level left must be the one in force, and the new one must differ and be active
        self.refused(previous_severity_version=self.level("MEDIUM"))
        self.refused(severity_version=self.level("HIGH"))
        self.refused(
            severity_version=self.level("HIGH"), previous_severity_version=self.level("HIGH")
        )
        retired = self.retire("LOW")
        self.refused(severity_version=retired)
        self.refused(severity_version=self.level("LOW", revision=1))  # superseded by the retirement

    def test_the_previous_level_follows_the_newest_re_grade_not_the_header(self):
        self.regrade("LOW")
        self.last = self.last_event()
        self.refused(
            previous_severity_version=self.level("HIGH"), severity_version=self.level("MEDIUM")
        )
        with transaction.atomic():
            self.event(
                previous_severity_version=self.level("LOW"), severity_version=self.level("MEDIUM")
            )

    def test_the_shape_is_closed(self):
        self.refused(explanation=None)
        self.refused(correction_ref=str(uuid4()))
        self.refused(assignee=self.supervisor)
        self.refused(previous_severity_version=None)
        self.refused(severity_version=None)
        self.refused(to_status="IN_PROGRESS")
        self.refused(to_status=None)  # an UNKNOWN comparison would pass the CHECK
        self.refused(from_status="CLOSED_VERIFIED", to_status="CLOSED_VERIFIED")
        self.refused(from_status="REPORTED_IN_ERROR", to_status="REPORTED_IN_ERROR")

    def test_no_other_event_type_may_carry_a_level(self):
        self.refused(
            "CHECK_CORRECTION",
            event_type="CORRECTION_NOTICE",
            correction_ref=str(uuid4()),
        )

    def test_a_forked_or_skipped_chain_is_still_refused(self):
        self.refused(sequence=self.last.sequence + 2)
        self.refused(
            predecessor_event=DeficiencyEvent.objects.filter(sequence=1).first(), sequence=1
        )


@override_settings(**real_collaborator_settings())
class RegradeApiTests(RegradeCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.crew_client, self.crew_user = self.harness.sign_in("crew-http")
        self.sup_client, self.sup_user = self.harness.sign_in("supervisor-http")
        self.grant(self.sup_user, "SUPERVISOR")
        self.url = f"/api/v1/deficiencies/{self.deficiency_id}/severity-changes"
        self.read_url = f"/api/v1/deficiencies/{self.deficiency_id}/severity"

    def post(self, client, body=None, url=None):
        client.get("/")
        token = client.cookies["csrftoken"].value
        body = (
            {
                "operation_id": str(uuid4()),
                "severity": "LOW",
                "explanation": "Synthetic reason.",
                "expected_event_id": self.last_event().pk,
            }
            if body is None
            else body
        )
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return client.post(
            url or self.url, data=data, content_type="application/json", HTTP_X_CSRFTOKEN=token
        )

    def rejected(self, response, status, code, paths=()):
        self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.assertEqual(
            [(e["path"], e["code"]) for e in response.json()["field_errors"]],
            [tuple(p) for p in paths],
        )

    def test_a_supervisor_re_grades_and_the_reader_shows_the_current_level_and_history(self):
        response = self.post(self.sup_client)
        payload = response.json()
        self.assertEqual((response.status_code, payload["status"]), (201, "COMMITTED"))
        self.assertEqual(
            {k: payload["result"][k] for k in ("deficiency_id", "previous_severity", "severity")},
            {"deficiency_id": self.deficiency_id, "previous_severity": "HIGH", "severity": "LOW"},
        )
        self.assertTrue(payload["result"]["server_recorded_at"].endswith("Z"))
        view = self.crew_client.get(self.read_url).json()  # any member of the organization reads
        self.assertEqual((view["status"], view["severity"]["code"]), ("OK", "LOW"))
        self.assertEqual(view["last_event_id"], payload["result"]["event_id"])
        self.assertEqual([g["severity"]["code"] for g in view["history"]], ["HIGH", "LOW"])
        self.assertTrue(view["history"][1]["recorded_at"].endswith("Z"))

    def test_a_replay_is_acknowledged_again_without_a_second_event(self):
        body = {
            "operation_id": str(uuid4()),
            "severity": "LOW",
            "explanation": "Synthetic reason.",
            "expected_event_id": self.last_event().pk,
        }
        first = self.post(self.sup_client, body).json()
        events = DeficiencyEvent.objects.count()
        self.assertEqual(self.post(self.sup_client, body).json(), first)
        self.assertEqual(DeficiencyEvent.objects.count(), events)
        body["severity"] = "MEDIUM"
        self.rejected(
            self.post(self.sup_client, body), 409, "OPERATION_REUSED", [("operation_id", "REUSED")]
        )

    def test_a_crew_member_is_forbidden_and_signed_out_visitors_are_not_authenticated(self):
        events = DeficiencyEvent.objects.count()
        self.rejected(self.post(self.crew_client), 403, "FORBIDDEN")
        self.assertEqual(DeficiencyEvent.objects.count(), events)
        anonymous = self.harness.sign_in.__self__  # keep the harness alive; use a fresh client
        from django.test import Client

        fresh = Client()
        self.assertEqual(fresh.get(self.read_url).status_code, 401)
        self.assertEqual(
            fresh.post(self.url, "{}", content_type="application/json").status_code, 401
        )
        self.assertIsNotNone(anonymous)

    def test_refusals_carry_the_stable_codes(self):
        stale = {
            "operation_id": str(uuid4()),
            "severity": "LOW",
            "explanation": "Synthetic reason.",
            "expected_event_id": str(uuid4()),
        }
        self.rejected(
            self.post(self.sup_client, stale),
            409,
            "DEFICIENCY_CHANGED",
            [("expected_event_id", "DEFICIENCY_CHANGED")],
        )
        current = {**stale, "operation_id": str(uuid4()), "expected_event_id": self.last_event().pk}
        self.rejected(
            self.post(self.sup_client, {**current, "severity": "NOPE"}),
            422,
            "INVALID_SEVERITY",
            [("severity", "INVALID_SEVERITY")],
        )
        self.rejected(
            self.post(self.sup_client, {**current, "severity": "HIGH"}),
            422,
            "SEVERITY_UNCHANGED",
            [("severity", "SEVERITY_UNCHANGED")],
        )
        missing = f"/api/v1/deficiencies/{uuid4()}/severity-changes"
        self.rejected(self.post(self.sup_client, current, missing), 404, "NOT_FOUND")
        self.assertEqual(self.last_event().event_type, "REPORTED")

    def test_malformed_bodies_are_rejected_before_any_write(self):
        good = {
            "operation_id": str(uuid4()),
            "severity": "LOW",
            "explanation": "Synthetic reason.",
            "expected_event_id": self.last_event().pk,
        }
        events = DeficiencyEvent.objects.count()
        self.rejected(self.post(self.sup_client, "not json"), 400, "MALFORMED_REQUEST")
        self.rejected(self.post(self.sup_client, "[]"), 400, "MALFORMED_REQUEST")
        missing = {k: v for k, v in good.items() if k != "explanation"}
        self.rejected(
            self.post(self.sup_client, missing),
            400,
            "MALFORMED_REQUEST",
            [("explanation", "REQUIRED")],
        )
        self.rejected(
            self.post(self.sup_client, {**good, "extra": 1}),
            400,
            "MALFORMED_REQUEST",
            [("$", "UNKNOWN_FIELD")],
        )
        self.rejected(
            self.post(self.sup_client, {**good, "explanation": " "}),
            400,
            "MALFORMED_REQUEST",
            [("explanation", "MALFORMED_REQUEST")],
        )
        bad_id = "/api/v1/deficiencies/not-an-id/severity-changes"
        self.rejected(
            self.post(self.sup_client, good, bad_id),
            400,
            "MALFORMED_REQUEST",
            [("deficiency_id", "MALFORMED_REQUEST")],
        )
        self.assertEqual(DeficiencyEvent.objects.count(), events)

    def test_the_reader_refuses_unknown_deficiencies_and_query_strings(self):
        self.assertEqual(
            self.crew_client.get(f"/api/v1/deficiencies/{uuid4()}/severity").status_code, 404
        )
        self.assertEqual(self.crew_client.get(self.read_url + "?x=1").status_code, 400)
        self.assertEqual(
            self.crew_client.get("/api/v1/deficiencies/not-an-id/severity").status_code, 400
        )
        self.assertEqual(self.post(self.crew_client, {}, self.read_url).status_code, 405)
