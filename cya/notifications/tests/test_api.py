"""Notification endpoints over real HTTP sign-in (issue 147 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/147))."""

import json
from uuid import uuid4

from django.test import Client, override_settings

from cya.notifications import delivery
from cya.notifications.models import NotificationPolicy, NotificationPref, NotificationRead
from cya.notifications.tests.support import NotificationCase
from cya.testing.notifications import StubSender, transient
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


@override_settings(**real_collaborator_settings())
class NotificationHttpCase(NotificationCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client, self.user = self.harness.sign_in("notify-crew")
        self.token = self.csrf(self.client)

    @staticmethod
    def csrf(client):
        client.get("/")
        return client.cookies["csrftoken"].value

    def as_role(self, role, sub):
        client, user = self.harness.sign_in(sub)
        if role != "CREW":
            self.grant(user, role)
        return client, user, self.csrf(client)

    def post(self, url, body, client=None, token=None):
        return (client or self.client).post(
            url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token or self.token,
        )

    def deliver_to(self, user):
        self.emit(user_ids=[str(user.pk)])

    def inbox(self, client=None, query=""):
        return (client or self.client).get(f"/api/v1/notifications{query}")


class InboxEndpointTests(NotificationHttpCase):
    def test_every_endpoint_requires_sign_in(self):
        anonymous = Client()
        for url in (
            "/api/v1/notifications",
            "/api/v1/notification-preferences",
            "/api/v1/notification-policy",
            "/api/v1/notification-failures",
        ):
            self.assertEqual(anonymous.get(url).status_code, 401, url)
        for url in (
            f"/api/v1/notifications/{uuid4()}/reads",
            "/api/v1/notification-preferences",
            "/api/v1/notification-policy",
        ):
            self.assertIn(self.post(url, {}, client=anonymous, token="x").status_code, (401, 403))

    def test_inbox_lists_only_the_callers_items_with_deep_links_and_unread_count(self):
        other, other_user, _ = self.as_role("CREW", "notify-other")
        self.deliver_to(self.user)
        self.deliver_to(other_user)
        body = self.inbox().json()
        self.assertEqual((len(body["items"]), body["unread_count"]), (1, 1))
        item = body["items"][0]
        self.assertEqual(item["deep_link"], "/status/vehicles/synthetic/")
        self.assertFalse(item["read"])
        self.assertNotIn("recipient_user_id", item)
        self.assertEqual(len(self.inbox(other).json()["items"]), 1)

    def test_query_is_validated_and_unknown_parameters_refused(self):
        for query in (
            "?limit=0",
            "?limit=201",
            "?limit=x",
            "?unread=2",
            "?user=1",
            "?limit=1&limit=2",
        ):
            with self.subTest(query):
                self.assertEqual(self.inbox(query=query).status_code, 400)

    def test_mark_read_is_own_only_and_idempotent(self):
        stranger, _, stranger_token = self.as_role("SUPERVISOR", "notify-stranger")
        self.deliver_to(self.user)
        item = self.inbox().json()["items"][0]["id"]
        url = f"/api/v1/notifications/{item}/reads"
        refused = self.post(url, {"operation_id": str(uuid4())}, stranger, stranger_token)
        self.assertEqual((refused.status_code, refused.json()["code"]), (404, "NOT_FOUND"))
        operation = str(uuid4())
        first = self.post(url, {"operation_id": operation})
        self.assertEqual((first.status_code, first.json()["result"]["code"]), (200, "SAVED"))
        replay = self.post(url, {"operation_id": operation})
        self.assertEqual(replay.json()["result"]["code"], "SAVED")
        again = self.post(url, {"operation_id": str(uuid4())})
        self.assertEqual(again.json()["result"]["code"], "ALREADY_READ")
        self.assertEqual(NotificationRead.objects.count(), 1)
        body = self.inbox().json()
        self.assertEqual((body["unread_count"], body["items"][0]["read"]), (0, True))
        self.assertEqual(self.inbox(query="?unread=1").json()["items"], [])

    def test_malformed_writes_are_refused(self):
        self.deliver_to(self.user)
        item = self.inbox().json()["items"][0]["id"]
        cases = (
            (f"/api/v1/notifications/{item}/reads", {}),
            (f"/api/v1/notifications/{item}/reads", {"operation_id": "nope"}),
            (f"/api/v1/notifications/{item}/reads", {"operation_id": str(uuid4()), "user": "x"}),
            ("/api/v1/notifications/not-an-id/reads", {"operation_id": str(uuid4())}),
        )
        for url, body in cases:
            with self.subTest(url=url, body=body):
                self.assertEqual(self.post(url, body).status_code, 400)
        self.assertFalse(NotificationRead.objects.exists())


class PreferenceAndPolicyEndpointTests(NotificationHttpCase):
    def pref(self, enabled, client=None, token=None, channel="EMAIL"):
        return self.post(
            "/api/v1/notification-preferences",
            {
                "operation_id": str(uuid4()),
                "event_type": "status.changed",
                "channel": channel,
                "enabled": enabled,
            },
            client,
            token,
        )

    def policy(self, mandatory, client=None, token=None):
        return self.post(
            "/api/v1/notification-policy",
            {
                "operation_id": str(uuid4()),
                "event_type": "status.changed",
                "channel": "EMAIL",
                "mandatory": mandatory,
            },
            client,
            token,
        )

    def test_preferences_are_per_user_and_listed_only_for_the_caller(self):
        other, other_user, other_token = self.as_role("CREW", "notify-other")
        self.assertEqual(self.pref(False).status_code, 200)
        self.assertEqual(self.pref(True, other, other_token).status_code, 200)
        mine = self.client.get("/api/v1/notification-preferences").json()["preferences"]
        theirs = other.get("/api/v1/notification-preferences").json()["preferences"]
        self.assertEqual([p["enabled"] for p in mine], [False])
        self.assertEqual([p["enabled"] for p in theirs], [True])
        self.assertEqual(NotificationPref.objects.count(), 2)

    def test_policy_is_administrator_only_and_audited(self):
        sup, _, sup_token = self.as_role("SUPERVISOR", "notify-sup")
        for client, token in ((self.client, self.token), (sup, sup_token)):
            self.assertEqual(self.policy(True, client, token).status_code, 403)
            self.assertEqual(client.get("/api/v1/notification-policy").status_code, 403)
            self.assertEqual(client.get("/api/v1/notification-failures").status_code, 403)
        admin, _, admin_token = self.as_role("ADMINISTRATOR", "notify-admin")
        self.assertEqual(self.policy(True, admin, admin_token).status_code, 200)
        listed = admin.get("/api/v1/notification-policy").json()["policy"]
        self.assertEqual(
            listed, [{"event_type": "status.changed", "channel": "EMAIL", "mandatory": True}]
        )
        self.assertEqual(NotificationPolicy.objects.count(), 1)

    def test_mandatory_policy_blocks_the_off_switch_with_409(self):
        admin, _, admin_token = self.as_role("ADMINISTRATOR", "notify-admin")
        self.policy(True, admin, admin_token)
        refused = self.pref(False)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "MANDATORY_EVENT"))
        self.assertEqual(self.pref(True).status_code, 200)
        self.assertEqual(self.pref(False, channel="IN_APP").status_code, 200)

    def test_malformed_input_is_refused(self):
        admin, _, admin_token = self.as_role("ADMINISTRATOR", "notify-admin")
        for body in (
            {"event_type": "Bad Event", "channel": "EMAIL", "mandatory": True},
            {"event_type": "status.changed", "channel": "SMS", "mandatory": True},
            {"event_type": "status.changed", "channel": "EMAIL", "mandatory": "yes"},
            {"event_type": "status.changed", "channel": "EMAIL"},
        ):
            with self.subTest(body=body):
                response = self.post(
                    "/api/v1/notification-policy",
                    {"operation_id": str(uuid4()), **body},
                    admin,
                    admin_token,
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(NotificationPolicy.objects.exists())

    def test_failures_show_safe_codes_to_administrators(self):
        admin, _, _ = self.as_role("ADMINISTRATOR", "notify-admin")
        self.deliver_to(self.user)
        delivery.deliver_pending(str(self.org.pk), StubSender([transient()]), base_url="")
        rows = admin.get("/api/v1/notification-failures").json()["failures"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["last_outcome"], rows[0]["last_error_code"]), ("RETRY", "TRANSIENT")
        )
        self.assertEqual(
            set(rows[0]),
            {
                "outbox_id",
                "event_type",
                "recipient_user_id",
                "attempts",
                "last_outcome",
                "last_error_code",
            },
        )
