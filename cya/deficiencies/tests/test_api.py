"""Deficiency HTTP endpoints, and Failed checks over POST /api/v1/checks (issue 80 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/80))."""

import json
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.test import override_settings
from django.utils import timezone

from cya.checks.models import CheckRecord
from cya.core.models import CommandReceipt
from cya.deficiencies.closure import close_administrator, reopen_administrator
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyEvent,
    DeficiencyLink,
    DeficiencyProjection,
    DeficiencySeverity,
)
from cya.deficiencies.tests.support import DeficiencyCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

URL = "/api/v1/deficiencies"


@override_settings(**real_collaborator_settings())
class DeficiencyApiTests(DeficiencyCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client, self.user = self.harness.sign_in("crew-http")
        self.client.get("/")  # obtains the CSRF cookie
        self.token = self.client.cookies["csrftoken"].value

    def body(self, **overrides):
        body = {
            "operation_id": str(uuid4()),
            "vehicle_id": str(self.vehicle.pk),
            "target": {"item_id": str(self.suction.pk)},
            **self.new_fields(),
        }
        body.update(overrides)
        return {key: value for key, value in body.items() if value is not ...}

    def post(self, body=None, url=URL, client=None, token=True, **extra):
        body = self.body() if body is None else body
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        headers = {"HTTP_X_CSRFTOKEN": self.token} if token else {}
        return (client or self.client).post(
            url, data=data, content_type=extra.pop("content_type", "application/json"), **headers
        )

    def rejected(self, response, status, code, paths=()):
        self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.assertEqual(response.json()["status"], "REJECTED")
        self.assertEqual(
            [(e["path"], e["code"]) for e in response.json()["field_errors"]],
            [tuple(p) for p in paths],
        )

    def test_a_report_commits_and_acknowledges(self):
        response = self.post()
        payload = response.json()
        self.assertEqual((response.status_code, payload["status"]), (201, "COMMITTED"))
        row = Deficiency.objects.get()
        self.assertEqual(payload["result"]["deficiency_id"], row.pk)
        self.assertEqual(payload["result"]["related_link_event_ids"], [])
        self.assertTrue(payload["result"]["server_recorded_at"].endswith("Z"))
        self.assertEqual(row.reporter_id, self.user.pk)

    def test_a_replay_returns_the_stored_acknowledgement(self):
        body = self.body()
        first = self.post(body).json()
        receipts = CommandReceipt.objects.count()
        self.assertEqual(self.post(body).json(), first)
        self.assertEqual(
            (Deficiency.objects.count(), CommandReceipt.objects.count()), (1, receipts)
        )
        response = self.post({**body, "severity": "LOW"})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "OPERATION_REUSED"))

    def test_unauthenticated_and_csrf_less_requests_are_refused(self):
        from django.test import Client

        anonymous = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post(client=anonymous, token=False).status_code, 403)
        self.assertEqual(self.post(token=False).json()["code"], "CSRF_REJECTED")
        self.assertEqual(Deficiency.objects.count(), 0)

    def test_malformed_bodies_use_the_contract_shape(self):
        self.rejected(self.post("{not json"), 400, "MALFORMED_REQUEST")
        self.rejected(self.post("[]"), 400, "MALFORMED_REQUEST")
        self.rejected(self.post(content_type="text/plain"), 400, "MALFORMED_REQUEST")
        self.rejected(
            self.post(self.body(summary=...)), 400, "MALFORMED_REQUEST", [("summary", "REQUIRED")]
        )
        self.rejected(
            self.post(self.body(extra=1)), 400, "MALFORMED_REQUEST", [("$", "UNKNOWN_FIELD")]
        )
        self.rejected(
            self.post(self.body(deadline="tomorrow")),
            400,
            "MALFORMED_REQUEST",
            [("deadline", "INVALID")],
        )
        self.rejected(
            self.post(self.body(target={"item_id": "x"})),
            400,
            "MALFORMED_REQUEST",
            [("target.item_id", "MALFORMED_REQUEST")],
        )
        self.assertEqual(Deficiency.objects.count(), 0)

    def test_rule_failures_use_the_contract_codes_and_safe_paths(self):
        self.rejected(
            self.post(self.body(severity="SEVERE")),
            422,
            "INVALID_SEVERITY",
            [("severity", "INVALID_SEVERITY")],
        )
        self.rejected(
            self.post(self.body(vehicle_id=str(uuid4()))),
            404,
            "NOT_FOUND",
            [("vehicle_id", "NOT_FOUND")],
        )
        self.rejected(
            self.post(self.body(target={"kit_id": str(uuid4())})),
            404,
            "NOT_FOUND",
            [("target.kit_id", "NOT_FOUND")],
        )
        self.rejected(
            self.post(self.body(session_id=str(uuid4()))),
            422,
            "NOT_YET_SUPPORTED",
            [("session_id", "NOT_YET_SUPPORTED")],
        )

    def test_a_crew_member_cannot_assign_over_http(self):
        response = self.post(self.body(assignee_id=str(self.supervisor.pk)))
        self.rejected(response, 403, "FORBIDDEN")
        self.assertEqual(Deficiency.objects.count(), 0)

    def test_assignment_api_serializes_administrator_history_and_as_of(self):
        deficiency_id = self.failed().body["deficiency_ids"][0]
        reported = DeficiencyEvent.objects.get(deficiency_id=deficiency_id, sequence=1)
        closed = close_administrator(
            deficiency_id=str(deficiency_id),
            expected_event_id=str(reported.pk),
            explanation="Administrative closure while the replacement is unavailable.",
            **self.context(self.admin),
        )
        close_event = DeficiencyEvent.objects.get(pk=closed.body["event_id"])
        url = f"{URL}/{deficiency_id}/assignment"

        before_close = self.client.get(
            url,
            {"as_of": (close_event.recorded_at - timedelta(microseconds=1)).isoformat()},
        )
        self.assertEqual(before_close.status_code, 200, before_close.content)
        self.assertEqual(before_close.json()["history"], [])

        through_close = self.client.get(url, {"as_of": close_event.recorded_at.isoformat()})
        self.assertEqual(through_close.status_code, 200, through_close.content)
        closed_payload = through_close.json()
        self.assertEqual(closed_payload["deficiency_status"], "CLOSED_ADMINISTRATOR")
        close_history = closed_payload["history"][-1]
        self.assertEqual(close_history["event_type"], "ADMINISTRATOR_CLOSED")
        self.assertIsNone(close_history["assignee_id"])
        self.assertIsNone(close_history["deadline"])

        reopened = reopen_administrator(
            deficiency_id=str(deficiency_id),
            expected_event_id=closed.body["event_id"],
            explanation="The replacement arrived; work can resume.",
            **self.context(self.admin),
        )
        reopen_event = DeficiencyEvent.objects.get(pk=reopened.body["event_id"])
        current = self.client.get(url)
        self.assertEqual(current.status_code, 200, current.content)
        self.assertEqual(current.json()["deficiency_status"], "OPEN")
        self.assertEqual(
            [event["event_type"] for event in current.json()["history"]],
            ["ADMINISTRATOR_CLOSED", "ADMINISTRATOR_REOPENED"],
        )
        through_reopen = self.client.get(url, {"as_of": reopen_event.recorded_at.isoformat()})
        self.assertEqual(through_reopen.status_code, 200, through_reopen.content)
        self.assertEqual(through_reopen.json()["deficiency_status"], "OPEN")
        self.assertEqual(
            [event["event_type"] for event in through_reopen.json()["history"]],
            ["ADMINISTRATOR_CLOSED", "ADMINISTRATOR_REOPENED"],
        )

    def test_supervisor_empty_scope_closure_route_is_explicit_and_replayable(self):
        self.grant(self.user, "SUPERVISOR")
        deficiency_id = self.failed().body["deficiency_ids"][0]
        newest = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        url = f"{URL}/{deficiency_id}/empty-scope-closures"
        body = {
            "operation_id": str(uuid4()),
            "expected_event_id": str(newest.pk),
            "explanation": "The checklist no longer contains this item.",
        }
        with mock.patch("cya.deficiencies.service.correction_scope", return_value=[]):
            response = self.post(body, url)
            self.assertEqual((response.status_code, response.json()["status"]), (201, "COMMITTED"))
            replay = self.post(body, url)
        self.assertEqual(replay.json(), response.json())
        event = DeficiencyEvent.objects.get(pk=response.json()["result"]["event_id"])
        self.assertEqual(
            (event.event_type, event.to_status), ("EMPTY_SCOPE_CLOSED", "CLOSED_UNVERIFIABLE")
        )
        self.assertEqual(event.actor_user_id, self.user.pk)

    def test_scope_expansion_and_added_verification_routes(self):
        from cya.checks.commands import record_check
        from cya.deficiencies.commands import record_correction, start_work

        self.grant(self.user, "SUPERVISOR")
        context = {
            "organization_id": str(self.org.pk),
            "actor_id": str(self.user.pk),
            "correlation_id": str(uuid4()),
            "request_attempt_id": str(uuid4()),
        }

        def newest(deficiency_id):
            return str(
                DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence").pk
            )

        deficiency_id = self.post().json()["result"]["deficiency_id"]  # independent report
        for command in (start_work, record_correction):
            command(
                deficiency_id=deficiency_id,
                expected_event_id=newest(deficiency_id),
                operation_id=str(uuid4()),
                **context,
            )
        body = {
            "operation_id": str(uuid4()),
            "expected_event_id": newest(deficiency_id),
            "explanation": "Also check the masks.",
            "items": [{"item_id": str(self.masks.pk)}],
        }
        url = f"{URL}/{deficiency_id}/scope-expansions"
        response = self.post(body, url)
        self.assertEqual((response.status_code, response.json()["status"]), (201, "COMMITTED"))
        result = response.json()["result"]
        self.assertEqual((result["status"], result["scope_revision"]), ("PENDING_VERIFICATION", 2))
        self.assertEqual(self.post(body, url).json(), response.json())  # replay
        for item, extra in ((self.suction, {}), (self.masks, {"quantity": 6})):
            record_check(
                vehicle_id=str(self.vehicle.pk),
                item_id=str(item.pk),
                checklist_version_id=self.version_id,
                outcome="PASSED",
                purpose="VERIFICATION",
                verification_for_deficiency_id=deficiency_id,
                operation_id=str(uuid4()),
                **extra,
                **context,
            )
        self.assertEqual(
            DeficiencyProjection.objects.get(deficiency_id=deficiency_id).status, "CLOSED_VERIFIED"
        )
        deadline = (timezone.now() + timedelta(days=5)).isoformat()
        added = self.post(
            {
                **body,
                "operation_id": str(uuid4()),
                "expected_event_id": newest(deficiency_id),
                "deadline": deadline,
            },
            f"{URL}/{deficiency_id}/added-verifications",
        )
        self.assertEqual(
            (added.status_code, added.json()["status"]), (201, "COMMITTED"), added.content
        )
        self.assertEqual(added.json()["result"]["status"], "OPEN")
        self.assertIn("deadline", added.json()["result"])
        missing = self.post(
            {**body, "operation_id": str(uuid4()), "expected_event_id": newest(deficiency_id)},
            f"{URL}/{deficiency_id}/added-verifications",
        )
        self.assertEqual(missing.status_code, 400)  # the deadline field is required

    def test_links_can_be_added_and_removed_over_http(self):
        first = self.post().json()["result"]["deficiency_id"]
        second = self.post().json()["result"]["deficiency_id"]
        add = f"{URL}/{first}/links"
        remove = f"{URL}/{first}/link-removals"
        body = {"operation_id": str(uuid4()), "related_deficiency_id": second}
        response = self.post(body, add)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            sorted(
                [response.json()["result"]["lower_deficiency_id"]]
                + [response.json()["result"]["upper_deficiency_id"]]
            ),
            sorted([first, second]),
        )
        self.assertEqual(self.post(body, add).json(), response.json())  # replay
        again = self.post({**body, "operation_id": str(uuid4())}, add)
        self.rejected(again, 409, "ALREADY_LINKED")
        removed = self.post({**body, "operation_id": str(uuid4())}, remove)
        self.assertEqual(removed.status_code, 201)
        self.rejected(self.post({**body, "operation_id": str(uuid4())}, remove), 409, "NOT_LINKED")
        self.rejected(
            self.post({**body, "operation_id": str(uuid4()), "related_deficiency_id": first}, add),
            422,
            "SELF_LINK",
        )
        self.rejected(
            self.post({"operation_id": str(uuid4())}, add),
            400,
            "MALFORMED_REQUEST",
            [("related_deficiency_id", "REQUIRED")],
        )
        self.assertEqual(DeficiencyLink.objects.count(), 2)

    def test_suggestions_are_a_bounded_read_only_listing(self):
        match = self.post().json()["result"]["deficiency_id"]
        before = (Deficiency.objects.count(), CommandReceipt.objects.count())
        response = self.client.get(
            f"{URL}/suggestions", {"vehicle_id": self.vehicle.pk, "item_id": self.suction.pk}
        )
        self.assertEqual(response.status_code, 200)
        rows = response.json()["suggestions"]
        self.assertEqual([r["deficiency_id"] for r in rows], [match])
        self.assertEqual(rows[0]["status"], "OPEN")
        self.assertEqual(
            self.client.get(f"{URL}/suggestions", {"vehicle_id": self.vehicles[1].pk}).json()[
                "suggestions"
            ],
            [],
        )
        for query in ({}, {"vehicle_id": "x"}, {"vehicle_id": self.vehicle.pk, "other": 1}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"{URL}/suggestions", query).status_code, 400)
        self.assertEqual(self.post({}, f"{URL}/suggestions").status_code, 405)
        self.assertEqual((Deficiency.objects.count(), CommandReceipt.objects.count()), before)

    def test_a_failed_check_over_http_creates_its_deficiency(self):
        body = {
            "operation_id": str(uuid4()),
            "vehicle_id": str(self.vehicle.pk),
            "item_id": str(self.suction.pk),
            "checklist_version_id": self.version_id,
            "outcome": "FAILED",
            "purpose": "ROUTINE",
            "deficiency": {"create": self.new_fields()},
        }
        response = self.post(body, "/api/v1/checks")
        self.assertEqual(response.status_code, 201, response.content)
        result = response.json()["result"]
        self.assertEqual(result["deficiency_ids"], [Deficiency.objects.get().pk])
        self.assertEqual(len(result["event_ids"]), 1)
        self.assertEqual(self.post(body, "/api/v1/checks").json(), response.json())
        self.assertEqual((CheckRecord.objects.count(), Deficiency.objects.count()), (1, 1))

    def test_a_failed_check_without_a_deficiency_is_rejected_over_http(self):
        body = {
            "operation_id": str(uuid4()),
            "vehicle_id": str(self.vehicle.pk),
            "item_id": str(self.suction.pk),
            "checklist_version_id": self.version_id,
            "outcome": "FAILED",
            "purpose": "ROUTINE",
        }
        self.rejected(
            self.post(body, "/api/v1/checks"),
            422,
            "DEFICIENCY_REQUIRED",
            [("deficiency", "DEFICIENCY_REQUIRED")],
        )
        self.assertEqual(CheckRecord.objects.count(), 0)

    def test_the_listing_gives_each_active_level_with_its_criteria_least_severe_first(self):
        self.client.get("/")
        response = self.client.get("/api/v1/deficiency-severities")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["severities"]
        self.assertEqual([r["code"] for r in rows], ["LOW", "MEDIUM", "HIGH", "CRITICAL"])
        self.assertEqual(set(rows[0]), {"code", "label", "description", "rank"})
        self.assertTrue(all(r["description"] for r in rows))

    def test_a_retired_level_is_not_offered_and_a_new_one_is(self):
        DeficiencySeverity.objects.create(
            organization=self.org,
            code="LOW",
            revision=2,
            label="Low",
            description="x",
            rank=1,
            active=False,
            effective_at=self.now - timedelta(minutes=1),
        )
        DeficiencySeverity.objects.create(
            organization=self.org,
            code="URGENT",
            revision=1,
            label="Urgent",
            description="y",
            rank=9,
            active=True,
            effective_at=self.now - timedelta(minutes=1),
        )
        codes = [
            r["code"] for r in self.client.get("/api/v1/deficiency-severities").json()["severities"]
        ]
        self.assertEqual(codes, ["MEDIUM", "HIGH", "CRITICAL", "URGENT"])

    def test_the_listing_is_read_only_and_takes_no_parameters(self):
        self.assertEqual(
            self.client.get("/api/v1/deficiency-severities", {"x": 1}).status_code, 400
        )
        self.assertEqual(self.post({}, "/api/v1/deficiency-severities").status_code, 405)
        from django.test import Client

        self.assertEqual(Client().get("/api/v1/deficiency-severities").status_code, 401)

    def test_correction_and_invalidation_report_their_deficiency_effects_over_http(self):
        failed = self.post(
            {
                "operation_id": str(uuid4()),
                "vehicle_id": str(self.vehicle.pk),
                "item_id": str(self.suction.pk),
                "checklist_version_id": self.version_id,
                "outcome": "FAILED",
                "purpose": "ROUTINE",
                "deficiency": {"create": self.new_fields()},
            },
            "/api/v1/checks",
        ).json()["result"]
        check_id, deficiency_id = failed["check_id"], failed["deficiency_ids"][0]
        corrected = self.post(
            {
                "operation_id": str(uuid4()),
                "expected_tip_check_id": check_id,
                "outcome": "PASSED",
            },
            f"/api/v1/checks/{check_id}/corrections",
        )
        self.assertEqual(corrected.status_code, 201, corrected.content)
        events = corrected.json()["result"]["deficiency_event_ids"]
        self.assertEqual(len(events), 1)
        self.assertEqual(DeficiencyProjection.objects.get().status, "REPORTED_IN_ERROR")

        admin_client, admin_user = self.harness.sign_in("admin-http")
        self.grant(admin_user, "ADMINISTRATOR")
        admin_client.get("/")
        token = admin_client.cookies["csrftoken"].value
        response = admin_client.post(
            f"/api/v1/checks/{check_id}/invalidations",
            data=json.dumps({"operation_id": str(uuid4()), "explanation": "Synthetic reason."}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["result"]["reopened_deficiency_ids"], [deficiency_id])
        self.assertEqual(DeficiencyProjection.objects.get().status, "OPEN")
