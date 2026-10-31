"""POST /api/v1/checks: the contract's status codes, error shape, CSRF and body bounds."""

import json
from uuid import uuid1, uuid4

from django.test import Client, override_settings

from cya.checks import api
from cya.checks.models import CheckEvent, CheckRecord
from cya.checks.tests.support import CheckCase
from cya.core.models import CommandReceipt
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

URL = "/api/v1/checks"
CORRECTION_URL = "/api/v1/checks/{}/corrections"


@override_settings(**real_collaborator_settings())
class ApiTests(CheckCase):
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
            "item_id": str(self.suction.pk),
            "checklist_version_id": self.version_id,
            "outcome": "PASSED",
            "purpose": "ROUTINE",
        }
        body.update(overrides)
        return {key: value for key, value in body.items() if value is not ...}

    def post(self, body=None, client=None, token=True, **extra):
        body = self.body() if body is None else body
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        headers = {"HTTP_X_CSRFTOKEN": self.token} if token else {}
        return (client or self.client).post(
            URL,
            data=data,
            content_type=extra.pop("content_type", "application/json"),
            **headers,
            **extra,
        )

    def rejected(self, response, status, code, paths=()):
        self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        payload = response.json()
        self.assertEqual(payload["status"], "REJECTED")
        self.assertEqual(
            [(e["path"], e["code"]) for e in payload["field_errors"]], [tuple(p) for p in paths]
        )
        self.assertEqual(CheckRecord.objects.count(), 0)

    def test_a_signed_in_user_records_a_check_and_the_acknowledgement_has_the_contract_shape(self):
        body = self.body(client_observed_at="2026-06-01T12:00:00Z")
        response = self.post(body)
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        row = CheckRecord.objects.get()
        self.assertEqual(
            (payload["status"], payload["operation_id"]), ("COMMITTED", body["operation_id"])
        )
        self.assertEqual(row.actor_user_id, self.user.pk)
        self.assertEqual(
            payload["result"],
            {
                "check_id": row.pk,
                "outcome": "PASSED",
                "server_recorded_at": row.recorded_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "obligation_links": [],
                "deficiency_ids": [],
                "event_ids": [],
            },
        )

    def test_author_correction_endpoint_returns_a_committed_chain_event(self):
        original = self.post(self.body()).json()["result"]["check_id"]
        body = {
            "operation_id": str(uuid4()),
            "expected_tip_check_id": original,
            "outcome": "PASSED",
            "explanation": "Synthetic correction note.",
        }
        response = self.client.post(
            CORRECTION_URL.format(original),
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["result"]["root_check_id"], original)
        self.assertEqual(CheckEvent.objects.get().event_type, "CORRECTION")

    def test_correction_rejects_untrusted_attribution_fields(self):
        original = self.post(self.body()).json()["result"]["check_id"]
        body = {
            "operation_id": str(uuid4()),
            "expected_tip_check_id": original,
            "outcome": "PASSED",
            "actor_user_id": str(self.user.pk),
        }
        response = self.client.post(
            CORRECTION_URL.format(original),
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(
            (response.status_code, response.json()["code"]), (400, "MALFORMED_REQUEST")
        )
        self.assertEqual(CheckRecord.objects.count(), 1)
        self.assertEqual(CheckEvent.objects.count(), 0)

    def test_a_check_in_a_session_over_http_and_the_session_refusals(self):
        from cya.inspections import commands as session_commands

        def session_call(function, **kwargs):
            return function(**kwargs, **self.context(self.user))

        opened = session_call(
            session_commands.open_or_join_session,
            vehicle_id=str(self.vehicle.pk),
            template_id=str(self.template.pk),
        )
        session_id = opened.body["session_id"]
        response = self.post(self.body(session_id=session_id))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(str(CheckRecord.objects.get().session_id), session_id)
        self.rejected_after(
            self.post(self.body(session_id=str(uuid4()))),
            404,
            "NOT_FOUND",
            [("session_id", "NOT_FOUND")],
        )
        session_call(session_commands.end_session, session_id=session_id)
        self.rejected_after(
            self.post(self.body(session_id=session_id)),
            409,
            "SESSION_NOT_OPEN",
            [("session_id", "SESSION_NOT_OPEN")],
        )

    def rejected_after(self, response, status, code, paths):
        """Like rejected, for a case where earlier checks already exist."""
        before = CheckRecord.objects.count()
        payload = response.json()
        self.assertEqual((response.status_code, payload["code"]), (status, code))
        self.assertEqual(
            [(e["path"], e["code"]) for e in payload["field_errors"]], [tuple(p) for p in paths]
        )
        self.assertEqual(CheckRecord.objects.count(), before)

    def test_a_replay_returns_the_stored_acknowledgement_with_the_original_correlation_id(self):
        body = self.body()
        first = self.post(body).json()
        receipts = CommandReceipt.objects.count()
        again = self.post(body)
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json(), first)
        self.assertEqual(CheckRecord.objects.count(), 1)
        self.assertEqual(CommandReceipt.objects.count(), receipts)

    def test_reusing_an_operation_id_for_a_different_request_is_a_conflict(self):
        body = self.body()
        self.post(body)
        response = self.post({**body, "item_id": str(self.special.pk)})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "OPERATION_REUSED"))
        self.assertEqual(CheckRecord.objects.count(), 1)

    def test_a_quantity_check_without_an_outcome_gets_the_derived_one(self):
        body = self.body(item_id=str(self.masks.pk), outcome=..., quantity=6)
        response = self.post(body)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["result"]["outcome"], "PASSED")
        self.assertEqual(CheckRecord.objects.get().outcome, "PASSED")
        self.assertEqual(self.post(body).json(), response.json())  # replay: same derived outcome

    def test_an_outcome_is_still_required_when_a_count_cannot_decide_it(self):
        response = self.post(self.body(outcome=...))
        self.rejected(response, 422, "INVALID_OUTCOME", [("outcome", "INVALID_OUTCOME")])

    def test_a_verification_purpose_without_its_target_is_refused_with_the_field(self):
        response = self.post(self.body(purpose="VERIFICATION"))
        self.rejected(
            response,
            422,
            "VERIFICATION_TARGET_REQUIRED",
            [("verification_for_deficiency_id", "VERIFICATION_TARGET_REQUIRED")],
        )

    def test_a_failed_check_must_name_its_deficiency(self):
        response = self.post(self.body(outcome="FAILED"))
        self.rejected(response, 422, "DEFICIENCY_REQUIRED", [("deficiency", "DEFICIENCY_REQUIRED")])

    def test_rule_failures_use_the_contract_codes_and_safe_paths(self):
        self.rejected(
            self.post(self.body(item_id=str(self.masks.pk))),
            422,
            "INVALID_OBSERVATION",
            [("quantity", "INVALID_OBSERVATION")],
        )
        self.rejected(
            self.post(self.body(outcome="NOT_APPLICABLE")),
            422,
            "INVALID_OUTCOME",
            [("outcome", "INVALID_OUTCOME")],
        )
        self.rejected(
            self.post(self.body(vehicle_id=str(uuid4()))),
            404,
            "NOT_FOUND",
            [("vehicle_id", "NOT_FOUND")],
        )
        self.rejected(
            self.post(self.body(client_observed_at="yesterday")),
            400,
            "MALFORMED_REQUEST",
            [("client_observed_at", "INVALID")],
        )

    def test_malformed_requests_write_nothing(self):
        cases = {
            "not json": ("{", "application/json", []),
            "not an object": ("[1]", "application/json", []),
            "wrong content type": (json.dumps(self.body()), "text/plain", []),
            "missing field": (
                self.body(item_id=...),
                "application/json",
                [("item_id", "REQUIRED")],
            ),
            "unknown field": (
                self.body(actor_user_id=str(uuid4())),
                "application/json",
                [("$", "UNKNOWN_FIELD")],
            ),
            "operation id version 1": (
                self.body(operation_id=str(uuid1())),
                "application/json",
                [("operation_id", "MALFORMED_REQUEST")],
            ),
            "bad uuid": (
                self.body(item_id="x"),
                "application/json",
                [("item_id", "MALFORMED_REQUEST")],
            ),
        }
        for label, (body, content_type, paths) in cases.items():
            with self.subTest(label):
                response = self.post(body, content_type=content_type)
                self.rejected(response, 400, "MALFORMED_REQUEST", paths)

    def test_a_client_cannot_name_the_actor_or_roles(self):
        for field in ("actor_user_id", "actor_id", "roles", "organization_id"):
            with self.subTest(field):
                response = self.post(self.body(**{field: str(uuid4())}))
                self.rejected(response, 400, "MALFORMED_REQUEST", [("$", "UNKNOWN_FIELD")])

    def test_oversize_bodies_are_refused_before_parsing(self):
        huge = self.body(explanation="x" * (api.MAX_BODY_BYTES + 1))
        self.rejected(self.post(huge), 413, "REQUEST_TOO_LARGE")

    def test_the_streamed_read_is_bounded_even_when_the_declared_length_lies(self):
        class Lying:
            META = {"CONTENT_LENGTH": "10"}

            def read(self, size):
                return b"x" * size

        with self.assertRaises(api.Rejected) as caught:
            api._read_body(Lying())
        self.assertEqual(
            (caught.exception.status, caught.exception.code), (413, "REQUEST_TOO_LARGE")
        )
        Lying.META = {"CONTENT_LENGTH": "not a number"}
        with self.assertRaises(api.Rejected) as caught:
            api._read_body(Lying())
        self.assertEqual(caught.exception.status, 400)

    def test_deeply_nested_json_is_a_malformed_request_not_a_crash(self):
        nested = "[" * 50_000 + "]" * 50_000
        self.rejected(self.post(nested), 400, "MALFORMED_REQUEST")

    def test_only_post_is_accepted(self):
        for method in ("get", "put", "delete"):
            with self.subTest(method):
                response = getattr(self.client, method)(URL, HTTP_X_CSRFTOKEN=self.token)
                self.assertEqual(response.status_code, 405)

    def test_a_missing_csrf_token_is_a_json_refusal_and_html_pages_keep_the_default(self):
        self.rejected(self.post(token=False), 403, "CSRF_REJECTED")
        html = self.client.post("/administration/roles/", {"operation_id": str(uuid4())})
        self.assertEqual(html.status_code, 403)
        self.assertNotEqual(html["Content-Type"], "application/json")

    def test_an_anonymous_request_is_refused(self):
        anonymous = Client(enforce_csrf_checks=False)
        self.rejected(self.post(client=anonymous, token=False), 401, "AUTHENTICATION_REQUIRED")

    def test_a_user_disabled_mid_session_is_refused_on_the_next_request(self):
        self.user.active = False
        self.user.save(update_fields=["active"])
        self.rejected(self.post(), 401, "AUTHENTICATION_REQUIRED")

    def test_a_conflict_has_the_dedicated_shape_and_proceeding_with_a_new_operation_commits(self):
        first = self.post(self.body())
        self.assertEqual(first.status_code, 201)
        other_client, other_user = self.harness.sign_in("crew-http-two")
        other_client.get("/")
        other_token = other_client.cookies["csrftoken"].value

        def other_post(body):
            return other_client.post(
                URL,
                data=json.dumps(body),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=other_token,
            )

        attempt = self.body()
        warned = other_post(attempt)
        payload = warned.json()
        self.assertEqual(warned.status_code, 409)
        self.assertEqual(
            payload,
            {
                "status": "CONFLICT",
                "code": "CHECK_CONFLICT",
                "operation_id": attempt["operation_id"],
                "correlation_id": payload["correlation_id"],
                "newest_check": {
                    "check_id": first.json()["result"]["check_id"],
                    "actor": {"user_id": self.user.pk, "display_name": self.user.display_name},
                    "server_recorded_at": first.json()["result"]["server_recorded_at"],
                    "outcome": "PASSED",
                },
                "obligation_id": None,
            },
        )
        self.assertEqual(CheckRecord.objects.count(), 1)
        self.assertEqual(
            CommandReceipt.objects.filter(operation_id=attempt["operation_id"]).count(), 0
        )
        proceed = self.body(acknowledges_check_id=payload["newest_check"]["check_id"])
        committed = other_post(proceed)
        self.assertEqual(committed.status_code, 201)
        self.assertEqual(CheckRecord.objects.count(), 2)
        self.assertEqual(
            CheckRecord.objects.get(pk=committed.json()["result"]["check_id"]).actor_user_id,
            other_user.pk,
        )

    def test_an_administrator_invalidates_a_chain_over_http_and_others_are_refused(self):
        check = self.post(self.body()).json()["result"]["check_id"]
        admin_client, admin_user = self.harness.sign_in("admin-http")
        self.grant(admin_user, "ADMINISTRATOR")
        admin_client.get("/")
        token = admin_client.cookies["csrftoken"].value

        def invalidate(client, token_value, body, check_id=check):
            return client.post(
                f"/api/v1/checks/{check_id}/invalidations",
                data=json.dumps(body),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token_value,
            )

        body = {"operation_id": str(uuid4()), "explanation": "Synthetic reason."}
        denied = invalidate(
            self.client, self.token, body
        )  # the crew author is not an administrator
        self.assertEqual(
            (denied.status_code, denied.json()["code"]), (403, "ADMINISTRATOR_REQUIRED")
        )
        blank = invalidate(admin_client, token, {**body, "explanation": " "})
        self.assertEqual((blank.status_code, blank.json()["code"]), (422, "EXPLANATION_REQUIRED"))
        self.assertEqual(
            blank.json()["field_errors"], [{"path": "explanation", "code": "EXPLANATION_REQUIRED"}]
        )
        extra = invalidate(admin_client, token, {**body, "actor_id": str(uuid4())})
        self.assertEqual((extra.status_code, extra.json()["code"]), (400, "MALFORMED_REQUEST"))
        self.assertEqual(CheckEvent.objects.count(), 0)

        done = invalidate(admin_client, token, body)
        self.assertEqual(done.status_code, 201)
        result = done.json()["result"]
        self.assertEqual(result["root_check_id"], check)
        self.assertEqual(
            (
                result["affected_obligation_ids"],
                result["voided_determination_ids"],
                result["reopened_deficiency_ids"],
            ),
            ([], [], []),
        )
        event = CheckEvent.objects.get()
        self.assertEqual(result["invalidation_event_id"], event.pk)
        replay = invalidate(admin_client, token, body)
        self.assertEqual((replay.status_code, replay.json()["result"]), (201, result))
        again = invalidate(admin_client, token, {**body, "operation_id": str(uuid4())})
        self.assertEqual((again.status_code, again.json()["code"]), (409, "CHAIN_INVALIDATED"))
        missing = invalidate(
            admin_client, token, {**body, "operation_id": str(uuid4())}, str(uuid4())
        )
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "NOT_FOUND"))
        self.assertEqual(CheckEvent.objects.count(), 1)

    def test_an_administrator_naming_their_own_check_gets_a_distinct_refusal(self):
        admin_client, admin_user = self.harness.sign_in("admin-own")
        self.grant(admin_user, "ADMINISTRATOR")
        admin_client.get("/")
        token = admin_client.cookies["csrftoken"].value
        own = admin_client.post(
            URL,
            data=json.dumps(self.body()),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        ).json()["result"]["check_id"]
        response = admin_client.post(
            f"/api/v1/checks/{own}/invalidations",
            data=json.dumps({"operation_id": str(uuid4()), "explanation": "Synthetic reason."}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(
            (response.status_code, response.json()["code"]), (403, "SELF_INVALIDATION_FORBIDDEN")
        )
        self.assertEqual(CheckEvent.objects.count(), 0)
