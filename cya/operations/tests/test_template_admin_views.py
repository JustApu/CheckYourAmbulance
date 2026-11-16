"""Checklist template screens over HTTP (SPEC CHK-2, CHK-3, CFG-3; issue #130 part 6a)."""

import re
import uuid
from unittest import mock

from django.test import override_settings

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
    Compartment,
    Item,
)
from cya.core.models import AuditEvent
from cya.kits.tests.support import KitCase
from cya.operations import template_admin_views
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

LIST = "/administration/checklists/"


@override_settings(**real_collaborator_settings())
class TemplatePageTests(KitCase):
    def setUp(self):
        super().setUp()
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("supervisor")
        self.url = f"{LIST}{self.template.pk}/"

    def csrf(self, client, url):
        page = client.get(url)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def post(self, url, client=None, **fields):
        client = client or self.client_
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return client.post(url, {"csrfmiddlewaretoken": self.csrf(client, url), **fields})

    def item_fields(self, **extra):
        return {
            "base_revision": "0",
            "place": f"COMPARTMENT:{self.cabinet.pk}",
            "kind": "QUANTITY",
            "expected_quantity": "4",
            "instructions": "Synthetic.",
            "outcome-FAILED": "on",
            **extra,
        }

    def versions(self):
        return ChecklistVersion.objects.filter(template=self.template).order_by("revision")

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        for sub in ("crew", "officer"):
            client, user = self.harness.sign_in(sub)
            with self.subTest(role=sub):
                self.assertEqual(client.get(LIST).status_code, 403)
                self.assertEqual(client.get(self.url).status_code, 403)
                self.assertEqual(client.get(f"{self.url}items/new/").status_code, 403)
                self.assertTrue(
                    AuditEvent.objects.filter(actor_user=user, outcome="DENIED").exists()
                )

    def test_the_administration_page_links_to_the_templates(self):
        page = self.client_.get("/administration/")
        self.assertContains(page, f'href="{LIST}"')
        crew, _ = self.harness.sign_in("crew")
        self.assertNotContains(crew.get("/administration/"), f'href="{LIST}"', status_code=403)

    def test_create_a_template_and_land_on_it(self):
        created = self.post(LIST, stage="create", label="Synthetic weekly", key="weekly")
        self.assertEqual(created.status_code, 302)
        template = ChecklistTemplate.objects.get(key="weekly")
        self.assertIn(str(template.pk), created["Location"])
        page = self.client_.get(created["Location"])
        self.assertContains(page, "Template created.")
        self.assertContains(page, "This template has no versions yet.")
        self.assertContains(self.client_.get(LIST), "Synthetic weekly")
        taken = self.post(LIST, stage="create", label="Again", key="weekly")
        self.assertContains(taken, "another template has that key", status_code=400)
        self.assertContains(taken, 'id="create-key-error" class="field-error"', status_code=400)
        self.assertContains(taken, 'value="Again"', status_code=400)

    def test_add_a_new_item_and_an_existing_one_then_edit_and_remove(self):
        added = self.post(
            f"{self.url}items/new/", new_item="Synthetic splint", **self.item_fields()
        )
        self.assertEqual(added.status_code, 302)
        splint = Item.objects.get(label="Synthetic splint")
        self.post(
            f"{self.url}items/new/",
            item_id=str(self.masks.pk),
            **self.item_fields(base_revision="1", kind="PRESENCE_CONDITION", expected_quantity=""),
        )
        latest = self.versions().last()
        self.assertEqual(latest.revision, 2)
        self.assertEqual(
            sorted(
                ChecklistItemDef.objects.filter(checklist_version=latest).values_list(
                    "item__label", "expected_quantity"
                )
            ),
            [("Synthetic masks", None), ("Synthetic splint", 4)],
        )
        page = self.client_.get(self.url)
        self.assertContains(page, "Draft revision 2")
        self.assertContains(page, "Synthetic splint")
        edit_url = f"{self.url}items/{splint.pk}/"
        self.assertContains(self.client_.get(edit_url), 'value="4"')
        self.post(edit_url, **self.item_fields(base_revision="2", expected_quantity="6"))
        latest = self.versions().last()
        self.assertEqual(
            ChecklistItemDef.objects.get(checklist_version=latest, item=splint).expected_quantity, 6
        )
        removed = self.post(self.url, stage="remove", item_id=str(self.masks.pk), base_revision="3")
        self.assertEqual(removed.status_code, 302)
        self.assertEqual(
            list(
                ChecklistItemDef.objects.filter(
                    checklist_version=self.versions().last()
                ).values_list("item_id", flat=True)
            ),
            [splint.pk],
        )
        last = self.post(self.url, stage="remove", item_id=str(splint.pk), base_revision="4")
        self.assertContains(last, "a version needs at least one item", status_code=400)

    def test_a_stale_edit_is_refused_and_keeps_what_was_typed(self):
        self.post(f"{self.url}items/new/", new_item="Synthetic splint", **self.item_fields())
        stale = self.post(
            f"{self.url}items/new/", new_item="Synthetic collar", **self.item_fields()
        )
        self.assertContains(stale, "changed since this edit started", status_code=400)
        self.assertContains(stale, 'value="Synthetic collar"', status_code=400)
        self.assertFalse(Item.objects.filter(label="Synthetic collar").exists())

    def test_form_mistakes_are_refused_before_any_command(self):
        both = self.post(
            f"{self.url}items/new/",
            new_item="Synthetic collar",
            item_id=str(self.masks.pk),
            **self.item_fields(),
        )
        self.assertContains(both, "not both", status_code=400)
        nowhere = self.post(
            f"{self.url}items/new/", new_item="Synthetic collar", **self.item_fields(place="")
        )
        self.assertContains(nowhere, "choose where the item is", status_code=400)
        self.assertContains(nowhere, 'id="place-error" class="field-error"', status_code=400)
        bad_count = self.post(
            f"{self.url}items/new/",
            new_item="Synthetic collar",
            **self.item_fields(expected_quantity="four"),
        )
        self.assertContains(bad_count, "quantity must be a whole number", status_code=400)
        self.assertContains(bad_count, 'id="quantity-error" class="field-error"', status_code=400)
        self.assertContains(
            bad_count, 'aria-invalid="true" aria-describedby="quantity-error"', status_code=400
        )
        forged = self.post(
            f"{self.url}items/new/",
            new_item="Synthetic collar",
            operation_id="not-a-uuid",
            **self.item_fields(),
        )
        self.assertEqual(forged.status_code, 400)
        self.assertFalse(self.versions().exists())

    def test_preview_then_publish(self):
        self.post(f"{self.url}items/new/", new_item="Synthetic splint", **self.item_fields())
        draft = self.versions().last()
        preview = self.post(self.url, stage="preview", version_id=str(draft.pk), when="now")
        self.assertContains(preview, "Before you publish revision 1")
        self.assertContains(preview, "Added: Synthetic splint")
        self.assertContains(preview, 'name="stage" value="publish"')
        self.assertFalse(ChecklistPublication.objects.exists())
        published = self.post(self.url, stage="publish", version_id=str(draft.pk), when="now")
        self.assertEqual(published.status_code, 302)
        self.assertTrue(ChecklistPublication.objects.filter(checklist_version=draft).exists())
        page = self.client_.get(self.url)
        self.assertContains(page, "Revision 1, in force since")
        self.assertContains(page, "Every version is published.")

    def test_publishing_needs_an_explicit_time_and_this_templates_draft(self):
        self.post(f"{self.url}items/new/", new_item="Synthetic splint", **self.item_fields())
        draft = self.versions().last()
        vague = self.post(self.url, stage="publish", version_id=str(draft.pk), when="")
        self.assertContains(vague, "choose when it takes effect", status_code=400)
        other = ChecklistTemplate.objects.create(organization=self.org, label="Other")
        wrong = self.post(
            f"{LIST}{other.pk}/", stage="publish", version_id=str(draft.pk), when="now"
        )
        self.assertContains(wrong, "choose this template&#x27;s draft", status_code=400)
        self.assertFalse(ChecklistPublication.objects.exists())

    def test_duplicate_a_template(self):
        self.post(f"{self.url}items/new/", new_item="Synthetic splint", **self.item_fields())
        draft = self.versions().last()
        url = f"{self.url}duplicate/"
        self.assertContains(self.client_.get(url), "Revision 1 (draft)")
        copied = self.post(url, version_id=str(draft.pk), label="Synthetic copy", key="copy")
        self.assertEqual(copied.status_code, 302)
        copy = ChecklistTemplate.objects.get(key="copy")
        self.assertIn(str(copy.pk), copied["Location"])
        self.assertEqual(
            ChecklistItemDef.objects.filter(checklist_version__template=copy).count(), 1
        )
        missing = self.post(url, version_id=str(uuid.uuid4()), label="X", key="x")
        self.assertContains(missing, "choose a version of this template", status_code=400)

    def test_another_organizations_template_is_not_found(self):
        self.assertEqual(self.client_.get(f"{LIST}{uuid.uuid4()}/").status_code, 404)

    def test_version_history_is_paged(self):
        for _ in range(5):
            self.draft()
        with mock.patch.object(template_admin_views, "VERSION_PAGE", 3):
            first = self.client_.get(self.url).content.decode()
            history = first.split('id="history-heading"')[1]
            self.assertEqual(re.findall(r"<tr><td>(\d+)</td>", history), ["5", "4", "3"])
            self.assertIn("?before=3", history)
            older = self.client_.get(f"{self.url}?before=3").content.decode()
            history = older.split('id="history-heading"')[1]
            self.assertEqual(re.findall(r"<tr><td>(\d+)</td>", history), ["2", "1"])
            self.assertIn("Newest versions", history)
            self.assertNotIn("Older versions", history)

    def test_an_older_revision_is_duplicated_by_its_number(self):
        self.draft(self.base_items()[:1])
        self.draft()
        self.draft()
        url = f"{self.url}duplicate/"
        with mock.patch.object(template_admin_views, "VERSION_PAGE", 2):
            page = self.client_.get(url)
            self.assertContains(page, "Or an older revision number")
            self.assertNotContains(page, ">Revision 1 (draft)<")
            copied = self.post(url, revision="1", label="Synthetic old copy", key="old-copy")
        self.assertEqual(copied.status_code, 302)
        copy = ChecklistTemplate.objects.get(key="old-copy")
        self.assertEqual(
            ChecklistItemDef.objects.filter(checklist_version__template=copy).count(), 1
        )
        bad = self.post(url, version_id="not-an-id", label="X", key="x")
        self.assertContains(bad, "choose a version of this template", status_code=400)
        unknown = self.post(url, revision="99", label="X", key="x")
        self.assertContains(unknown, "choose a version of this template", status_code=400)

    def test_existing_items_are_capped_and_found_by_name(self):
        Item.objects.create(organization=self.org, label="Synthetic splint")
        url = f"{self.url}items/new/"
        with mock.patch.object(template_admin_views, "ITEM_CHOICES", 1):
            capped = self.client_.get(url)
            self.assertContains(capped, "Showing the first 1 matching items")
            found = self.client_.get(f"{url}?q=splint")
        self.assertContains(found, ">Synthetic splint</option>")
        self.assertNotContains(found, ">Synthetic masks</option>")
        self.assertNotContains(found, "Showing the first")

    def test_places_and_kits_are_capped_and_found_by_name_keeping_the_current_one(self):
        self.post(f"{self.url}items/new/", new_item="Synthetic splint", **self.item_fields())
        splint = Item.objects.get(label="Synthetic splint")
        url = f"{self.url}items/{splint.pk}/"
        self.make_slot(Compartment, "drawer")  # two compartments, so a cap of 1 hides one
        with mock.patch.object(template_admin_views, "CHOICE_LIMIT", 1):
            page = self.client_.get(url).content.decode()
            self.assertIn("Showing the first 1 matching places or kits", page)
            # The cabinet the item is in stays on offer even when the search excludes it.
            narrowed = self.client_.get(f"{url}?p=lights").content.decode()
        self.assertIn("System: Synthetic lights", narrowed)
        self.assertIn("Compartment: Synthetic cabinet", narrowed)
        self.assertNotIn("Showing the first", self.client_.get(f"{url}?p=lights").content.decode())
