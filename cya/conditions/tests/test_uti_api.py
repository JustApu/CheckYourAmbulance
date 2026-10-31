"""HTTP error contract for Unable-to-Inspect determination commands."""

import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import RequestFactory, SimpleTestCase

from cya.conditions import api
from cya.conditions.uti import UnableCompletionError


class UnableApiErrorTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(organization_id=uuid4(), pk=uuid4())

    def post(self, view, path, body):
        request = self.factory.post(path, data=json.dumps(body), content_type="application/json")
        request.cya_user = self.user
        return view(request, str(uuid4()))

    def test_each_command_error_has_a_distinct_contract_status(self):
        cases = {
            "INVALID_REQUEST": 400,
            "NOT_FOUND": 404,
            "INELIGIBLE_CHECK": 422,
            "INELIGIBLE_OBLIGATION": 422,
            "SELF_COMPLETION_NOT_ALLOWED": 422,
            "OBLIGATION_ALREADY_DETERMINED": 409,
            "OBLIGATION_ALREADY_SATISFIED": 409,
            "NOT_CURRENT_DETERMINATION": 409,
            "ITEM_LOCK_UNAVAILABLE": 503,
            "CONFIGURATION_UNAVAILABLE": 503,
        }
        body = {"operation_id": str(uuid4()), "check_id": str(uuid4()), "explanation": "Synthetic"}
        for code, expected_status in cases.items():
            with self.subTest(code=code):
                with patch.object(
                    api,
                    "complete_unable_obligation",
                    side_effect=UnableCompletionError(code, "check_id"),
                ) as command:
                    response = self.post(
                        api.complete_unable, "/api/v1/obligations/x/unable-completions", body
                    )
                payload = json.loads(response.content)
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(payload["status"], "REJECTED")
                self.assertEqual(payload["code"], code)
                self.assertEqual(payload["field_errors"], [{"path": "check_id", "code": "INVALID"}])
                self.assertIn("correlation_id", payload)
                self.assertNotIn(body["explanation"], response.content.decode())
                command.assert_called_once()

    def test_invalidation_uses_the_same_mapping_and_unmapped_errors_fail_closed(self):
        body = {"operation_id": str(uuid4()), "explanation": "Synthetic"}
        with patch.object(
            api, "invalidate_supervisor_completion", side_effect=UnableCompletionError("NOT_FOUND")
        ):
            response = self.post(
                api.invalidate_completion, "/api/v1/obligation-determinations/x/invalidations", body
            )
        self.assertEqual(
            (response.status_code, json.loads(response.content)["code"]), (404, "NOT_FOUND")
        )

        with patch.object(
            api,
            "invalidate_supervisor_completion",
            side_effect=UnableCompletionError("UNKNOWN_INTERNAL"),
        ):
            response = self.post(
                api.invalidate_completion, "/api/v1/obligation-determinations/x/invalidations", body
            )
        self.assertEqual(
            (response.status_code, json.loads(response.content)["code"]),
            (503, "CONFIGURATION_UNAVAILABLE"),
        )

    def test_malformed_request_never_reaches_the_command(self):
        body = {
            "operation_id": str(uuid4()),
            "check_id": str(uuid4()),
            "explanation": "Synthetic",
            "actor_user_id": "untrusted",
        }
        with patch.object(api, "complete_unable_obligation") as command:
            response = self.post(
                api.complete_unable, "/api/v1/obligations/x/unable-completions", body
            )
        self.assertEqual(
            (response.status_code, json.loads(response.content)["code"]), (400, "INVALID_REQUEST")
        )
        command.assert_not_called()

    def test_anonymous_malformed_requests_require_authentication_first(self):
        for view, path in (
            (api.complete_unable, "/api/v1/obligations/x/unable-completions"),
            (api.invalidate_completion, "/api/v1/obligation-determinations/x/invalidations"),
        ):
            with self.subTest(path=path):
                request = self.factory.post(path, data="not json", content_type="application/json")
                request.cya_user = None
                with patch.object(api, "_body") as parse:
                    response = view(request, str(uuid4()))
                self.assertEqual(
                    (response.status_code, json.loads(response.content)["code"]),
                    (401, "AUTHENTICATION_REQUIRED"),
                )
                parse.assert_not_called()

    def test_configuration_refusals_are_logged_without_request_text(self):
        body = {"operation_id": str(uuid4()), "check_id": str(uuid4()), "explanation": "Synthetic"}
        for code in ("ITEM_LOCK_UNAVAILABLE", "CONFIGURATION_UNAVAILABLE"):
            with self.subTest(code=code):
                with (
                    patch.object(
                        api, "complete_unable_obligation", side_effect=UnableCompletionError(code)
                    ),
                    patch.object(api.logger, "error") as log,
                ):
                    response = self.post(
                        api.complete_unable, "/api/v1/obligations/x/unable-completions", body
                    )
                self.assertEqual(response.status_code, 503)
                log.assert_called_once_with("determination not confirmed: %s", code)
