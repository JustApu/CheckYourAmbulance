"""Physical kit administration screens over HTTP (issue #273)."""

import re
import uuid

from django.test import override_settings

from cya.checklists.models import KitInstance, Vehicle
from cya.core.models import AuditEvent
from cya.kits.commands import add_kit_instance
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
    set_kit_definition_active,
)
from cya.kits.models import (
    KitAssignmentEvent,
    KitInstanceDefinition,
)
from cya.kits.tests.support import KitCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


class KitAdminCase(KitCase):
    def setUp(self):
        super().setUp()
        self.harness = RoleHttpHarness(organization=self.org)
        self.definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **self.context()
        ).body["definition_id"]
        version = save_kit_definition_draft(
            definition_id=self.definition,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "QUANTITY",
                    "expected_quantity": 2,
                    "allowed_outcomes": ["PASSED", "FAILED"],
                }
            ],
            **self.context(),
        ).body["version_id"]
        publish_kit_definition_version(version_id=version, **self.context())

    def csrf(self, client, url):
        page = client.get(url)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def post(self, client, url, **fields):
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return client.post(url, {"csrfmiddlewaretoken": self.csrf(client, url), **fields})


@override_settings(**real_collaborator_settings())
class KitListRoutesRetiredTests(KitAdminCase):
    def test_expected_count_editor_routes_are_removed(self):
        client, _ = self.harness.sign_in("supervisor")
        for url in (
            "/administration/kit-lists/",
            f"/administration/kit-lists/{self.vehicle.pk}/",
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 404)


@override_settings(**real_collaborator_settings())
class KitInstancePageTests(KitAdminCase):
    def setUp(self):
        super().setUp()
        self.client_, _ = self.harness.sign_in("officer")  # a Supply Officer may manage kits
        self.inventory_url = "/administration/kit-instances/"
        self.url = f"/administration/kit-instances/{self.vehicle.pk}/"

    def test_crew_is_refused(self):
        client, _ = self.harness.sign_in("crew")
        self.assertEqual(client.get(self.inventory_url).status_code, 403)
        self.assertEqual(client.get(self.url).status_code, 403)

    def test_a_supply_officer_creates_a_kit_in_stock_then_assigns_it(self):
        added = self.post(
            self.client_,
            self.inventory_url,
            stage="create",
            definition_id=self.definition,
            label="Synthetic jump kit 1",
        )
        self.assertEqual(added.status_code, 302)
        kit = KitInstance.objects.get(label="Synthetic jump kit 1")
        self.assertIsNone(kit.vehicle_id)
        self.assertIsNone(kit.compartment_id)
        self.assertEqual(
            str(KitInstanceDefinition.objects.get(kit=kit).definition_id), self.definition
        )
        page = self.client_.get(self.inventory_url)
        self.assertContains(page, "Synthetic jump kit 1")
        self.assertContains(page, 'aria-labelledby="create-kit-heading" class="admin-form"')

        assignment_page = self.client_.get(self.url)
        self.assertContains(assignment_page, "Assign a physical kit from stock")
        self.assertContains(assignment_page, "Synthetic jump kit 1 — Synthetic jump kit")
        assigned = self.post(
            self.client_,
            self.url,
            stage="assign",
            kit_id=str(kit.pk),
            compartment_id=str(self.cabinet.pk),
        )
        self.assertEqual(assigned.status_code, 302)
        kit.refresh_from_db()
        self.assertEqual((kit.vehicle_id, kit.compartment_id), (self.vehicle.pk, self.cabinet.pk))
        self.assertEqual(
            list(
                KitAssignmentEvent.objects.filter(kit=kit)
                .order_by("sequence")
                .values_list("active", "vehicle_id", "compartment_id")
            ),
            [(True, None, None), (True, self.vehicle.pk, self.cabinet.pk)],
        )

        returned = self.post(self.client_, self.url, stage="stock", kit_id=str(kit.pk))
        self.assertEqual(returned.status_code, 302)
        kit.refresh_from_db()
        self.assertIsNone(kit.vehicle_id)
        self.assertIsNone(kit.compartment_id)
        self.assertEqual(KitAssignmentEvent.objects.filter(kit=kit).count(), 3)

        missing = self.post(
            self.client_,
            self.inventory_url,
            stage="create",
            definition_id="",
            label="No type",
        )
        self.assertContains(missing, "choose the kit type", status_code=400)

    def test_with_no_kit_type_the_page_says_who_adds_one(self):
        # Issue #235 E1: no empty "(choose)" list; the page says what is missing and who adds it.
        set_kit_definition_active(
            definition_id=self.definition, active=False, **self.context(self.supervisor)
        )
        page = self.client_.get(self.inventory_url)
        self.assertContains(page, "No active kit types are available.")
        self.assertContains(page, "A Supervisor or Administrator adds kit types in Kit definitions")
        self.assertNotContains(page, 'href="/administration/kit-definitions/"')
        supervisor, _ = self.harness.sign_in("supervisor")
        self.assertContains(
            supervisor.get(self.inventory_url),
            '<a href="/administration/kit-definitions/">Kit definitions</a>',
        )

    def test_with_no_compartment_the_page_says_who_adds_one(self):
        bare = Vehicle.objects.create(organization=self.org, display_name="Synthetic bare")
        url = f"/administration/kit-instances/{bare.pk}/"
        add_kit_instance(
            label="Synthetic stock kit",
            definition_id=self.definition,
            **self.context(self.supervisor),
        )
        page = self.client_.get(url)
        self.assertContains(page, "This vehicle has no compartments.")
        self.assertNotContains(page, 'name="stage" value="assign"')
        self.assertNotContains(page, f'href="/administration/positions/{bare.pk}/"')
        supervisor, _ = self.harness.sign_in("supervisor")
        self.assertContains(
            supervisor.get(url),
            f'<a href="/administration/positions/{bare.pk}/">Compartments and systems</a>',
        )

    def test_only_a_canonical_uuid_v4_operation_id_is_accepted(self):
        # A v1 or non-canonical id is refused rather than rewritten into some other
        # idempotency key (PR #170 review, applied to the shared helper).
        for other in (
            "not-a-uuid",
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # version 1
            str(uuid.uuid4()).upper(),
            "{" + str(uuid.uuid4()) + "}",
        ):
            with self.subTest(operation_id=other):
                response = self.post(
                    self.client_,
                    self.inventory_url,
                    stage="create",
                    definition_id=self.definition,
                    label="Synthetic jump kit 2",
                    operation_id=other,
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(KitInstance.objects.filter(label="Synthetic jump kit 2").exists())

    def test_assignment_starts_with_a_vehicle_choice(self):
        chooser = self.client_.get("/administration/kit-instances/vehicles/")
        self.assertEqual(chooser.status_code, 200)
        self.assertContains(chooser, f'href="{self.url}"')
        self.assertContains(chooser, self.vehicle.display_name)


@override_settings(**real_collaborator_settings())
class AdministrationLandingTests(KitAdminCase):
    def links(self, sub):
        client, _ = self.harness.sign_in(sub)
        html = client.get("/administration/").content.decode()
        return set(re.findall(r'<li><a href="(/administration/[^"]+)"', html))

    def test_each_role_sees_only_the_pages_it_may_open(self):
        self.assertEqual(
            self.links("officer"),
            {"/administration/kit-instances/", "/administration/kit-instances/vehicles/"},
        )
        supervisor = self.links("supervisor")
        self.assertNotIn("/administration/kit-lists/", supervisor)
        self.assertIn("/administration/kit-instances/vehicles/", supervisor)
        self.assertIn("/administration/kit-instances/", supervisor)
        self.assertIn("/administration/positions/", supervisor)
        self.assertNotIn("/administration/roles/", supervisor)
        self.assertIn("/administration/roles/", self.links("admin"))
