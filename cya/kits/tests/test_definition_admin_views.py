"""Kit definition screens and command changes (SPEC KDEF-1, KDEF-2, CFG-6; issue #130 part 7)."""

import re
import uuid

from django.test import override_settings

from cya.checklists.models import Item
from cya.core.models import AuditEvent
from cya.kits.definition_admin_views import definition_uses
from cya.kits.definitions import (
    KitDefinitionError,
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.kits.models import KitDefinition, KitDefinitionPublication, KitDefinitionVersion
from cya.kits.tests.support import KitCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

LIST = "/administration/kit-definitions/"


def content(item, order=10, **overrides):
    entry = {
        "item_id": str(item.pk),
        "display_order": order,
        "kind": "QUANTITY",
        "expected_quantity": 2,
        "allowed_outcomes": ["PASSED", "FAILED"],
    }
    entry.update(overrides)
    return entry


class DraftCommandTests(KitCase):
    def setUp(self):
        super().setUp()
        self.definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **self.context()
        ).body["definition_id"]

    def save(self, contents, **kw):
        return save_kit_definition_draft(
            definition_id=self.definition, contents=contents, **kw, **self.context()
        )

    def test_a_new_content_item_by_name_is_created_with_the_draft(self):
        entry = content(self.kit_item)
        del entry["item_id"]
        body = self.save([{"new_item": "Synthetic gauze", **entry}]).body
        item = Item.objects.get(label="Synthetic gauze")
        self.assertEqual(body["new_item_ids"], [str(item.pk)])

    def test_a_taken_name_and_a_stale_edit_are_refused(self):
        entry = content(self.kit_item)
        del entry["item_id"]
        with self.assertRaisesMessage(KitDefinitionError, "choose it instead"):
            self.save([{"new_item": "Synthetic masks", **entry}])
        self.save([content(self.kit_item)], base_revision=0)
        with self.assertRaisesMessage(KitDefinitionError, "changed since this edit started"):
            self.save([content(self.kit_item)], base_revision=0)

    def test_an_existing_save_keeps_its_result(self):
        body = self.save([content(self.kit_item)]).body
        self.assertEqual(set(body), {"code", "version_id", "revision", "content_count"})


@override_settings(**real_collaborator_settings())
class DefinitionPageTests(KitCase):
    def setUp(self):
        super().setUp()
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("supervisor")

    def csrf(self, url):
        page = self.client_.get(url)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def post(self, url, **fields):
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return self.client_.post(url, {"csrfmiddlewaretoken": self.csrf(url), **fields})

    def create(self):
        created = self.post(LIST, stage="create", label="Synthetic jump kit", key="jump-kit")
        self.assertEqual(created.status_code, 302)
        definition = KitDefinition.objects.get(key="jump-kit")
        return definition, f"{LIST}{definition.pk}/"

    def fields(self, **extra):
        return {
            "base_revision": "0",
            "kind": "QUANTITY",
            "expected_quantity": "2",
            "outcome-FAILED": "on",
            **extra,
        }

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        for sub in ("crew", "officer"):
            client, user = self.harness.sign_in(sub)
            with self.subTest(role=sub):
                self.assertEqual(client.get(LIST).status_code, 403)
                self.assertTrue(
                    AuditEvent.objects.filter(actor_user=user, outcome="DENIED").exists()
                )

    def test_create_add_edit_remove_and_publish(self):
        definition, url = self.create()
        self.assertContains(self.client_.get(url), "has no versions yet")
        self.post(f"{url}items/new/", new_item="Synthetic gauze", **self.fields())
        self.post(
            f"{url}items/new/", item_id=str(self.kit_item.pk), **self.fields(base_revision="1")
        )
        gauze = Item.objects.get(label="Synthetic gauze")
        self.post(
            f"{url}items/{gauze.pk}/", **self.fields(base_revision="2", expected_quantity="5")
        )
        page = self.client_.get(url)
        self.assertContains(page, "Draft revision 3")
        self.assertContains(page, "Quantity: 5")
        self.post(url, stage="remove", item_id=str(self.kit_item.pk), base_revision="3")
        draft = (
            KitDefinitionVersion.objects.filter(definition=definition).order_by("-revision").first()
        )
        self.assertEqual(draft.revision, 4)
        preview = self.post(url, stage="publish-preview", version_id=str(draft.pk), when="now")
        self.assertContains(preview, "Before you publish revision 4")
        self.assertFalse(KitDefinitionPublication.objects.exists())
        done = self.post(url, stage="publish", version_id=str(draft.pk), when="now")
        self.assertEqual(done.status_code, 302)
        self.assertTrue(KitDefinitionPublication.objects.filter(version=draft).exists())
        stale = self.post(f"{url}items/new/", new_item="Synthetic tape", **self.fields())
        self.assertContains(stale, "changed since this edit started", status_code=400)
        bad = self.post(
            f"{url}items/new/",
            new_item="Synthetic tape",
            **self.fields(base_revision="4", expected_quantity="two"),
        )
        self.assertContains(bad, 'id="quantity-error" class="field-error"', status_code=400)

    def test_deactivate_shows_uses_and_refuses_a_changed_preview(self):
        definition, url = self.create()
        self.post(f"{url}items/new/", item_id=str(self.kit_item.pk), **self.fields())
        preview = self.post(url, stage="activity-preview", active="false")
        self.assertContains(preview, "Where it is used")
        self.assertContains(preview, "Active kit instances: 0")
        token = re.search(r'name="token" value="([^"]+)"', preview.content.decode()).group(1)
        self.add_kit_of(definition)
        changed = self.post(url, stage="activity", active="false", token=token)
        self.assertContains(changed, "has changed; check it again", status_code=400)
        definition.refresh_from_db()
        self.assertTrue(definition.active)
        token = re.search(r'name="token" value="([^"]+)"', changed.content.decode()).group(1)
        done = self.post(url, stage="activity", active="false", token=token)
        self.assertEqual(done.status_code, 302)
        definition.refresh_from_db()
        self.assertFalse(definition.active)
        back = self.post(url, stage="activity-preview", active="true")
        token = re.search(r'name="token" value="([^"]+)"', back.content.decode()).group(1)
        self.post(url, stage="activity", active="true", token=token)
        definition.refresh_from_db()
        self.assertTrue(definition.active)

    def add_kit_of(self, definition):
        from cya.kits.commands import add_kit_instance

        version = KitDefinitionVersion.objects.filter(definition=definition).first()
        publish_kit_definition_version(version_id=str(version.pk), **self.context())
        add_kit_instance(
            label="Synthetic kit 1",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            definition_id=str(definition.pk),
            **self.context(self.supervisor),
        )

    def test_duplicate_copies_a_version(self):
        definition, url = self.create()
        self.post(f"{url}items/new/", item_id=str(self.kit_item.pk), **self.fields())
        copied = self.post(f"{url}duplicate/", revision="1", label="Synthetic copy", key="copy-kit")
        self.assertEqual(copied.status_code, 302)
        self.assertTrue(KitDefinition.objects.filter(key="copy-kit").exists())

    def test_uses_are_bounded_and_empty_for_an_unused_definition(self):
        definition, _url = self.create()
        uses = definition_uses(str(self.org.pk), definition)
        self.assertEqual((uses["templates"], uses["vehicles"], uses["kit_count"]), ([], [], 0))
