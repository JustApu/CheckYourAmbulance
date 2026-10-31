"""Crew inspection pages and session endpoints (issue 85 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/85)), over real HTTP sign-in.

The browser script is not executed here: these tests cover what the server owns (authorization,
participation, escaping, CSRF, the session and checklist contracts the script relies on) and the
markup the script needs. Script behavior is checked by hand; see docs/crew-inspection-ui.md.
"""

import json
import re
from uuid import uuid4

from django.test import Client, override_settings

from cya.attention.tests.support import AttentionCase
from cya.checklists.models import Vehicle
from cya.inspections.models import InspectionSession
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

SESSIONS = "/api/v1/inspection-sessions"


@override_settings(**real_collaborator_settings())
class CrewUiCase(AttentionCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client, self.user = self.harness.sign_in("crew-ui")
        self.token = self.csrf(self.client)

    @staticmethod
    def csrf(client):
        client.get("/")
        return client.cookies["csrftoken"].value

    def post(self, url, body, client=None, token=None):
        client = client or self.client
        token = token or self.token
        return client.post(
            url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    def open(self, client=None, token=None, **over):
        body = {
            "operation_id": str(uuid4()),
            "vehicle_id": str(self.vehicle.pk),
            "template_id": str(self.template.pk),
            **over,
        }
        return self.post(SESSIONS, body, client, token)

    def config(self, response):
        found = re.search(
            r'<script id="cya-config" type="application/json">(.*?)</script>',
            response.content.decode(),
            re.S,
        )
        self.assertIsNotNone(found, "page carries its config")
        return json.loads(found.group(1))

    def second_user(self, sub="crew-ui-2"):
        client, user = self.harness.sign_in(sub)
        return client, user, self.csrf(client)


class SessionApiTests(CrewUiCase):
    def test_open_then_join_then_replay(self):
        first = self.open()
        self.assertEqual(first.status_code, 200, first.content)
        result = first.json()["result"]
        self.assertEqual(result["code"], "SESSION_OPENED")
        self.assertEqual(result["state"], "OPEN")
        self.assertTrue(result["checklist_version_id"])

        other, _user, token = self.second_user()
        joined = self.open(other, token).json()["result"]
        self.assertEqual(joined["code"], "SESSION_JOINED")
        self.assertEqual(joined["session_id"], result["session_id"])

        replay_id = str(uuid4())
        one = self.open(operation_id=replay_id).json()
        two = self.open(operation_id=replay_id).json()
        self.assertEqual(one["result"], two["result"])
        self.assertEqual(InspectionSession.objects.count(), 1)

    def test_separate_always_opens_another(self):
        first = self.open().json()["result"]["session_id"]
        second = self.open(separate=True).json()["result"]["session_id"]
        self.assertNotEqual(first, second)

    def test_malformed_requests_are_refused_without_a_write(self):
        before = InspectionSession.objects.count()
        cases = [
            {"vehicle_id": "not-a-uuid"},
            {"template_id": str(self.template.pk).upper()},
            {"operation_id": "not-a-uuid"},
            {"operation_id": "11111111-1111-1111-8111-111111111111"},  # not a version 4 id
            {"separate": "yes"},
            {"extra": 1},
        ]
        for over in cases:
            with self.subTest(over=over):
                response = self.open(**over)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json()["status"], "REJECTED")
        self.assertEqual(InspectionSession.objects.count(), before)

    def test_missing_required_field_and_wrong_media_type(self):
        body = {"operation_id": str(uuid4()), "vehicle_id": str(self.vehicle.pk)}
        self.assertEqual(self.post(SESSIONS, body).status_code, 400)
        form = self.client.post(SESSIONS, data=body, HTTP_X_CSRFTOKEN=self.token)
        self.assertEqual(form.status_code, 400)

    def test_authentication_csrf_and_method(self):
        body = {
            "operation_id": str(uuid4()),
            "vehicle_id": str(self.vehicle.pk),
            "template_id": str(self.template.pk),
        }
        anonymous = Client()
        unauthenticated = anonymous.post(
            SESSIONS, data=json.dumps(body), content_type="application/json"
        )
        self.assertEqual(unauthenticated.status_code, 401)
        no_token = self.client.post(
            SESSIONS, data=json.dumps(body), content_type="application/json"
        )
        self.assertEqual(no_token.status_code, 403)
        self.assertEqual(self.client.get(SESSIONS).status_code, 405)
        self.assertEqual(InspectionSession.objects.count(), 0)

    def test_an_unavailable_template_is_a_safe_refusal(self):
        response = self.open(template_id=str(uuid4()))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "SESSION_UNAVAILABLE")
        self.assertNotIn("message", response.json())
        self.assertEqual(InspectionSession.objects.count(), 0)

    def test_end_then_reopen_by_a_participant(self):
        session_id = self.open().json()["result"]["session_id"]
        ended = self.post(f"{SESSIONS}/{session_id}/end", {"operation_id": str(uuid4())})
        self.assertEqual(ended.status_code, 200, ended.content)
        self.assertEqual(ended.json()["result"]["state"], "CLOSED")
        again = self.post(f"{SESSIONS}/{session_id}/end", {"operation_id": str(uuid4())})
        self.assertEqual(again.status_code, 409)
        reopened = self.post(f"{SESSIONS}/{session_id}/reopen", {"operation_id": str(uuid4())})
        self.assertEqual(reopened.status_code, 200, reopened.content)
        self.assertEqual(reopened.json()["result"]["state"], "OPEN")
        already = self.post(f"{SESSIONS}/{session_id}/reopen", {"operation_id": str(uuid4())})
        self.assertEqual(already.status_code, 409)
        self.assertEqual(already.json()["code"], "SESSION_UNAVAILABLE")

    def test_end_state_must_be_closed_or_incomplete(self):
        session_id = self.open().json()["result"]["session_id"]
        response = self.post(
            f"{SESSIONS}/{session_id}/end", {"operation_id": str(uuid4()), "state": "OPEN"}
        )
        self.assertEqual(response.status_code, 400)
        incomplete = self.post(
            f"{SESSIONS}/{session_id}/end", {"operation_id": str(uuid4()), "state": "INCOMPLETE"}
        )
        self.assertEqual(incomplete.json()["result"]["state"], "INCOMPLETE")

    def test_only_a_participant_may_reopen_or_end(self):
        session_id = self.open().json()["result"]["session_id"]
        other, _user, token = self.second_user()
        end = self.post(
            f"{SESSIONS}/{session_id}/end", {"operation_id": str(uuid4())}, other, token
        )
        self.assertEqual((end.status_code, end.json()["code"]), (403, "FORBIDDEN"))
        self.post(f"{SESSIONS}/{session_id}/end", {"operation_id": str(uuid4())})
        reopen = self.post(
            f"{SESSIONS}/{session_id}/reopen", {"operation_id": str(uuid4())}, other, token
        )
        self.assertEqual((reopen.status_code, reopen.json()["code"]), (403, "FORBIDDEN"))
        unknown = self.post(
            f"{SESSIONS}/{uuid4()}/reopen", {"operation_id": str(uuid4())}, other, token
        )
        self.assertEqual((unknown.status_code, unknown.json()["code"]), (403, "FORBIDDEN"))

    def test_a_bad_session_id_is_a_client_error(self):
        response = self.post(f"{SESSIONS}/not-a-uuid/reopen", {"operation_id": str(uuid4())})
        self.assertEqual(response.status_code, 400)


class PageTests(CrewUiCase):
    def test_signed_out_visitors_are_sent_to_sign_in(self):
        anonymous = Client()
        for url in (
            "/inspection/",
            f"/inspection/vehicles/{self.vehicle.pk}/",
            f"/inspection/sessions/{uuid4()}/",
        ):
            with self.subTest(url=url):
                response = anonymous.get(url)
                self.assertEqual(
                    (response.status_code, response["Location"]), (302, "/auth/login/")
                )

    def test_the_vehicle_list_shows_active_vehicles_and_escapes_names(self):
        Vehicle.objects.filter(pk=self.vehicle.pk).update(display_name='<script>x</script> "A"')
        page = self.client.get("/inspection/")
        html = page.content.decode()
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
        self.assertIn(f"/inspection/vehicles/{self.vehicle.pk}/", html)
        self.assertIn("In Service", html)
        self.assertIn('aria-current="page"', html)

    def test_an_inactive_vehicle_is_not_listed_or_reachable(self):
        Vehicle.objects.filter(pk=self.vehicle.pk).update(active=False)
        self.assertNotIn(str(self.vehicle.pk), self.client.get("/inspection/").content.decode())
        page = self.client.get(f"/inspection/vehicles/{self.vehicle.pk}/")
        self.assertEqual(page.status_code, 404)

    def test_the_vehicle_page_offers_the_bound_template_and_keeps_the_section_current(self):
        page = self.client.get(f"/inspection/vehicles/{self.vehicle.pk}/")
        html = page.content.decode()
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'data-template="{self.template.pk}"', html)
        self.assertIn('aria-current="page"', html)
        self.assertIn("X-CSRFToken", html)
        config = self.config(page)
        self.assertEqual(config["vehicle_id"], str(self.vehicle.pk))

    def test_unknown_and_malformed_vehicle_ids_are_404(self):
        for value in (str(uuid4()), "not-a-uuid"):
            with self.subTest(value=value):
                self.assertEqual(self.client.get(f"/inspection/vehicles/{value}/").status_code, 404)

    def test_another_organizations_vehicle_is_404(self):
        other = self.make_other_org_vehicle()
        self.assertEqual(self.client.get(f"/inspection/vehicles/{other.pk}/").status_code, 404)

    def make_other_org_vehicle(self):
        from cya.accounts.models import Organization

        org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        return Vehicle.objects.create(organization=org, display_name="Elsewhere")

    def test_the_session_page_is_for_participants_only(self):
        session_id = self.open().json()["result"]["session_id"]
        page = self.client.get(f"/inspection/sessions/{session_id}/")
        self.assertEqual(page.status_code, 200)
        config = self.config(page)
        self.assertEqual(config["session_id"], session_id)
        self.assertEqual(config["vehicle_id"], str(self.vehicle.pk))
        self.assertEqual(config["state"], "OPEN")
        self.assertEqual(config["view"], "NORMAL")
        self.assertFalse(config["needs_attention_only"])
        self.assertEqual(config["urls"]["socket"], f"/ws/v1/vehicles/{self.vehicle.pk}/live/")
        self.assertEqual(config["urls"]["checks"], "/api/v1/checks")
        html = page.content.decode()
        self.assertIn('name="csrfmiddlewaretoken"', html)
        self.assertIn('id="odometer-form"', html)
        self.assertIn('id="conflict-dialog"', html)
        self.assertNotIn('id="reopen-session"', html)
        # An Unconfirmed save may have committed, so the live-loss banner must not say that
        # anything not marked Saved was not recorded (REC-4).
        banner = re.search(r'<div id="live-banner".*?</div>', html, re.S).group(0)
        self.assertIn('id="live-detail"', banner)
        self.assertIn("Unconfirmed", banner)
        self.assertNotIn("not been recorded", banner)

        other, _user, _token = self.second_user()
        unknown = other.get(f"/inspection/sessions/{uuid4()}/")
        stranger = other.get(f"/inspection/sessions/{session_id}/")
        self.assertEqual((unknown.status_code, stranger.status_code), (404, 404))

    def test_an_ended_session_page_offers_reopen_and_no_recording(self):
        session_id = self.open().json()["result"]["session_id"]
        self.post(f"{SESSIONS}/{session_id}/end", {"operation_id": str(uuid4())})
        page = self.client.get(f"/inspection/sessions/{session_id}/")
        html = page.content.decode()
        self.assertEqual(self.config(page)["state"], "CLOSED")
        self.assertIn('id="reopen-session"', html)
        self.assertNotIn('id="odometer-form"', html)

    def test_the_page_uses_the_saved_view_preference(self):
        session_id = self.open().json()["result"]["session_id"]
        self.post(
            "/api/v1/me/view-preferences",
            {
                "operation_id": str(uuid4()),
                "preferred_view": "PRIORITY",
                "needs_attention_only": True,
            },
        )
        config = self.config(self.client.get(f"/inspection/sessions/{session_id}/"))
        self.assertEqual((config["view"], config["needs_attention_only"]), ("PRIORITY", True))

    def test_the_vehicle_page_lists_open_sessions_and_own_ended_ones_only(self):
        mine = self.open().json()["result"]["session_id"]
        other, _user, token = self.second_user()
        theirs = self.open(other, token, separate=True).json()["result"]["session_id"]
        self.post(f"{SESSIONS}/{theirs}/end", {"operation_id": str(uuid4())}, other, token)
        html = self.client.get(f"/inspection/vehicles/{self.vehicle.pk}/").content.decode()
        self.assertIn(f"/inspection/sessions/{mine}/", html)
        self.assertNotIn(f"/inspection/sessions/{theirs}/", html)  # ended and not mine

    def test_a_user_with_no_role_is_refused_and_the_section_stays_hidden(self):
        urls = ("/inspection/", f"/inspection/vehicles/{self.vehicle.pk}/")
        with override_settings(CYA_ROLE_LOOKUP=None):
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)


class ChecklistContractTests(CrewUiCase):
    """The row fields the screen records checks with."""

    def rows(self):
        return self.client.get(f"/api/v1/vehicles/{self.vehicle.pk}/checklist").json()["items"]

    def test_rows_name_the_version_to_record_against_and_how_to_enter_a_value(self):
        by_item = {r["item_id"]: r for r in self.rows()}
        suction = by_item[str(self.suction.pk)]
        self.assertEqual(suction["checklist_version_id"], self.version_id)
        self.assertEqual(suction["allowed_outcomes"][0], "PASSED")
        for row in by_item.values():
            self.assertEqual(row["checklist_version_id"], self.version_id)
            if row["kind"] == "QUANTITY":
                self.assertIsNotNone(row["expected_quantity"])

    def test_the_newest_check_is_reported_even_when_it_is_a_pass(self):
        before = next(r for r in self.rows() if r["item_id"] == str(self.suction.pk))
        self.assertIsNone(before["latest_check_id"])
        recorded = self.record(self.suction, actor=self.supervisor)
        after = next(r for r in self.rows() if r["item_id"] == str(self.suction.pk))
        self.assertEqual(after["latest_check_id"], recorded.body["check_id"])
        self.assertEqual(after["latest_check_outcome"], "PASSED")
        self.assertEqual(after["latest_check_by"], self.supervisor.display_name)
        self.assertEqual(after["item_attention"], "CLEAR")
        self.assertIsNone(after["attention_check_id"])  # only a red check is named there
        self.assertTrue(after["latest_check_at"].endswith("Z"))

    def test_a_check_recorded_through_the_basis_the_screen_sends_commits_without_a_warning(self):
        self.record(self.suction, actor=self.supervisor)
        shown = next(r for r in self.rows() if r["item_id"] == str(self.suction.pk))
        response = self.post(
            "/api/v1/checks",
            {
                "operation_id": str(uuid4()),
                "vehicle_id": str(self.vehicle.pk),
                "item_id": shown["item_id"],
                "checklist_version_id": shown["checklist_version_id"],
                "purpose": "ROUTINE",
                "outcome": "PASSED",
                "basis_check_id": shown["latest_check_id"],
            },
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_a_stale_basis_gets_the_conflict_warning_the_dialog_shows(self):
        shown = next(r for r in self.rows() if r["item_id"] == str(self.suction.pk))
        newer = self.record(self.suction, actor=self.supervisor)
        response = self.post(
            "/api/v1/checks",
            {
                "operation_id": str(uuid4()),
                "vehicle_id": str(self.vehicle.pk),
                "item_id": shown["item_id"],
                "checklist_version_id": shown["checklist_version_id"],
                "purpose": "ROUTINE",
                "outcome": "PASSED",
                "basis_check_id": shown["latest_check_id"],
            },
        )
        body = response.json()
        self.assertEqual((response.status_code, body["status"]), (409, "CONFLICT"))
        self.assertEqual(body["newest_check"]["check_id"], newer.body["check_id"])
        self.assertEqual(
            body["newest_check"]["actor"]["display_name"], self.supervisor.display_name
        )

    def test_proceeding_from_the_warning_alone_records_both_checks(self):
        # The dialog keeps its row even if a reload drops it from the list, so the proceed is
        # built from what the warning named: its newest check is both basis and acknowledgement.
        shown = next(r for r in self.rows() if r["item_id"] == str(self.suction.pk))
        newer = self.record(self.suction, actor=self.supervisor)
        newest_id = newer.body["check_id"]
        body = {
            "vehicle_id": str(self.vehicle.pk),
            "item_id": shown["item_id"],
            "checklist_version_id": shown["checklist_version_id"],
            "purpose": "ROUTINE",
            "outcome": "PASSED",
            "basis_check_id": shown["latest_check_id"],
        }
        first = self.post("/api/v1/checks", {**body, "operation_id": str(uuid4())})
        self.assertEqual(first.status_code, 409)
        self.assertEqual(first.json()["newest_check"]["check_id"], newest_id)
        proceed = self.post(
            "/api/v1/checks",
            {
                **body,
                "operation_id": str(uuid4()),
                "basis_check_id": newest_id,
                "acknowledges_check_id": newest_id,
            },
        )
        self.assertEqual(proceed.status_code, 201, proceed.content)
        latest = next(r for r in self.rows() if r["item_id"] == str(self.suction.pk))
        self.assertEqual(latest["latest_check_id"], proceed.json()["result"]["check_id"])
