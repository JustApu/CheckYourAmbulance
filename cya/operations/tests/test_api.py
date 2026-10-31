"""Status endpoints and pages over real HTTP sign-in (issue 148 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/148))."""

import json
from uuid import uuid4

from django.test import Client, override_settings

from cya.accounts.models import Organization, User
from cya.authorization.commands import change_role
from cya.checklists.models import Vehicle
from cya.checklists.tests.support import ids
from cya.operations.models import StatusRequest, VehicleStatusEvent
from cya.operations.tests.support import StatusCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


@override_settings(**real_collaborator_settings())
class StatusHttpCase(StatusCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client, self.user = self.harness.sign_in("status-crew")
        self.token = self.csrf(self.client)

    @staticmethod
    def csrf(client):
        client.get("/")
        return client.cookies["csrftoken"].value

    def as_role(self, role, sub):
        client, user = self.harness.sign_in(sub)
        if role != "CREW":
            self.grant(user, role)
        return client, self.csrf(client)

    def post(self, url, body, client=None, token=None):
        return (client or self.client).post(
            url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token or self.token,
        )

    def change(self, status="OUT_OF_SERVICE", client=None, token=None, **extra):
        body = {
            "operation_id": str(uuid4()),
            "status": status,
            "explanation": "Synthetic reason.",
            **extra,
        }
        return self.post(f"/api/v1/vehicles/{self.vehicle.pk}/status-changes", body, client, token)

    def ask(self, status="OUT_OF_SERVICE", client=None, token=None, **extra):
        body = {
            "operation_id": str(uuid4()),
            "requested_status": status,
            "reason": "Synthetic request.",
            **extra,
        }
        return self.post(f"/api/v1/vehicles/{self.vehicle.pk}/status-requests", body, client, token)

    def post_decision(self, request_id, decision="APPROVE", client=None, token=None, **extra):
        body = {
            "operation_id": str(uuid4()),
            "decision": decision,
            "explanation": "Synthetic decision.",
            **extra,
        }
        return self.post(f"/api/v1/status-requests/{request_id}/decisions", body, client, token)

    def status(self, client=None):
        return (client or self.client).get(f"/api/v1/vehicles/{self.vehicle.pk}/status")


class AuthorizationTests(StatusHttpCase):
    def test_every_endpoint_requires_sign_in(self):
        anonymous = Client()
        urls = (
            f"/api/v1/vehicles/{self.vehicle.pk}/status",
            "/api/v1/status-requests",
        )
        for url in urls:
            self.assertEqual(anonymous.get(url).status_code, 401, url)
        posts = (
            f"/api/v1/vehicles/{self.vehicle.pk}/status-changes",
            f"/api/v1/vehicles/{self.vehicle.pk}/status-requests",
            f"/api/v1/status-requests/{uuid4()}/decisions",
        )
        for url in posts:
            self.assertIn(self.post(url, {}, client=anonymous, token="x").status_code, (401, 403))
        self.assertFalse(VehicleStatusEvent.objects.exists())

    def test_crew_may_request_and_read_but_not_set_or_decide(self):
        self.assertEqual(self.change().status_code, 403)
        asked = self.ask()
        self.assertEqual(asked.status_code, 200, asked.content)
        self.assertEqual(self.post_decision(asked.json()["result"]["request_id"]).status_code, 403)
        self.assertEqual(self.status().status_code, 200)
        self.assertEqual(self.client.get("/api/v1/status-requests").status_code, 200)
        self.assertFalse(VehicleStatusEvent.objects.exists())

    def test_supply_officer_alone_cannot_request_or_set_but_can_read(self):
        client, token = self.as_role("SUPPLY_OFFICER", "status-supply")
        self.assertEqual(self.ask(client=client, token=token).status_code, 200)  # also holds Crew
        user = User.objects.get(idp_subject="status-supply")
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(user.pk),
            role="CREW",
            action="REVOKE",
            **ids(),
        )
        self.assertEqual(self.ask(client=client, token=token).status_code, 403)
        self.assertEqual(self.change(client=client, token=token).status_code, 403)
        self.assertEqual(self.status(client).status_code, 200)

    def test_supervisor_and_administrator_may_set_and_decide(self):
        asked = self.ask().json()["result"]["request_id"]
        for role, sub in (("SUPERVISOR", "status-sup"), ("ADMINISTRATOR", "status-adm")):
            client, token = self.as_role(role, sub)
            status = "LIMITED_SERVICE" if role == "SUPERVISOR" else "OUT_OF_SERVICE"
            extra = {"restrictions": "No siren."} if role == "SUPERVISOR" else {}
            self.assertEqual(
                self.change(status, client=client, token=token, **extra).status_code, 200, role
            )
        self.assertEqual(
            self.post_decision(asked, "DENY", client=client, token=token).status_code, 200
        )

    def test_a_user_with_no_current_role_is_refused_every_read(self):
        with override_settings(CYA_ROLE_LOOKUP=None):
            for url in (f"/api/v1/vehicles/{self.vehicle.pk}/status", "/api/v1/status-requests"):
                self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_another_organizations_vehicle_and_request_look_missing(self):
        client, token = self.as_role("SUPERVISOR", "status-sup")
        other = Organization.objects.create(name="Synthetic other", station_zone="UTC")
        foreign = Vehicle.objects.create(organization=other, display_name="Synthetic elsewhere")
        self.assertEqual(self.client.get(f"/api/v1/vehicles/{foreign.pk}/status").status_code, 404)
        body = {
            "operation_id": str(uuid4()),
            "status": "OUT_OF_SERVICE",
            "explanation": "Synthetic.",
        }
        url = f"/api/v1/vehicles/{foreign.pk}/status-changes"
        self.assertEqual(self.post(url, body, client, token).status_code, 404)
        missing = self.post_decision(str(uuid4()), client=client, token=token)
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "NOT_FOUND"))
        self.assertFalse(VehicleStatusEvent.objects.exists())


class CommandTests(StatusHttpCase):
    def setUp(self):
        super().setUp()
        self.sup, self.sup_token = self.as_role("SUPERVISOR", "status-sup")

    def test_set_status_records_an_event_and_replays_on_the_same_operation(self):
        body = {
            "operation_id": str(uuid4()),
            "status": "LIMITED_SERVICE",
            "explanation": "Synthetic.",
            "restrictions": "No lights.",
        }
        url = f"/api/v1/vehicles/{self.vehicle.pk}/status-changes"
        first = self.post(url, body, self.sup, self.sup_token)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()["status"], "COMMITTED")
        self.assertEqual(first.json()["result"]["code"], "STATUS_SET")
        replay = self.post(url, body, self.sup, self.sup_token)
        self.assertEqual(replay.json()["result"], first.json()["result"])
        self.assertEqual(self.events().count(), 1)
        reused = self.post(url, {**body, "explanation": "Different."}, self.sup, self.sup_token)
        self.assertEqual((reused.status_code, reused.json()["code"]), (409, "OPERATION_REUSED"))

    def test_rules_are_reported_with_stable_codes_and_write_nothing(self):
        self.assertEqual(self.change("OUT_OF_SERVICE", self.sup, self.sup_token).status_code, 200)
        cases = {
            "no change": (
                self.change("OUT_OF_SERVICE", self.sup, self.sup_token),
                409,
                "NO_CHANGE",
            ),
            "limited without restrictions": (
                self.change("LIMITED_SERVICE", self.sup, self.sup_token),
                400,
                "MALFORMED_REQUEST",
            ),
            "restrictions on in service": (
                self.change("IN_SERVICE", self.sup, self.sup_token, restrictions="No."),
                400,
                "MALFORMED_REQUEST",
            ),
            "blank explanation": (
                self.change("IN_SERVICE", self.sup, self.sup_token, explanation=" "),
                400,
                "MALFORMED_REQUEST",
            ),
            "unknown status": (
                self.change("DEFECT_PENDING", self.sup, self.sup_token),
                400,
                "MALFORMED_REQUEST",
            ),
        }
        for label, (response, status, code) in cases.items():
            with self.subTest(label):
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.assertEqual(self.events().count(), 1)

    def test_malformed_bodies_are_refused_before_any_command(self):
        url = f"/api/v1/vehicles/{self.vehicle.pk}/status-changes"
        good = {
            "operation_id": str(uuid4()),
            "status": "OUT_OF_SERVICE",
            "explanation": "Synthetic.",
        }
        bad = [
            {**good, "extra": 1},
            {k: v for k, v in good.items() if k != "status"},
            {**good, "operation_id": "not-a-uuid"},
            {**good, "operation_id": "11111111-1111-1111-8111-111111111111"},
            {**good, "restrictions": 5},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.post(url, body, self.sup, self.sup_token).status_code, 400)
        raw = self.sup.post(
            url, data="[]", content_type="application/json", HTTP_X_CSRFTOKEN=self.sup_token
        )
        self.assertEqual(raw.status_code, 400)
        text = self.sup.post(
            url, data="{}", content_type="text/plain", HTTP_X_CSRFTOKEN=self.sup_token
        )
        self.assertEqual(text.status_code, 400)
        self.assertEqual(
            self.post(
                "/api/v1/vehicles/not-a-uuid/status-changes", good, self.sup, self.sup_token
            ).status_code,
            400,
        )
        self.assertFalse(self.events().exists())

    def test_request_then_approve_sets_status_and_a_second_decision_is_refused(self):
        asked = self.ask("LIMITED_SERVICE").json()["result"]["request_id"]
        missing = self.post_decision(asked, "APPROVE", self.sup, self.sup_token)
        self.assertEqual((missing.status_code, missing.json()["code"]), (400, "MALFORMED_REQUEST"))
        approved = self.post_decision(
            asked, "APPROVE", self.sup, self.sup_token, restrictions="No siren."
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        self.assertEqual(self.events().get().restrictions, "No siren.")
        race = self.post_decision(asked, "DENY", self.sup, self.sup_token)
        self.assertEqual((race.status_code, race.json()["code"]), (409, "ALREADY_DECIDED"))
        self.assertEqual(self.events().count(), 1)
        self.assertEqual(StatusRequest.objects.filter(action="DENY").count(), 0)

    def test_denial_needs_an_explanation_and_changes_no_status(self):
        asked = self.ask().json()["result"]["request_id"]
        blank = self.post_decision(asked, "DENY", self.sup, self.sup_token, explanation=" ")
        self.assertEqual(blank.status_code, 400)
        denied = self.post_decision(asked, "DENY", self.sup, self.sup_token)
        self.assertEqual(denied.status_code, 200)
        self.assertFalse(self.events().exists())
        self.assertEqual(self.client.get("/api/v1/status-requests").json()["requests"], [])

    def test_two_supervisors_deciding_one_request_get_exactly_one_outcome(self):
        asked = self.ask().json()["result"]["request_id"]
        other, other_token = self.as_role("SUPERVISOR", "status-sup-2")
        codes = sorted(
            (
                self.post_decision(asked, "APPROVE", self.sup, self.sup_token).status_code,
                self.post_decision(asked, "DENY", other, other_token).status_code,
            )
        )
        self.assertEqual(codes, [200, 409])
        self.assertEqual(StatusRequest.objects.filter(request_id=asked).count(), 1)


class ReadTests(StatusHttpCase):
    def test_status_view_has_current_history_and_only_undecided_requests(self):
        empty = self.status().json()
        self.assertEqual(
            (empty["current"], empty["history"], empty["pending_requests"]), (None, [], [])
        )
        self.set_status("OUT_OF_SERVICE")
        self.set_status("LIMITED_SERVICE", restrictions="No siren.")
        decided = self.request("IN_SERVICE").body["request_id"]
        self.decide(decided, "DENY")
        open_request = self.request("IN_SERVICE", reason="Synthetic open.").body["request_id"]
        view = self.status().json()
        self.assertEqual(view["current"]["status"], "LIMITED_SERVICE")
        self.assertEqual([e["sequence"] for e in view["history"]], [2, 1])
        self.assertEqual([r["request_id"] for r in view["pending_requests"]], [open_request])
        self.assertEqual(view["pending_requests"][0]["reason"], "Synthetic open.")
        listed = self.client.get("/api/v1/status-requests").json()
        self.assertEqual([r["request_id"] for r in listed["requests"]], [open_request])
        narrowed = self.client.get(f"/api/v1/status-requests?vehicle_id={self.vehicles[1].pk}")
        self.assertEqual(narrowed.json()["requests"], [])

    def test_reads_refuse_unknown_and_repeated_parameters(self):
        for url in (
            f"/api/v1/vehicles/{self.vehicle.pk}/status?x=1",
            "/api/v1/status-requests?x=1",
            "/api/v1/status-requests?vehicle_id=a&vehicle_id=b",
            "/api/v1/status-requests?vehicle_id=not-a-uuid",
        ):
            self.assertEqual(self.client.get(url).status_code, 400, url)

    def test_checklist_reader_shows_the_new_status_and_exempt_obligations_stay_exempt(self):
        client, token = self.as_role("SUPERVISOR", "status-sup")
        self.assertEqual(self.change("OUT_OF_SERVICE", client, token).status_code, 200)
        checklist = self.client.get(f"/api/v1/vehicles/{self.vehicle.pk}/checklist").json()
        self.assertEqual(checklist["vehicle_status"]["status"], "OUT_OF_SERVICE")
        self.assertNotIn("PASSED", {row["obligation_outcome"] for row in checklist["items"]})
