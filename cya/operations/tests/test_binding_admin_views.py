"""Checklist binding screens over HTTP (SPEC 4.8 CFG-7, CFG-8, AC-37; issue #177).

AttentionCase binds the template to vehicle A on slot test-0600 at 2026-06-01 00:00 UTC and
generates the 06:00 inspections of 06-01 and 06-02 (windows open at 05:00).
"""

import re
import uuid

from django.test import override_settings

from cya.attention.tests.support import AttentionCase
from cya.core.models import AuditEvent
from cya.obligations.models import RequirementBinding
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


@override_settings(**real_collaborator_settings())
class BindingPagesTests(AttentionCase):
    def setUp(self):
        super().setUp()
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("supervisor")
        self.url = f"/administration/vehicles/{self.vehicle.pk}/bindings/"
        self.other_url = f"/administration/vehicles/{self.vehicles[1].pk}/bindings/"

    def csrf(self, client, url):
        page = client.get(url)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def post(self, url=None, client=None, **fields):
        url = url or self.url
        client = client or self.client_
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return client.post(url, {"csrfmiddlewaretoken": self.csrf(client, url), **fields})

    def hidden(self, response, name):
        return re.search(rf'name="{name}" value="([^"]*)"', response.content.decode()).group(1)

    def preview(self, **fields):
        return self.post(
            stage="unbind-preview",
            template_id=str(self.template.pk),
            slot="test-0600",
            **fields,
        )

    def confirm(self, preview, **fields):
        return self.post(
            stage="unbind",
            template_id=str(self.template.pk),
            slot="test-0600",
            token=self.hidden(preview, "token"),
            operation_id=self.hidden_after(preview, "unbind"),
            **fields,
        )

    def hidden_after(self, response, stage):
        """The operation id of the form whose stage is `stage`."""
        html = response.content.decode()
        form = html[html.index(f'name="stage" value="{stage}"') :]
        return re.search(r'name="operation_id" value="([^"]+)"', form).group(1)

    def actions(self, vehicle=None):
        return list(
            RequirementBinding.objects.filter(vehicle=vehicle or self.vehicle)
            .order_by("revision")
            .values_list("action", flat=True)
        )

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        for sub in ("crew", "officer"):
            client, user = self.harness.sign_in(sub)
            with self.subTest(role=sub):
                self.assertEqual(client.get(self.url).status_code, 403)
                self.assertTrue(
                    AuditEvent.objects.filter(actor_user=user, outcome="DENIED").exists()
                )
                # A direct post with a valid CSRF token is refused too (AC-37(1)).
                client.get("/")
                response = client.post(
                    self.other_url,
                    {
                        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
                        "stage": "bind",
                        "template_id": str(self.template.pk),
                        "slot": "test-0600",
                        "when": "now",
                        "operation_id": str(uuid.uuid4()),
                    },
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.actions(self.vehicles[1]), [])

    def test_the_vehicle_page_links_to_bindings_and_lists_them(self):
        vehicle_page = self.client_.get(f"/administration/vehicles/{self.vehicle.pk}/")
        self.assertContains(vehicle_page, f'href="{self.url}"')
        page = self.client_.get(self.url)
        self.assertContains(page, self.template.label)
        self.assertContains(page, "Bound since 2026-06-01")
        self.assertContains(page, "Unbind…")
        self.assertContains(page, 'class="admin-form"', count=1)

    def test_binding_now_and_at_a_local_time(self):
        response = self.post(
            self.other_url,
            stage="bind",
            template_id=str(self.template.pk),
            slot="test-0600",
            when="at",
            at="2026-06-03T12:00",
        )
        self.assertEqual(response.status_code, 302)
        binding = RequirementBinding.objects.get(vehicle=self.vehicles[1])
        self.assertEqual(binding.effective_at, self.at(days=2, hours=12))  # station zone UTC
        self.assertEqual(binding.actor_user_id, str(self.supervisor.pk))
        page = self.client_.get(self.other_url)
        self.assertContains(page, "Starts 2026-06-03 12:00")
        refused = self.post(
            stage="bind", template_id=str(self.template.pk), slot="test-0600", when="now"
        )
        self.assertContains(refused, "already bound", status_code=400)
        missing = self.post(self.other_url, stage="bind", template_id="", when="now")
        self.assertContains(missing, "choose a checklist template", status_code=400)

    def test_an_inactive_vehicle_can_be_bound(self):
        other = self.vehicles[1]
        other.active = False
        other.save(update_fields=["active"])
        page = self.client_.get(self.other_url)
        self.assertContains(page, 'id="inactive-note"')
        response = self.post(
            self.other_url,
            stage="bind",
            template_id=str(self.template.pk),
            slot="test-0600",
            when="now",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.actions(other), ["BIND"])

    def test_unbinding_previews_then_confirms(self):
        self.clock.set(self.at(hours=5, minutes=30))
        preview = self.preview()
        self.assertEqual(preview.status_code, 200)
        html = preview.content.decode()
        superseded = html[html.index('id="superseded"') : html.index('id="continuing-heading"')]
        continuing = html[html.index('id="continuing"') :]
        self.assertIn("2026-06-02 06:00", superseded)
        self.assertIn("2026-06-01 06:00", continuing[: continuing.index("</table>")])
        self.assertIn("does not complete any inspection", html)
        self.assertIn("Checks, deficiencies and audit history are kept", html)
        self.assertEqual(self.actions(), ["BIND"], "a preview changes nothing")
        done = self.confirm(preview)
        self.assertEqual(done.status_code, 302)
        self.assertEqual(self.actions(), ["BIND", "UNBIND"])
        self.assertContains(self.client_.get(done["Location"]), "Template unbound.")

    def test_a_stale_preview_is_refused_with_a_fresh_one(self):
        self.clock.set(self.at(hours=4))
        preview = self.preview()
        self.clock.set(self.at(hours=5, minutes=30))  # the 06-01 window opened since
        refused = self.confirm(preview)
        self.assertContains(refused, "changed since the preview", status_code=409)
        self.assertEqual(self.actions(), ["BIND"])
        self.assertNotEqual(self.hidden(refused, "token"), self.hidden(preview, "token"))
        self.assertEqual(self.confirm(refused).status_code, 302)
        self.assertEqual(self.actions(), ["BIND", "UNBIND"])

    def test_a_resubmitted_confirmation_replays(self):
        self.clock.set(self.at(hours=4))
        preview = self.preview()
        self.assertEqual(self.confirm(preview).status_code, 302)
        self.assertEqual(self.confirm(preview).status_code, 302)
        self.assertEqual(self.actions(), ["BIND", "UNBIND"])

    def test_unbinding_an_unbound_template_is_refused(self):
        other = self.post(
            self.other_url,
            stage="unbind-preview",
            template_id=str(self.template.pk),
            slot="test-0600",
        )
        self.assertContains(other, "not bound to this vehicle", status_code=400)
        forged = self.post(
            self.other_url,
            stage="unbind",
            template_id=str(self.template.pk),
            slot="test-0600",
            token="0" * 64,
        )
        self.assertContains(forged, "already unbound", status_code=400)
        bad = self.post(stage="unbind", template_id=str(self.template.pk), slot="test-0600")
        self.assertContains(bad, "malformed", status_code=400)
        self.assertEqual(self.actions(), ["BIND"])
