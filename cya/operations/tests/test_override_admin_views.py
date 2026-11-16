"""Vehicle override screens and command changes (SPEC CHK-6; issue #130 part 6b)."""

import re
import uuid

from django.test import override_settings

from cya.checklists import commands
from cya.checklists.models import (
    ChecklistPublication,
    Item,
    VehicleOverrideItem,
    VehicleOverrideVersion,
)
from cya.checklists.validation import ChecklistValidationError
from cya.core.models import AuditEvent
from cya.kits.tests.support import KitCase
from cya.operations.override_admin_views import carry_over
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


class OverrideCommandTests(KitCase):
    def setUp(self):
        super().setUp()
        self.version = self.published_version()

    def save(self, entries, **kw):
        return commands.save_override_draft(
            vehicle_id=str(self.vehicle.pk),
            template_version_id=self.version,
            entries=entries,
            **kw,
            **self.context(),
        )

    def new_entry(self, label):
        definition = self.spec(self.suction, 50)
        del definition["item_id"]
        return {"new_item": label, "action": "ADD", "definition": definition}

    def test_an_added_entry_may_name_a_new_item(self):
        body = self.save([self.new_entry("Synthetic tourniquet")]).body
        item = Item.objects.get(label="Synthetic tourniquet")
        self.assertEqual(body["new_item_ids"], [str(item.pk)])
        entry = VehicleOverrideItem.objects.get(override_version_id=body["override_version_id"])
        self.assertEqual((entry.action, entry.item_id), ("ADD", item.pk))

    def test_a_new_item_only_with_add_and_never_a_taken_name(self):
        bad = self.new_entry("Synthetic cot")
        bad["action"] = "REPLACE"
        with self.assertRaisesMessage(ChecklistValidationError, "can only be added"):
            self.save([bad])
        with self.assertRaisesMessage(ChecklistValidationError, "choose it instead"):
            self.save([self.new_entry("Synthetic masks")])
        self.assertFalse(VehicleOverrideVersion.objects.exists())

    def test_a_save_based_on_an_older_vehicle_revision_is_refused(self):
        remove = [{"item_id": str(self.siren.pk), "action": "REMOVE"}]
        self.save(remove, base_revision=0)
        with self.assertRaisesMessage(ChecklistValidationError, "changed since this edit"):
            self.save(remove, base_revision=0)
        self.save(remove, base_revision=1)
        self.assertEqual(VehicleOverrideVersion.objects.filter(vehicle=self.vehicle).count(), 2)

    def test_an_existing_save_keeps_its_result(self):
        body = self.save([{"item_id": str(self.siren.pk), "action": "REMOVE"}]).body
        self.assertEqual(set(body), {"code", "override_version_id", "revision"})

    def test_carry_over_keeps_what_still_applies(self):
        entries = [
            {"item_id": "a", "action": "REMOVE", "definition": None},
            {"item_id": "b", "action": "REPLACE", "definition": {}},
            {"item_id": "c", "action": "ADD", "definition": {}},
            {"item_id": "d", "action": "ADD", "definition": {}},
        ]
        kept, dropped = carry_over(entries, {"a", "c"})
        self.assertEqual([e["item_id"] for e in kept], ["a", "d"])
        self.assertEqual([e["item_id"] for e, _why in dropped], ["b", "c"])


@override_settings(**real_collaborator_settings())
class OverridePageTests(KitCase):
    def setUp(self):
        super().setUp()
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("supervisor")
        self.publish_schedule("test-0600")
        self.version = self.published_version()
        self.url = f"/administration/checklists/{self.template.pk}/vehicles/{self.vehicle.pk}/"

    def csrf(self, url):
        page = self.client_.get(url)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def post(self, url, **fields):
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return self.client_.post(url, {"csrfmiddlewaretoken": self.csrf(url), **fields})

    def overrides(self):
        return VehicleOverrideVersion.objects.filter(vehicle=self.vehicle).order_by("revision")

    def entries(self):
        latest = self.overrides().last()
        return sorted(
            VehicleOverrideItem.objects.filter(override_version=latest).values_list(
                "item__label", "action"
            )
        )

    def test_crew_is_refused_and_audited(self):
        client, user = self.harness.sign_in("crew")
        self.assertEqual(client.get(self.url).status_code, 403)
        self.assertTrue(AuditEvent.objects.filter(actor_user=user, outcome="DENIED").exists())

    def test_remove_then_undo_an_item_for_this_vehicle(self):
        page = self.client_.get(self.url)
        self.assertContains(page, "No override is in force")
        self.assertContains(page, "Synthetic siren")
        removed = self.post(
            self.url, stage="remove-item", item_id=str(self.siren.pk), base_revision="0"
        )
        self.assertEqual(removed.status_code, 302)
        self.assertEqual(self.entries(), [("Synthetic siren", "REMOVE")])
        self.assertContains(self.client_.get(self.url), "Put back")
        self.post(self.url, stage="remove-item", item_id=str(self.masks.pk), base_revision="1")
        self.post(self.url, stage="undo", item_id=str(self.siren.pk), base_revision="2")
        self.assertEqual(self.entries(), [("Synthetic masks", "REMOVE")])
        last = self.post(self.url, stage="undo", item_id=str(self.masks.pk), base_revision="3")
        self.assertContains(last, "an override needs at least one change", status_code=400)

    def test_add_a_new_item_and_change_a_template_item(self):
        fields = {
            "base_revision": "0",
            "place": f"COMPARTMENT:{self.cabinet.pk}",
            "kind": "PRESENCE_CONDITION",
            "outcome-FAILED": "on",
        }
        added = self.post(f"{self.url}items/new/", new_item="Synthetic splint", **fields)
        self.assertEqual(added.status_code, 302)
        changed = self.post(
            f"{self.url}items/{self.masks.pk}/",
            **{**fields, "base_revision": "1", "kind": "QUANTITY", "expected_quantity": "9"},
        )
        self.assertEqual(changed.status_code, 302)
        self.assertEqual(
            self.entries(), [("Synthetic masks", "REPLACE"), ("Synthetic splint", "ADD")]
        )
        page = self.client_.get(self.url)
        self.assertContains(page, "Quantity: 9")
        self.assertContains(page, "Added")
        stale = self.post(f"{self.url}items/new/", new_item="Synthetic collar", **fields)
        self.assertContains(stale, "changed since this edit started", status_code=400)

    def test_publishing_needs_a_binding_then_previews_and_publishes(self):
        self.post(self.url, stage="remove-item", item_id=str(self.siren.pk), base_revision="0")
        draft = self.overrides().last()
        page = self.client_.get(self.url)
        self.assertContains(page, "not bound to this template")
        self.assertNotContains(page, 'value="publish-preview"')
        refused = self.post(
            self.url, stage="publish", override_version_id=str(draft.pk), when="now"
        )
        self.assertContains(refused, "published only for a bound vehicle", status_code=400)
        self.bind()
        preview = self.post(
            self.url, stage="publish-preview", override_version_id=str(draft.pk), when="now"
        )
        self.assertContains(preview, "Before you publish override revision 1")
        self.assertFalse(ChecklistPublication.objects.filter(override_version=draft).exists())
        done = self.post(self.url, stage="publish", override_version_id=str(draft.pk), when="now")
        self.assertEqual(done.status_code, 302)
        self.assertTrue(ChecklistPublication.objects.filter(override_version=draft).exists())
        self.assertContains(self.client_.get(self.url), "In force: override revision 1")

    def test_an_override_of_an_older_version_is_carried_over(self):
        self.post(self.url, stage="remove-item", item_id=str(self.siren.pk), base_revision="0")
        self.post(self.url, stage="remove-item", item_id=str(self.masks.pk), base_revision="1")
        # A new template version drops the siren: the siren removal can no longer be carried.
        self.published_version(self.base_items()[:2])
        page = self.client_.get(self.url)
        self.assertContains(page, "This override is out of date")
        preview = self.post(self.url, stage="carry-preview")
        self.assertContains(preview, "Carrying it over keeps 1 change")
        self.assertContains(preview, "the template no longer has this item")
        carried = self.post(self.url, stage="carry", base_revision="2")
        self.assertEqual(carried.status_code, 302)
        latest = self.overrides().last()
        self.assertEqual(str(latest.template_version.revision), "2")
        self.assertEqual(self.entries(), [("Synthetic masks", "REMOVE")])
        self.assertNotContains(self.client_.get(self.url), "out of date")

    def test_the_template_page_lists_vehicles_for_overrides(self):
        self.bind()
        page = self.client_.get(f"/administration/checklists/{self.template.pk}/")
        self.assertContains(page, "Vehicle overrides")
        self.assertContains(page, self.url)
