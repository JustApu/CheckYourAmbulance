"""Compartments and systems screens over HTTP (SPEC 4.8 CFG-2, CFG-6; issue #130 part 5b)."""

import re
import uuid

from django.test import override_settings

from cya.checklists.models import Compartment
from cya.core.models import AuditEvent
from cya.kits.models import PositionKey
from cya.kits.tests.support import KitCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

LIST = "/administration/positions/"


@override_settings(**real_collaborator_settings())
class PositionPagesTests(KitCase):
    def setUp(self):
        super().setUp()
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("supervisor")
        self.url = f"{LIST}{self.vehicle.pk}/"

    def csrf(self, client=None):
        page = (client or self.client_).get(self.url)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def post(self, client=None, **fields):
        client = client or self.client_
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return client.post(self.url, {"csrfmiddlewaretoken": self.csrf(client), **fields})

    def rows(self, slot=None):
        return list(
            Compartment.objects.filter(
                vehicle=self.vehicle, stable_slot_id=slot or self.cabinet.stable_slot_id
            )
            .order_by("revision")
            .values_list("revision", "label", "display_order", "retired")
        )

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        for sub in ("crew", "officer"):
            client, user = self.harness.sign_in(sub)
            with self.subTest(role=sub):
                self.assertEqual(client.get(LIST).status_code, 403)
                self.assertEqual(client.get(self.url).status_code, 403)
                self.assertTrue(
                    AuditEvent.objects.filter(actor_user=user, outcome="DENIED").exists()
                )

    def test_supervisors_reach_positions_from_administration_but_not_roles(self):
        landing = self.client_.get("/administration/")
        self.assertEqual(landing.status_code, 200)
        self.assertContains(landing, f'href="{LIST}"')
        self.assertNotContains(landing, 'href="/administration/roles/"')
        self.assertEqual(self.client_.get("/administration/roles/").status_code, 403)
        listing = self.client_.get(LIST)
        self.assertContains(listing, f'href="{self.url}"')
        page = self.client_.get(self.url)
        self.assertContains(page, self.cabinet.label)
        self.assertContains(page, 'name="stage" value="add"')
        admin, _ = self.harness.sign_in("admin")
        self.assertContains(admin.get("/administration/"), 'href="/administration/roles/"')

    def test_an_unknown_vehicle_is_not_found(self):
        self.assertEqual(self.client_.get(f"{LIST}{uuid.uuid4()}/").status_code, 404)

    def test_add_then_a_resubmitted_form_replays_instead_of_adding_twice(self):
        operation = str(uuid.uuid4())
        fields = {
            "stage": "add",
            "kind": "COMPARTMENT",
            "label": "Synthetic drawer",
            "key": "drawer",
            "display_order": "3",
            "operation_id": operation,
        }
        first = self.post(**fields)
        self.assertEqual(first.status_code, 302)
        self.assertIn("saved=add", first["Location"])
        slot = PositionKey.objects.get(key="drawer").stable_slot_id
        self.assertEqual(self.rows(slot), [(1, "Synthetic drawer", 3, False)])
        self.assertEqual(self.post(**fields).status_code, 302)
        self.assertEqual(len(self.rows(slot)), 1)
        self.assertContains(self.client_.get(first["Location"]), "Position added.")

    def test_bad_input_is_shown_with_the_form_kept(self):
        response = self.post(
            stage="add",
            kind="COMPARTMENT",
            label="Synthetic drawer",
            key="Not A Key",
            display_order="",
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Nothing was changed.", status_code=400)
        self.assertContains(response, 'value="Not A Key"', status_code=400)
        response = self.post(
            stage="add",
            kind="COMPARTMENT",
            label="Synthetic drawer",
            key="drawer",
            display_order="two",
        )
        self.assertContains(response, "order must be a whole number", status_code=400)
        self.assertFalse(PositionKey.objects.filter(key="drawer").exists())
        self.assertEqual(self.post(stage="unknown").status_code, 400)
        self.assertEqual(self.post(stage="add", operation_id="not-a-uuid").status_code, 400)
        # Only an exact canonical UUID v4 is accepted; a v1 or non-canonical form is refused
        # rather than rewritten into some other idempotency key (PR #170 review).
        for other in (
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # version 1
            str(uuid.uuid4()).upper(),
            "{" + str(uuid.uuid4()) + "}",
        ):
            with self.subTest(operation_id=other):
                response = self.post(
                    stage="add",
                    kind="COMPARTMENT",
                    label="Synthetic drawer",
                    key="drawer",
                    display_order="",
                    operation_id=other,
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(PositionKey.objects.filter(key="drawer").exists())

    def test_rename_and_reorder_append_a_revision(self):
        slot = str(self.cabinet.stable_slot_id)
        response = self.post(
            stage="revise",
            kind="COMPARTMENT",
            slot=slot,
            label="Renamed cabinet",
            display_order="2",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows()[-1], (2, "Renamed cabinet", 2, False))
        unchanged = self.post(
            stage="revise",
            kind="COMPARTMENT",
            slot=slot,
            label="Renamed cabinet",
            display_order="2",
        )
        self.assertContains(unchanged, "already has those values", status_code=400)

    def test_retiring_shows_blockers_or_asks_to_confirm(self):
        self.publish_schedule("test-0600")
        self.published_version()
        self.bind()
        slot = str(self.cabinet.stable_slot_id)
        blocked = self.post(stage="retire-preview", kind="COMPARTMENT", slot=slot)
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, "cannot be retired")
        self.assertNotContains(blocked, 'name="stage" value="retire"')
        self.bind(action="UNBIND")
        preview = self.post(stage="retire-preview", kind="COMPARTMENT", slot=slot)
        self.assertContains(preview, 'name="stage" value="retire"')
        operation = re.search(
            r'name="stage" value="retire">.*?name="operation_id" value="([^"]+)"',
            preview.content.decode(),
            re.S,
        ).group(1)
        done = self.post(stage="retire", kind="COMPARTMENT", slot=slot, operation_id=operation)
        self.assertEqual(done.status_code, 302)
        self.assertTrue(self.rows()[-1][3], "retired")
        page = self.client_.get(self.url)
        self.assertNotContains(page, f'aria-label="Retire {self.cabinet.label}"')
