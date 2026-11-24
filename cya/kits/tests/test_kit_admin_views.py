"""Kit list and kit instance screens over HTTP (SPEC KDEF-3, KDEF-9, CFG-5; issue #130 part 5c)."""

import re
import uuid
from datetime import datetime
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from cya.checklists.models import KitInstance, Vehicle
from cya.core.models import AuditEvent
from cya.kits import kit_list_views
from cya.kits.commands import add_kit_instance
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
    set_kit_definition_active,
)
from cya.kits.kit_list_views import _effective_at
from cya.kits.kit_lists import KitListError
from cya.kits.models import (
    KitAssignmentEvent,
    KitInstanceDefinition,
    VehicleKitListPublication,
    VehicleKitListVersion,
    VehicleKitRequirement,
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
class KitListPageTests(KitAdminCase):
    def setUp(self):
        super().setUp()
        self.client_, _ = self.harness.sign_in("supervisor")
        self.url = f"/administration/kit-lists/{self.vehicle.pk}/"

    def line(self, index, count="2", definition=None, compartment=None, **extra):
        return {
            f"line-{index}-definition": definition or self.definition,
            f"line-{index}-compartment": compartment or str(self.cabinet.pk),
            f"line-{index}-count": count,
            f"line-{index}-order": "1",
            **extra,
        }

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        for sub in ("crew", "officer"):
            client, user = self.harness.sign_in(sub)
            with self.subTest(role=sub):
                self.assertEqual(client.get("/administration/kit-lists/").status_code, 403)
                self.assertEqual(client.get(self.url).status_code, 403)
                self.assertTrue(
                    AuditEvent.objects.filter(actor_user=user, outcome="DENIED").exists()
                )

    def test_save_a_draft_then_publish_it_now(self):
        saved = self.post(self.client_, self.url, stage="draft", rows="1", **self.line(0))
        self.assertEqual(saved.status_code, 302)
        draft = VehicleKitListVersion.objects.get(vehicle=self.vehicle)
        self.assertEqual(
            list(
                VehicleKitRequirement.objects.filter(version=draft).values_list(
                    "definition_id", "compartment_id", "required_count"
                )
            ),
            [(self.definition, str(self.cabinet.pk), 2)],
        )
        page = self.client_.get(saved["Location"])
        self.assertContains(page, "Draft saved.")
        self.assertContains(page, 'name="stage" value="publish"')
        published = self.post(
            self.client_, self.url, stage="publish", version_id=str(draft.pk), when="now"
        )
        self.assertEqual(published.status_code, 302)
        self.assertTrue(VehicleKitListPublication.objects.filter(version=draft).exists())
        self.assertContains(self.client_.get(self.url), "Revision 1.")

    def test_bad_lines_keep_what_was_typed_and_removed_lines_are_dropped(self):
        bad = self.post(
            self.client_, self.url, stage="draft", rows="1", **self.line(0, count="two")
        )
        self.assertContains(bad, "line 1 count must be a whole number", status_code=400)
        self.assertContains(bad, 'value="two"', status_code=400)
        self.assertFalse(VehicleKitListVersion.objects.exists())
        self.post(
            self.client_,
            self.url,
            stage="draft",
            rows="2",
            **self.line(0),
            **self.line(1, compartment=str(self.cabinet.pk), **{"line-1-remove": "on"}),
        )
        draft = VehicleKitListVersion.objects.get(vehicle=self.vehicle)
        self.assertEqual(VehicleKitRequirement.objects.filter(version=draft).count(), 1)

    def test_a_half_filled_row_is_refused_and_an_inactive_kit_type_is_explained(self):
        half = self.post(
            self.client_,
            self.url,
            stage="draft",
            rows="2",
            **self.line(0),
            **{"line-1-definition": "", "line-1-compartment": str(self.cabinet.pk)},
        )
        self.assertContains(half, "line 2: choose both a kit and a compartment", status_code=400)
        self.assertFalse(VehicleKitListVersion.objects.exists())
        self.post(self.client_, self.url, stage="draft", rows="1", **self.line(0))
        set_kit_definition_active(
            definition_id=self.definition, active=False, **self.context(self.supervisor)
        )
        page = self.client_.get(self.url)
        self.assertContains(page, 'aria-describedby="note-0"')
        # Issue #176: the line editor uses the admin field style.
        self.assertContains(page, 'aria-labelledby="edit-heading" class="admin-form"')
        self.assertContains(
            page,
            "Synthetic jump kit is no longer an active kit type: choose again or remove this line.",
        )
        self.assertNotContains(page, f'value="{self.definition}" selected')

    def test_a_malformed_or_partial_editor_post_never_replaces_the_draft(self):
        # The draft is replaced by exactly the rows posted, so a bad row count or a missing row
        # refuses the save rather than saving part of the list (PR #172 review).
        self.post(self.client_, self.url, stage="draft", rows="1", **self.line(0))
        draft = VehicleKitListVersion.objects.get(vehicle=self.vehicle)
        for rows in (None, "", "two", "-1", str(kit_list_views.MAX_ROWS + 1), "2"):
            fields = {**self.line(0)} if rows is None else {"rows": rows, **self.line(0)}
            with self.subTest(rows=rows):
                refused = self.post(self.client_, self.url, stage="draft", **fields)
                self.assertContains(refused, "the form was not valid", status_code=400)
        self.assertEqual(list(VehicleKitListVersion.objects.filter(vehicle=self.vehicle)), [draft])

    def test_a_list_longer_than_the_editor_is_shown_but_not_offered_for_editing(self):
        self.post(self.client_, self.url, stage="draft", rows="1", **self.line(0))
        with mock.patch.object(kit_list_views, "MAX_ROWS", 0):
            page = self.client_.get(self.url)
        self.assertContains(page, "This list has 1 line. This page can edit at most 0 lines")
        self.assertNotContains(page, 'name="stage" value="draft"')
        self.assertContains(page, 'name="stage" value="publish"')  # it can still be published

    def test_publishing_needs_an_explicit_choice_of_when(self):
        self.post(self.client_, self.url, stage="draft", rows="1", **self.line(0))
        draft = VehicleKitListVersion.objects.get(vehicle=self.vehicle)
        for when in (None, "later"):
            fields = {} if when is None else {"when": when}
            with self.subTest(when=when):
                refused = self.post(
                    self.client_, self.url, stage="publish", version_id=str(draft.pk), **fields
                )
                self.assertContains(refused, "choose when it takes effect", status_code=400)
        self.assertFalse(VehicleKitListPublication.objects.exists())

    def test_a_publication_cannot_be_backdated_or_name_another_vehicle_draft(self):
        self.post(self.client_, self.url, stage="draft", rows="1", **self.line(0))
        draft = VehicleKitListVersion.objects.get(vehicle=self.vehicle)
        past = self.post(
            self.client_,
            self.url,
            stage="publish",
            version_id=str(draft.pk),
            when="at",
            at="2000-01-01T06:00",
        )
        self.assertEqual(past.status_code, 400)
        other = self.post(
            self.client_,
            f"/administration/kit-lists/{self.vehicle_b.pk}/",
            stage="publish",
            version_id=str(draft.pk),
            when="now",
        )
        self.assertContains(other, "draft to publish", status_code=400)
        self.assertFalse(VehicleKitListPublication.objects.exists())


class EffectiveAtTests(SimpleTestCase):
    def test_station_local_times_and_clock_changes(self):
        with timezone.override("America/New_York"):
            self.assertIsNone(_effective_at({"when": "now"}))
            aware = _effective_at({"when": "at", "at": "2026-07-01T06:00"})
            self.assertEqual(_effective_at({"when": "at", "at": "2026-07-01T06:00:30"}).second, 30)
            self.assertEqual(aware.utcoffset().total_seconds(), -4 * 3600)
            with self.assertRaises(KitListError):  # skipped by the spring-forward change
                _effective_at({"when": "at", "at": "2026-03-08T02:30"})
            repeated = _effective_at({"when": "at", "at": "2026-11-01T01:30"})
            self.assertEqual(repeated.fold, 0)  # the first of the repeated hour
            for bad in (
                "",
                "tomorrow",
                "2026-07-01T06:00+02:00",
                "2026-07-01",  # a date alone would parse as midnight (PR #172 review)
                "2026-07-01 06:00",
                "2026-07-01T06",
                "2026-07-01T06:00:00.5",
            ):
                with self.subTest(at=bad), self.assertRaises(KitListError):
                    _effective_at({"when": "at", "at": bad})
            # Only an explicit "now" publishes now (PR #172 review).
            for when in (None, "", "later", "NOW"):
                with self.subTest(when=when), self.assertRaises(KitListError):
                    _effective_at({} if when is None else {"when": when})
        self.assertIsInstance(aware, datetime)


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
        self.assertEqual(self.links("officer"), {"/administration/kit-instances/"})
        supervisor = self.links("supervisor")
        self.assertIn("/administration/kit-lists/", supervisor)
        self.assertIn("/administration/kit-instances/", supervisor)
        self.assertIn("/administration/positions/", supervisor)
        self.assertNotIn("/administration/roles/", supervisor)
        self.assertIn("/administration/roles/", self.links("admin"))
