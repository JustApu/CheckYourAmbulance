"""HTTP for paper-entry batch headers and performer search (issue #9 Part B1)."""

import json
from uuid import uuid4

from django.test import override_settings

from cya.paper.models import PaperEntryBatch
from cya.paper.tests.support import ZONE, PaperCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

BATCHES = "/api/v1/paper-entry-batches"
PERFORMERS = "/api/v1/paper-entry-performers"


@override_settings(**real_collaborator_settings())
class ApiCase(PaperCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        org = self.harness.org
        org.station_zone = ZONE
        org.save(update_fields=["station_zone"])
        return org

    def sign_in(self, subject, role=None):
        client, user = self.harness.sign_in(subject)
        if role:
            self.grant(user, role)
        client.get("/")  # obtains the CSRF cookie
        return client, user

    def body(self, user, **overrides):
        body = {
            "operation_id": str(uuid4()),
            "vehicle_id": str(self.vehicle.pk),
            "checklist_version_id": self.version,
            "performed_on": "2026-06-01",
            "performed_time": "07:30",
            "performer_ids": [str(user.pk)],
        }
        body.update(overrides)
        return {key: value for key, value in body.items() if value is not ...}

    def post(self, client, body, **extra):
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return client.post(
            BATCHES,
            data=data,
            content_type=extra.pop("content_type", "application/json"),
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
            **extra,
        )

    def rejected(self, response, status, code, paths=()):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertEqual((body["status"], body["code"]), ("REJECTED", code))
        self.assertEqual([e["path"] for e in body["field_errors"]], list(paths))


class BatchApiTests(ApiCase):
    def test_a_header_is_saved_and_replayed(self):
        client, user = self.sign_in("crew-http")
        body = self.body(user)
        response = self.post(client, body)
        self.assertEqual(response.status_code, 201, response.content)
        result = response.json()
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["result"]["performed_at"], "2026-06-01T11:30:00+00:00")
        self.assertEqual(result["result"]["performer_ids"], [str(user.pk)])
        again = self.post(client, body)
        self.assertEqual(again.json()["result"]["batch_id"], result["result"]["batch_id"])
        self.assertEqual(PaperEntryBatch.objects.count(), 1)
        reused = self.post(client, {**body, "performed_time": "07:31"})
        self.rejected(reused, 409, "OPERATION_REUSED", ["operation_id"])

    def test_crew_naming_someone_else_is_forbidden(self):
        client, user = self.sign_in("crew-http")
        response = self.post(client, self.body(user, performer_ids=[str(self.crew.pk)]))
        self.rejected(response, 403, "FORBIDDEN")
        self.assertFalse(PaperEntryBatch.objects.exists())

    def test_malformed_requests(self):
        client, user = self.sign_in("crew-http")
        for overrides, path in (
            ({"performed_on": "31/05/2026"}, "performed_on"),
            ({"performed_on": "2026-02-30"}, "performed_on"),
            ({"performed_time": "7:30"}, "performed_time"),
            ({"performed_time": "19:30:00"}, "performed_time"),
            ({"performer_ids": "x"}, "performer_ids"),
            ({"kit_definition_version_ids": None}, "kit_definition_version_ids"),
            ({"kit_definition_version_ids": 7}, "kit_definition_version_ids"),
            ({"kit_definition_version_ids": "x"}, "kit_definition_version_ids"),
            ({"actor_user_id": str(user.pk)}, "$"),
            ({"operation_id": ...}, "operation_id"),
        ):
            with self.subTest(path=path):
                self.rejected(
                    self.post(client, self.body(user, **overrides)),
                    400,
                    "MALFORMED_REQUEST",
                    [path],
                )
        self.rejected(self.post(client, "[]"), 400, "MALFORMED_REQUEST")
        self.rejected(
            self.post(client, self.body(user), content_type="text/plain"), 400, "MALFORMED_REQUEST"
        )

    def test_rule_refusals_name_their_field(self):
        client, user = self.sign_in("crew-http")
        self.rejected(
            self.post(client, self.body(user, performed_time="23:00")),
            422,
            "PERFORMED_IN_FUTURE",
            ["performed_time"],
        )
        self.rejected(
            self.post(client, self.body(user, checklist_version_id=str(uuid4()))),
            404,
            "NOT_FOUND",
            ["checklist_version_id"],
        )

    def test_sign_in_and_csrf_are_required(self):
        anonymous = self.client
        self.rejected(
            anonymous.post(BATCHES, data="{}", content_type="application/json"),
            401,
            "AUTHENTICATION_REQUIRED",
        )
        client, user = self.sign_in("crew-http")
        response = client.post(
            BATCHES, data=json.dumps(self.body(user)), content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get(BATCHES).status_code, 405)


class PerformerSearchTests(ApiCase):
    def test_a_supervisor_finds_active_and_inactive_users(self):
        client, _user = self.sign_in("supervisor-http", "SUPERVISOR")
        former = self.make_user("former-medic")
        former.active = False
        former.save(update_fields=["active"])
        response = client.get(PERFORMERS, {"q": "former"})
        self.assertEqual(response.status_code, 200, response.content)
        users = response.json()["users"]
        self.assertEqual(
            users,
            [
                {
                    "user_id": str(former.pk),
                    "display_name": "User former-medic",
                    "email": "former-medic@example.test",
                    "active": False,
                }
            ],
        )
        self.assertFalse(response.json()["truncated"])

    def test_results_are_bounded(self):
        client, _user = self.sign_in("supervisor-http", "SUPERVISOR")
        for n in range(22):
            self.make_user(f"bulk-{n:02d}")
        body = client.get(PERFORMERS, {"q": "bulk-"}).json()
        self.assertEqual(len(body["users"]), 20)
        self.assertTrue(body["truncated"])

    def test_crew_may_not_search(self):
        client, _user = self.sign_in("crew-http")
        self.rejected(client.get(PERFORMERS, {"q": "user"}), 403, "FORBIDDEN")

    def test_query_bounds(self):
        client, _user = self.sign_in("supervisor-http", "SUPERVISOR")
        for params in ({}, {"q": "a"}, {"q": "x" * 101}, {"q": "user", "page": "2"}):
            with self.subTest(params=params):
                self.rejected(client.get(PERFORMERS, params), 400, "MALFORMED_REQUEST", ["q"])

    def test_sign_in_is_required(self):
        self.rejected(self.client.get(PERFORMERS, {"q": "user"}), 401, "AUTHENTICATION_REQUIRED")
