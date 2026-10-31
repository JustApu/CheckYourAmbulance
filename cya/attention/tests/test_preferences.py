"""The synchronized view preference and its HTTP endpoints (issue 83 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/83), OQ-38)."""

import json
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.test import override_settings

from cya.attention import preferences
from cya.attention.models import UserPreference
from cya.attention.tests.support import AttentionCase
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


class PreferenceCommandTests(AttentionCase):
    def save(self, actor=None, view="PRIORITY", only=True, **context):
        return preferences.save_preferences(
            preferred_view=view,
            needs_attention_only=only,
            **(context or self.context(actor or self.crew)),
        )

    def read(self, user):
        return preferences.get_preferences(str(self.org.pk), user.pk)

    def test_defaults_before_anything_is_saved(self):
        self.assertEqual(
            self.read(self.crew), {"preferred_view": "NORMAL", "needs_attention_only": False}
        )

    def test_save_persists_for_that_user_only(self):
        self.save()
        self.assertEqual(
            self.read(self.crew), {"preferred_view": "PRIORITY", "needs_attention_only": True}
        )
        self.assertEqual(self.read(self.supervisor)["preferred_view"], "NORMAL")
        self.assertEqual(UserPreference.objects.count(), 1)

    def test_a_second_save_updates_the_same_row_and_audits_prior_and_new(self):
        self.save()
        self.save(view="NORMAL", only=False)
        self.assertEqual(UserPreference.objects.count(), 1)
        self.assertEqual(self.read(self.crew)["preferred_view"], "NORMAL")
        first, second = AuditEvent.objects.filter(action=preferences.COMMAND).order_by(
            "recorded_at"
        )
        self.assertIsNone(first.prior_snapshot)
        self.assertEqual(second.prior_snapshot["values"]["preferred_view"], "PRIORITY")
        self.assertEqual(second.new_snapshot["values"]["preferred_view"], "NORMAL")
        self.assertEqual(second.actor_user_id, self.crew.pk)
        self.assertEqual(second.target_id, str(self.crew.pk))

    def test_replay_returns_the_original_result_and_writes_nothing_more(self):
        context = self.context(self.crew)
        first = self.save(**context)
        receipts = CommandReceipt.objects.count()
        replay = self.save(**context)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.body, first.body)
        self.assertEqual(CommandReceipt.objects.count(), receipts)

    def test_the_same_operation_with_other_values_is_reported_as_reused(self):
        context = self.context(self.crew)
        self.save(**context)
        reused = self.save(view="NORMAL", only=False, **context)
        self.assertTrue(reused.operation_reused)
        self.assertEqual(self.read(self.crew)["preferred_view"], "PRIORITY")

    def test_malformed_values_are_refused_before_anything_is_written(self):
        for view, only in (("SIDEWAYS", True), ("NORMAL", "yes"), (None, True), ("NORMAL", 1)):
            with self.subTest(view=view, only=only), self.assertRaises(preferences.PreferenceError):
                self.save(view=view, only=only)
        self.assertEqual(UserPreference.objects.count(), 0)

    def test_an_inactive_user_is_denied(self):
        type(self.crew).objects.filter(pk=self.crew.pk).update(active=False)
        with self.assertRaises(CommandDenied):
            self.save()
        self.assertEqual(UserPreference.objects.count(), 0)


class PreferenceSchemaGuardTests(AttentionCase):
    def test_the_database_refuses_a_bad_view_a_moved_row_and_a_foreign_user(self):
        self.save_one()
        row = UserPreference.objects.get()
        for sql, params in (
            ("UPDATE user_preference SET preferred_view='SIDEWAYS'", []),
            ("UPDATE user_preference SET user_id=%s", [str(self.supervisor.pk)]),
            ("UPDATE user_preference SET id=%s", [str(uuid4())]),
        ):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(sql, params)
        row.refresh_from_db()
        self.assertEqual((row.user_id, row.preferred_view), (self.crew.pk, "PRIORITY"))

    def test_a_preference_cannot_name_a_user_of_another_organization(self):
        other = self.make_org_row()
        stranger = type(self.crew).objects.create(
            organization=other,
            idp_issuer="https://accounts.google.com",
            idp_subject="stranger",
            email="stranger@example.test",
            display_name="Stranger",
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            UserPreference.objects.create(
                organization=self.org,
                user=stranger,
                preferred_view="NORMAL",
                needs_attention_only=False,
                updated_at=self.clock.now,
            )

    def make_org_row(self):
        from cya.accounts.models import Organization

        return Organization.objects.create(name="Other Sample Agency", station_zone="UTC")

    def save_one(self):
        preferences.save_preferences(
            preferred_view="PRIORITY", needs_attention_only=True, **self.context(self.crew)
        )


@override_settings(**real_collaborator_settings())
class PreferenceApiTests(AttentionCase):
    def make_org(self):
        self.harness = RoleHttpHarness()
        return self.harness.org

    def setUp(self):
        super().setUp()
        self.client, self.user = self.harness.sign_in("crew-http")
        self.client.get("/")
        self.token = self.client.cookies["csrftoken"].value

    def post(self, body, token=True, client=None):
        headers = {"HTTP_X_CSRFTOKEN": self.token} if token else {}
        return (client or self.client).post(
            "/api/v1/me/view-preferences",
            data=json.dumps(body),
            content_type="application/json",
            **headers,
        )

    def body(self, **over):
        return {
            "operation_id": str(uuid4()),
            "preferred_view": "PRIORITY",
            "needs_attention_only": True,
            **over,
        }

    def checklist(self, query="", client=None):
        url = f"/api/v1/vehicles/{self.vehicle.pk}/checklist{query}"
        return (client or self.client).get(url)

    def test_get_returns_defaults_then_the_saved_values(self):
        first = self.client.get("/api/v1/me/view-preferences").json()
        self.assertEqual(
            (first["preferred_view"], first["needs_attention_only"]), ("NORMAL", False)
        )
        response = self.post(self.body())
        self.assertEqual(
            (response.status_code, response.json()["result"]["preferred_view"]), (200, "PRIORITY")
        )
        second = self.client.get("/api/v1/me/view-preferences").json()
        self.assertEqual(
            (second["preferred_view"], second["needs_attention_only"]), ("PRIORITY", True)
        )

    def test_the_preference_follows_the_user_to_a_second_device(self):
        self.post(self.body())
        other_device, _user = self.harness.sign_in("crew-http")
        saved = other_device.get("/api/v1/me/view-preferences").json()
        self.assertEqual(
            (saved["preferred_view"], saved["needs_attention_only"]), ("PRIORITY", True)
        )

    def test_the_checklist_defaults_from_the_preference_and_the_query_overrides_it(self):
        self.failed(self.siren)
        self.post(self.body())
        saved = self.checklist().json()
        self.assertEqual((saved["view"], saved["needs_attention_only"]), ("PRIORITY", True))
        self.assertEqual([i["item_id"] for i in saved["items"]], [str(self.siren.pk)])
        self.assertEqual(saved["items"][0]["item_attention"], "ATTENTION")
        wide = self.checklist("?view=NORMAL&needs_attention_only=false").json()
        self.assertEqual((wide["view"], wide["needs_attention_only"]), ("NORMAL", False))
        self.assertGreater(len(wide["items"]), 1)
        self.assertEqual(wide["vehicle_status"]["status"], "IN_SERVICE")

    def test_bad_requests(self):
        for body in (
            self.body(preferred_view="SIDEWAYS"),
            self.body(needs_attention_only="yes"),
            self.body(operation_id="nope"),
            self.body(operation_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8"),  # canonical, not v4
            self.body(operation_id="6ba7b811-9dad-51d1-80b4-00c04fd430c8"),  # canonical, v5
            {"preferred_view": "NORMAL"},
            self.body(extra=1),
        ):
            self.assertEqual(self.post(body).status_code, 400, body)
        self.assertEqual(UserPreference.objects.count(), 0)
        for query in ("?view=SIDEWAYS", "?needs_attention_only=maybe", "?x=1", "?as_of=2026-01-01"):
            self.assertEqual(self.checklist(query).status_code, 400, query)

    def test_unauthenticated_and_csrf_and_unknown_vehicle(self):
        from django.test import Client

        anonymous = Client()
        self.assertEqual(anonymous.get("/api/v1/me/view-preferences").status_code, 401)
        self.assertEqual(self.checklist(client=anonymous).status_code, 401)
        self.assertEqual(self.post(self.body(), token=False).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/vehicles/{uuid4()}/checklist").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/vehicles/not-a-uuid/checklist").status_code, 404)
