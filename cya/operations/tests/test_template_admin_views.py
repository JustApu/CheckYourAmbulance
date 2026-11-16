"""Checklist template screens over HTTP (SPEC CHK-2, CHK-3, CFG-3; issue #130 part 6a)."""

import re
import uuid

from django.test import override_settings

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
    Item,
)
from cya.core.models import AuditEvent
from cya.kits.tests.support import KitCase
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
        bad_count = self.post(
            f"{self.url}items/new/",
            new_item="Synthetic collar",
            **self.item_fields(expected_quantity="four"),
        )
        self.assertContains(bad_count, "quantity must be a whole number", status_code=400)
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
