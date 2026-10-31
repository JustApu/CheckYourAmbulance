"""POST /api/v1/odometer-entries: status codes, error shape, CSRF and body bounds."""

import json
from uuid import uuid4

from django.test import override_settings

from cya.checks.tests.support import CheckCase
from cya.core.models import CommandReceipt
from cya.odometer.models import OdometerEntry, OdometerObligationLink
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

URL = "/api/v1/odometer-entries"


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
            "value": "40210.5",
            "unit": "mi",
        }
        body.update(overrides)
        return {key: value for key, value in body.items() if value is not ...}

    def post(self, body=None, token=True, **extra):
        body = self.body() if body is None else body
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        headers = {"HTTP_X_CSRFTOKEN": self.token} if token else {}
        return self.client.post(
            URL,
            data=data,
            content_type=extra.pop("content_type", "application/json"),
            **headers,
            **extra,
        )

    def rejected(self, response, status, code, paths=()):
        payload = response.json()
        self.assertEqual((response.status_code, payload["code"]), (status, code))
        self.assertEqual(payload["status"], "REJECTED")
        self.assertEqual(
            [(e["path"], e["code"]) for e in payload["field_errors"]], [tuple(p) for p in paths]
        )
        self.assertEqual(OdometerEntry.objects.count(), 0)

    def test_a_signed_in_user_records_a_reading_and_the_acknowledgement_has_the_contract_shape(
        self,
    ):
        body = self.body(engine_hours="812.5")
        response = self.post(body)
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        row = OdometerEntry.objects.get()
        self.assertEqual(row.actor_user_id, self.user.pk)
        self.assertEqual(
            (payload["status"], payload["operation_id"]), ("COMMITTED", body["operation_id"])
        )
        self.assertEqual(
            payload["result"],
            {
                "entry_id": row.pk,
                "engine_hours_entry_id": payload["result"]["engine_hours_entry_id"],
                "server_recorded_at": row.recorded_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "anomaly_confirmed": False,
                "obligation_links": [],
            },
        )
        self.assertEqual(OdometerObligationLink.objects.count(), 0)

    def test_a_replay_returns_the_original_and_a_reused_operation_id_is_refused(self):
        body = self.body()
        first = self.post(body)
        again = self.post(body)
        self.assertEqual((again.status_code, again.json()["result"]), (201, first.json()["result"]))
        reused = self.post(self.body(operation_id=body["operation_id"], value="1"))
        self.assertEqual((reused.status_code, reused.json()["code"]), (409, "OPERATION_REUSED"))
        self.assertEqual(OdometerEntry.objects.count(), 1)
        self.assertEqual(CommandReceipt.objects.filter(command_type="ODOMETER_RECORD").count(), 1)

    def test_an_anomalous_reading_is_refused_with_its_path_until_confirmed(self):
        self.assertEqual(self.post(self.body(value=5000)).status_code, 201)
        refusal = self.post(self.body(value=4000))
        self.assertEqual(
            (refusal.status_code, refusal.json()["code"], refusal.json()["field_errors"]),
            (
                409,
                "ANOMALY_CONFIRMATION_REQUIRED",
                [{"path": "value", "code": "ANOMALY_CONFIRMATION_REQUIRED"}],
            ),
        )
        confirmed = self.post(
            self.body(value=4000, confirm_anomaly=True, explanation="Cluster replaced.")
        )
        self.assertEqual(
            (confirmed.status_code, confirmed.json()["result"]["anomaly_confirmed"]), (201, True)
        )

    def test_malformed_requests_are_refused_with_schema_paths_only(self):
        self.rejected(self.post("{not json"), 400, "MALFORMED_REQUEST")
        self.rejected(self.post("[1]"), 400, "MALFORMED_REQUEST")
        self.rejected(self.post(content_type="text/plain"), 400, "MALFORMED_REQUEST")
        self.rejected(
            self.post(self.body(value=...)), 400, "MALFORMED_REQUEST", [("value", "REQUIRED")]
        )
        self.rejected(
            self.post(self.body(actor_id=str(uuid4()))),
            400,
            "MALFORMED_REQUEST",
            [("$", "UNKNOWN_FIELD")],
        )
        self.rejected(
            self.post(self.body(value="1e3")),
            422,
            "INVALID_OBSERVATION",
            [("value", "INVALID_OBSERVATION")],
        )
        self.rejected(
            self.post(self.body(vehicle_id=str(uuid4()))),
            404,
            "NOT_FOUND",
            [("vehicle_id", "NOT_FOUND")],
        )
        self.rejected(
            self.post(self.body(session_id=str(uuid4()))),
            422,
            "NOT_YET_SUPPORTED",
            [("session_id", "NOT_YET_SUPPORTED")],
        )

    def test_an_oversized_body_is_refused(self):
        self.rejected(
            self.post(self.body(explanation="x" * (300 * 1024))), 413, "REQUEST_TOO_LARGE"
        )

    def test_csrf_authentication_and_method_are_enforced(self):
        refused = self.post(token=False)
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "CSRF_REJECTED"))
        self.assertEqual(self.client.get(URL).status_code, 405)
        anonymous = self.client_class().post(URL, data="{}", content_type="application/json")
        self.assertIn(anonymous.status_code, (401, 403))
        self.assertEqual(OdometerEntry.objects.count(), 0)

    def test_correction_and_invalidation_routes_validate_and_replay(self):
        root = self.post(self.body(value=100)).json()["result"]["entry_id"]
        correction_url = f"/api/v1/readings/odometer/{root}/corrections"
        invalidation_url = f"/api/v1/readings/odometer/{root}/invalidations"
        fields = {"operation_id": str(uuid4()), "expected_tip_entry_id": root, "value": 110}
        corrected = self.client.post(
            correction_url,
            data=json.dumps(fields),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(corrected.status_code, 201)
        self.assertEqual(corrected.json()["result"]["root_entry_id"], root)
        replay = self.client.post(
            correction_url,
            data=json.dumps(fields),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(replay.json()["result"], corrected.json()["result"])
        other_client, _ = self.harness.sign_in("other-crew")
        other_client.get("/")
        other_token = other_client.cookies["csrftoken"].value
        denied_correction = other_client.post(
            correction_url,
            data=json.dumps(
                {
                    "operation_id": str(uuid4()),
                    "expected_tip_entry_id": root,
                    "value": 115,
                }
            ),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=other_token,
        )
        self.assertEqual(
            (denied_correction.status_code, denied_correction.json()["code"]),
            (403, "FORBIDDEN"),
        )
        refused = self.client.post(
            invalidation_url,
            data=json.dumps({"operation_id": str(uuid4()), "explanation": "Synthetic."}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(refused.status_code, 403)
        malformed = self.client.post(
            correction_url,
            data=json.dumps(
                {
                    "operation_id": str(uuid4()),
                    "expected_tip_entry_id": root,
                    "value": 120,
                    "actor_id": str(uuid4()),
                }
            ),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(
            (malformed.status_code, malformed.json()["code"]), (400, "MALFORMED_REQUEST")
        )

    def test_correction_exceeding_a_later_reading_requires_confirmation_via_api(self):
        first = self.client.post(
            "/api/v1/odometer-entries",
            data=json.dumps(
                {
                    "operation_id": str(uuid4()),
                    "vehicle_id": str(self.vehicle.pk),
                    "value": 100,
                    "unit": "mi",
                }
            ),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        first_id = first.json()["result"]["entry_id"]

        second = self.client.post(
            "/api/v1/odometer-entries",
            data=json.dumps(
                {
                    "operation_id": str(uuid4()),
                    "vehicle_id": str(self.vehicle.pk),
                    "value": 150,
                    "unit": "mi",
                }
            ),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(second.status_code, 201)

        correction_url = f"/api/v1/readings/odometer/{first_id}/corrections"
        # Correcting first reading (100) to 300 without confirm_anomaly
        refused = self.client.post(
            correction_url,
            data=json.dumps(
                {
                    "operation_id": str(uuid4()),
                    "expected_tip_entry_id": first_id,
                    "value": 300,
                }
            ),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "ANOMALY_CONFIRMATION_REQUIRED")
        self.assertEqual(
            refused.json()["field_errors"],
            [{"path": "value", "code": "ANOMALY_CONFIRMATION_REQUIRED"}],
        )

        # Correcting with confirm_anomaly=True succeeds
        confirmed = self.client.post(
            correction_url,
            data=json.dumps(
                {
                    "operation_id": str(uuid4()),
                    "expected_tip_entry_id": first_id,
                    "value": 300,
                    "confirm_anomaly": True,
                    "explanation": "Recalibrated speedometer.",
                }
            ),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(confirmed.status_code, 201)
        replacement_id = confirmed.json()["result"]["entry_id"]
        replacement = OdometerEntry.objects.get(pk=replacement_id)
        self.assertTrue(replacement.anomaly_confirmed)
        self.assertEqual(replacement.value, 300)
        self.assertEqual(replacement.confirmation_explanation, "Recalibrated speedometer.")
