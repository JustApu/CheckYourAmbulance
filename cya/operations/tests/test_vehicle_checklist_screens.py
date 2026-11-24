"""Vehicle checklist screens and commands after the cutover (SPEC 5.0.0; issue #220 part 3).

CHK-1, CHK-8, CHK-9 and CFG-9 (a checklist per vehicle, placed on its own positions or Not
placed), CFG-2 and IMP-6 (vehicle-local position keys), CFG-7 (assignments of a vehicle's own
checklists), CFG-4 (a duplicate copies the checklists, assignments, precedence and keys).
"""

import re
import uuid

from django.test import override_settings

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistTemplate,
    ChecklistVersion,
    Compartment,
)
from cya.checklists.tests.support import ids
from cya.kits import keys, positions
from cya.kits.keys import KeyRuleError
from cya.kits.models import VehiclePositionKey
from cya.kits.positions import PositionError
from cya.kits.tests.support import KitCase
from cya.obligations.models import RequirementBinding
from cya.operations import duplication
from cya.schedules.commands import publish_rule
from cya.schedules.engine import Rule
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings

LIST = "/administration/checklists/"


class PositionKeyTests(KitCase):
    def setUp(self):
        super().setUp()
        self.record_cutover()

    def add(self, vehicle, key, label="Synthetic shelf"):
        return positions.add_position(
            vehicle_id=str(vehicle.pk), kind="COMPARTMENT", label=label, key=key, **self.context()
        ).body

    def test_a_new_position_is_local_to_its_vehicle(self):
        first = self.add(self.vehicles[0], "shelf")
        second = self.add(self.vehicles[1], "shelf")  # the same key on another vehicle is fine
        self.assertNotEqual(first["stable_slot_id"], second["stable_slot_id"])
        self.assertTrue(first["new_position"] and second["new_position"])
        for vehicle, body in ((self.vehicles[0], first), (self.vehicles[1], second)):
            key = VehiclePositionKey.objects.get(vehicle=vehicle, key="shelf")
            self.assertEqual(str(key.stable_slot_id), body["stable_slot_id"])
        with self.assertRaisesRegex(PositionError, "already has position"):
            self.add(self.vehicles[0], "shelf")

    def test_positions_show_the_vehicle_keys(self):
        self.add(self.vehicles[0], "shelf")
        listed = positions.positions(str(self.org.pk), str(self.vehicles[0].pk), "COMPARTMENT")
        self.assertIn("shelf", [key for _row, key in listed])
        other = positions.positions(str(self.org.pk), str(self.vehicles[1].pk), "COMPARTMENT")
        self.assertNotIn("shelf", [key for _row, key in other])

    def test_missing_keys_are_set_per_vehicle(self):
        slot = str(self.cabinet.stable_slot_id)
        with self.assertRaisesRegex(KeyRuleError, "read-only"):
            keys.set_position_key(
                kind="COMPARTMENT", stable_slot_id=slot, key="cabinet", **self.context()
            )
        for vehicle in self.vehicles:
            keys.set_vehicle_position_key(
                vehicle_id=str(vehicle.pk),
                kind="COMPARTMENT",
                stable_slot_id=slot,
                key="cabinet",
                **self.context(),
            )
        self.assertEqual(VehiclePositionKey.objects.filter(key="cabinet").count(), 2)
        with self.assertRaisesRegex(KeyRuleError, "already set"):
            keys.set_vehicle_position_key(
                vehicle_id=str(self.vehicles[0].pk),
                kind="COMPARTMENT",
                stable_slot_id=slot,
                key="other",
                **self.context(),
            )


@override_settings(**real_collaborator_settings())
class ChecklistScreenTests(KitCase):
    def setUp(self):
        super().setUp()
        self.record_cutover()
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("supervisor")
        self.own = self.vehicle_checklist()
        self.url = f"{LIST}{self.own.pk}/"

    def csrf(self, url):
        page = self.client_.get(url)
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)

    def post(self, url, **fields):
        fields.setdefault("operation_id", str(uuid.uuid4()))
        return self.client_.post(url, {"csrfmiddlewaretoken": self.csrf(url), **fields})

    def test_the_list_shows_vehicle_checklists_and_creates_one_for_a_vehicle(self):
        page = self.client_.get(LIST)
        self.assertContains(page, "Vehicle checklists")
        self.assertContains(page, self.own.label)
        self.assertNotContains(page, self.template.label)  # the legacy template is history
        created = self.post(
            LIST, stage="create", label="Synthetic weekly", vehicle_id=str(self.vehicles[1].pk)
        )
        self.assertEqual(created.status_code, 302)
        weekly = ChecklistTemplate.objects.get(label="Synthetic weekly")
        self.assertEqual((weekly.vehicle_id, weekly.precedence), (self.vehicles[1].pk, 1))

    def test_an_item_can_be_left_not_placed(self):
        saved = self.post(
            f"{self.url}items/new/",
            base_revision="0",
            place="NOT_PLACED",
            kind="PRESENCE_CONDITION",
            instructions="Synthetic.",
            item_id=str(self.suction.pk),
        )
        self.assertEqual(saved.status_code, 302, saved.content.decode()[:2000])
        definition = ChecklistItemDef.objects.get(checklist_version__template=self.own)
        self.assertIsNone(definition.compartment_id)
        self.assertIsNone(definition.system_id)
        self.assertContains(self.client_.get(self.url), "Not placed")

    def test_the_editor_offers_only_the_vehicle_own_positions(self):
        page = self.client_.get(f"{self.url}items/new/").content.decode()
        own_row = self.own_row(self.cabinet, self.vehicles[0])
        other_row = self.own_row(self.cabinet, self.vehicles[1])
        self.assertIn(f"COMPARTMENT:{own_row.pk}", page)
        self.assertNotIn(f"COMPARTMENT:{other_row.pk}", page)
        self.assertIn('value="NOT_PLACED"', page)
        legacy = self.client_.get(f"{LIST}{self.template.pk}/items/new/").content.decode()
        self.assertNotIn('value="NOT_PLACED"', legacy)

    def test_the_assignments_page_offers_the_vehicle_own_checklists(self):
        publish_rule(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            slot="test-0600",
            rule=Rule(zone="UTC", wall_time=self.now.time(), effective_at=self.now),
            **ids(),
        )
        other = self.vehicle_checklist(vehicle=self.vehicles[1], label="Other vehicle's")
        url = f"/administration/vehicles/{self.vehicles[0].pk}/bindings/"
        page = self.client_.get(url)
        self.assertContains(page, "Checklist assignments")
        self.assertContains(page, self.own.label)
        self.assertNotContains(page, other.label)
        self.assertNotContains(page, self.template.label)
        assigned = self.post(
            url, stage="bind", template_id=str(self.own.pk), slot="test-0600", when="now"
        )
        self.assertEqual(assigned.status_code, 302)
        self.assertTrue(RequirementBinding.objects.filter(template=self.own).exists())
        refused = self.post(
            url, stage="bind", template_id=str(other.pk), slot="test-0600", when="now"
        )
        self.assertEqual(refused.status_code, 400)


class DuplicationTests(KitCase):
    def setUp(self):
        super().setUp()
        self.record_cutover()
        self.source = self.vehicles[0]
        publish_rule(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            slot="test-0600",
            rule=Rule(zone="UTC", wall_time=self.now.time(), effective_at=self.now),
            **ids(),
        )
        self.first = self.vehicle_checklist(label="First")
        self.second = self.vehicle_checklist(label="Second")
        for checklist in (self.first, self.second):
            self.publish_checklist(checklist, self.own_items())
        self.bind(self.first)
        keys.set_vehicle_position_key(
            vehicle_id=str(self.source.pk),
            kind="COMPARTMENT",
            stable_slot_id=str(self.cabinet.stable_slot_id),
            key="cabinet",
            **self.context(),
        )

    def bind(self, checklist):
        from cya.obligations.commands import bind_requirement

        return bind_requirement(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            vehicle_id=str(checklist.vehicle_id),
            template_id=str(checklist.pk),
            slot="test-0600",
            **ids(),
        )

    def duplicate(self):
        preview = duplication.preview_vehicle_duplicate(str(self.org.pk), str(self.source.pk))
        return duplication.duplicate_vehicle(
            source_vehicle_id=str(self.source.pk),
            vin="1HGBH41JXMN109186",
            display_name="Synthetic copy",
            base_token=preview["base_token"],
            **self.context(),
        ).body

    def test_the_copy_owns_drafts_of_each_checklist_in_the_same_order(self):
        body = self.duplicate()
        copies = ChecklistTemplate.objects.filter(vehicle_id=body["vehicle_id"]).order_by(
            "precedence"
        )
        self.assertEqual([(c.label, c.precedence) for c in copies], [("First", 1), ("Second", 2)])
        for copy in copies:
            version = ChecklistVersion.objects.get(template=copy)
            for definition in ChecklistItemDef.objects.filter(checklist_version=version):
                if definition.compartment_id:
                    self.assertEqual(
                        str(Compartment.objects.get(pk=definition.compartment_id).vehicle_id),
                        body["vehicle_id"],
                    )
        self.assertEqual(body["checklists"], 2)

    def test_the_copy_keeps_the_assignments_and_keys(self):
        body = self.duplicate()
        first_copy = ChecklistTemplate.objects.get(vehicle_id=body["vehicle_id"], label="First")
        self.assertTrue(
            RequirementBinding.objects.filter(
                vehicle_id=body["vehicle_id"], template=first_copy, slot="test-0600"
            ).exists()
        )
        self.assertFalse(
            RequirementBinding.objects.filter(
                vehicle_id=body["vehicle_id"], template__label="Second"
            ).exists()
        )
        self.assertTrue(
            VehiclePositionKey.objects.filter(vehicle_id=body["vehicle_id"], key="cabinet").exists()
        )

    def test_the_source_is_unchanged(self):
        before = ChecklistTemplate.objects.filter(vehicle=self.source).count()
        self.duplicate()
        self.assertEqual(ChecklistTemplate.objects.filter(vehicle=self.source).count(), before)


class RequirementDuplicationTests(DuplicationTests):  # SPEC 6.0.0 CFG-4; issue #256 C4
    def setUp(self):
        super().setUp()
        self.record_item_cutover()
        for checklist in (self.first, self.second):
            self.publish_checklist(checklist, self.own_items())

    def requirements_of(self, vehicle_id):
        from cya.checklists.models import VehicleItemRequirementVersion

        return list(VehicleItemRequirementVersion.objects.filter(vehicle_id=vehicle_id))

    def masks_requirement(self):
        return next(v for v in self.requirements_of(self.source.pk) if v.item_id == self.masks.pk)

    def publish_requirement(self, version, **changes):
        from cya.checklists.commands import publish_item_requirement_version

        values = {
            "kind": version.kind,
            "allowed_outcomes": version.allowed_outcomes,
            "instructions": version.instructions,
            "expected_quantity": version.expected_quantity,
            "unit": version.unit,
            "expiration_tracked": version.expiration_tracked,
            "compartment_id": str(version.compartment_id) if version.compartment_id else None,
            "system_id": str(version.system_id) if version.system_id else None,
        }
        values.update(changes)
        return publish_item_requirement_version(
            requirement_id=str(version.requirement_id), **values, **self.context()
        )

    def test_the_copy_takes_the_source_requirements_at_its_own_positions(self):
        from uuid import UUID

        body = self.duplicate()
        self.assertEqual(body["item_requirements"], 3)
        source = {str(v.item_id): v for v in self.requirements_of(self.source.pk)}
        copied = {str(v.item_id): v for v in self.requirements_of(body["vehicle_id"])}
        self.assertEqual(set(copied), set(source))
        for item, version in copied.items():
            self.assertNotEqual(version.requirement_id, source[item].requirement_id)
            self.assertEqual(UUID(str(version.requirement_id)).version, 5)
            self.assertEqual(
                (version.revision, version.place_kind, version.place_slot_id),
                (1, source[item].place_kind, source[item].place_slot_id),
            )
            if version.compartment_id:
                self.assertEqual(
                    str(Compartment.objects.get(pk=version.compartment_id).vehicle_id),
                    body["vehicle_id"],
                )

    def test_copied_drafts_take_an_edited_requirement_and_publish_under_rule_a(self):
        from cya.checklists.commands import publish_template_version

        self.publish_requirement(self.masks_requirement(), expected_quantity=8)
        body = self.duplicate()
        draft = ChecklistVersion.objects.get(
            template__vehicle_id=body["vehicle_id"], template__label="First"
        )
        masks = ChecklistItemDef.objects.get(checklist_version=draft, item=self.masks)
        self.assertEqual(masks.expected_quantity, 8)  # not the 6 the source's rows recorded
        before = len(self.requirements_of(body["vehicle_id"]))
        publish_template_version(version_id=str(draft.pk), **self.context())
        self.assertEqual(len(self.requirements_of(body["vehicle_id"])), before)

    def test_a_retired_position_leaves_one_not_placed_requirement(self):
        from unittest import mock

        from cya.checklists.commands import publish_template_version

        unplaced = self.spec(self.suction, 10, compartment_id=None, instructions="Loose.")
        third = self.vehicle_checklist(label="Third")
        self.publish_checklist(third, [unplaced])  # a second suction requirement, Not placed
        with mock.patch("cya.kits.positions.retired_blockers", return_value=[]):
            positions.revise_position(
                vehicle_id=str(self.source.pk),
                kind="COMPARTMENT",
                stable_slot_id=str(self.cabinet.stable_slot_id),
                retired=True,
                **self.context(),
            )
        preview = duplication.preview_vehicle_duplicate(str(self.org.pk), str(self.source.pk))
        self.assertTrue(any("already has the item Not placed" in e for e in preview["excluded"]))
        body = self.duplicate()
        suction = [
            v for v in self.requirements_of(body["vehicle_id"]) if v.item_id == self.suction.pk
        ]
        self.assertEqual(
            [(v.place_kind, v.instructions) for v in suction], [("NOT_PLACED", "Loose.")]
        )
        draft = ChecklistVersion.objects.get(
            template__vehicle_id=body["vehicle_id"], template__label="First"
        )
        row = ChecklistItemDef.objects.get(checklist_version=draft, item=self.suction)
        self.assertEqual((row.compartment_id, row.instructions), (None, "Loose."))
        before = len(self.requirements_of(body["vehicle_id"]))
        publish_template_version(version_id=str(draft.pk), **self.context())
        self.assertEqual(len(self.requirements_of(body["vehicle_id"])), before)


@override_settings(**real_collaborator_settings())
class RequirementEditorTests(KitCase):  # SPEC 6.0.0 CHK-11, AC-42 (1), (4); issue #256 C4
    def setUp(self):
        super().setUp()
        self.record_cutover()
        self.record_item_cutover()
        self.harness = RoleHttpHarness(organization=self.org)
        self.client_, _ = self.harness.sign_in("supervisor")
        self.first = self.vehicle_checklist(label="First")
        self.second = self.vehicle_checklist(label="Second")
        for checklist in (self.first, self.second):
            self.publish_checklist(checklist, self.own_items())
        self.cabinet_row = str(self.own_row(self.cabinet, self.vehicle).pk)

    csrf = ChecklistScreenTests.csrf
    post = ChecklistScreenTests.post

    def masks_spec(self, quantity):
        return self.spec(
            self.masks,
            20,
            kind="QUANTITY",
            expected_quantity=quantity,
            compartment_id=self.cabinet_row,
        )

    def draft(self, checklist, items):
        from cya.checklists.commands import save_template_draft

        return save_template_draft(
            template_id=str(checklist.pk), items=items, **self.context()
        ).body["version_id"]

    def masks_version(self):
        from cya.checklists.models import VehicleItemRequirementVersion

        return (
            VehicleItemRequirementVersion.objects.filter(vehicle=self.vehicle, item=self.masks)
            .order_by("-effective_at")
            .first()
        )

    def versions(self):
        from cya.checklists.models import VehicleItemRequirementVersion

        return VehicleItemRequirementVersion.objects.filter(vehicle=self.vehicle).count()

    def test_the_preview_names_a_shared_requirement_a_draft_would_change(self):  # AC-42 (1)
        from cya.operations.templates import publication_impact

        items = [i for i in self.own_items() if i["item_id"] != str(self.masks.pk)]
        draft = self.draft(self.second, [*items, self.masks_spec(4)])
        impact = publication_impact(str(self.org.pk), draft)
        self.assertTrue(impact.refused)
        self.assertTrue(
            any("First inspect it there with other requirements" in r for r in impact.blocking),
            impact.blocking,
        )
        third = self.vehicle_checklist(label="Third")
        mine = self.draft(third, [self.spec(self.siren, 30, compartment_id=self.cabinet_row)])
        lines = publication_impact(str(self.org.pk), mine).requirements
        self.assertEqual(len(lines), 1)
        self.assertIn("Synthetic siren at", lines[0])
        self.assertIn("a new item requirement", lines[0])

    def test_an_edit_from_a_published_version_keeps_a_requirement_edited_since(self):
        from cya.checklists.commands import (
            publish_item_requirement_version,
            publish_template_version,
        )

        current = self.masks_version()
        publish_item_requirement_version(
            requirement_id=str(current.requirement_id),
            kind="QUANTITY",
            allowed_outcomes=current.allowed_outcomes,
            instructions=current.instructions,
            expected_quantity=8,
            compartment_id=self.cabinet_row,
            **self.context(),
        )
        latest = ChecklistVersion.objects.filter(template=self.first).order_by("-revision").first()
        saved = self.post(
            f"{LIST}{self.first.pk}/items/{self.suction.pk}/",
            base_revision=str(latest.revision),
            place=f"COMPARTMENT:{self.cabinet_row}",
            kind="PRESENCE_CONDITION",
            instructions="Synthetic.",
            display_order="15",
            **{"outcome-FAILED": "on", "outcome-UNABLE_TO_INSPECT": "on"},
        )
        self.assertEqual(saved.status_code, 302, saved.content.decode()[:2000])
        draft = ChecklistVersion.objects.filter(template=self.first).order_by("-revision").first()
        masks = ChecklistItemDef.objects.get(checklist_version=draft, item=self.masks)
        self.assertEqual(masks.expected_quantity, 8)
        before = self.versions()
        publish_template_version(version_id=str(draft.pk), **self.context())
        self.assertEqual(self.versions(), before)  # every row uses its requirement (a)

    def draft_after_scheduled_masks(self):
        """First alone inspects the masks; masks quantity 8 is scheduled in 30 days; then an
        edit of another item starts a fresh draft of First from its published version."""
        from datetime import timedelta

        from django.utils import timezone

        from cya.checklists.commands import publish_item_requirement_version

        self.publish_checklist(
            self.second, [i for i in self.own_items() if i["item_id"] != str(self.masks.pk)]
        )
        current = self.masks_version()
        scheduled = timezone.now() + timedelta(days=30)
        publish_item_requirement_version(
            requirement_id=str(current.requirement_id),
            kind="QUANTITY",
            allowed_outcomes=current.allowed_outcomes,
            instructions=current.instructions,
            expected_quantity=8,
            compartment_id=self.cabinet_row,
            effective_at=scheduled,
            **self.context(),
        )
        latest = ChecklistVersion.objects.filter(template=self.first).order_by("-revision").first()
        saved = self.post(
            f"{LIST}{self.first.pk}/items/{self.suction.pk}/",
            base_revision=str(latest.revision),
            place=f"COMPARTMENT:{self.cabinet_row}",
            kind="PRESENCE_CONDITION",
            instructions="Synthetic.",
            display_order="15",
            **{"outcome-FAILED": "on", "outcome-UNABLE_TO_INSPECT": "on"},
        )
        self.assertEqual(saved.status_code, 302, saved.content.decode()[:2000])
        draft = ChecklistVersion.objects.filter(template=self.first).order_by("-revision").first()
        masks = ChecklistItemDef.objects.get(checklist_version=draft, item=self.masks)
        self.assertEqual(masks.expected_quantity, 8)  # the scheduled version is the baseline
        return draft, scheduled

    def test_a_fresh_draft_published_after_a_scheduled_requirement_keeps_it(self):
        from datetime import timedelta

        from cya.checklists.commands import publish_template_version

        draft, scheduled = self.draft_after_scheduled_masks()
        before, revision = self.versions(), self.masks_version().revision
        publish_template_version(
            version_id=str(draft.pk), effective_at=scheduled + timedelta(days=1), **self.context()
        )
        self.assertEqual(self.versions(), before)  # rule (a): no new version reverts quantity 8
        latest = self.masks_version()
        self.assertEqual((latest.revision, latest.expected_quantity), (revision, 8))

    def test_a_fresh_draft_published_before_a_scheduled_requirement_is_refused(self):
        from cya.checklists.commands import publish_template_version
        from cya.checklists.validation import ChecklistValidationError

        draft, _scheduled = self.draft_after_scheduled_masks()
        before = self.versions()
        with self.assertRaisesRegex(ChecklistValidationError, "later instant here"):
            publish_template_version(version_id=str(draft.pk), **self.context())
        self.assertEqual(self.versions(), before)

    def test_a_new_row_takes_the_requirement_at_the_chosen_place_only(self):
        third = self.vehicle_checklist(label="Third")
        posted = {
            "base_revision": "0",
            "kind": "QUANTITY",
            "expected_quantity": "99",
            "instructions": "Typed.",
            "item_id": str(self.masks.pk),
            "use_requirement": "on",
        }
        page = self.client_.get(f"{LIST}{third.pk}/items/new/")
        self.assertContains(page, 'name="use_requirement" checked')
        saved = self.post(
            f"{LIST}{third.pk}/items/new/", place=f"COMPARTMENT:{self.cabinet_row}", **posted
        )
        self.assertEqual(saved.status_code, 302, saved.content.decode()[:2000])
        row = ChecklistItemDef.objects.get(checklist_version__template=third, item=self.masks)
        self.assertEqual(row.expected_quantity, 6)  # the cabinet's requirement
        fourth = self.vehicle_checklist(label="Fourth")
        self.post(f"{LIST}{fourth.pk}/items/new/", place="NOT_PLACED", **posted)
        row = ChecklistItemDef.objects.get(checklist_version__template=fourth, item=self.masks)
        self.assertEqual(row.expected_quantity, 99)  # no masks requirement Not placed: as typed

    def test_the_item_form_says_who_shares_the_requirement(self):
        page = self.client_.get(f"{LIST}{self.first.pk}/items/{self.masks.pk}/")
        self.assertContains(page, 'id="shared-requirement"')
        self.assertContains(page, "that Second also inspect")

    def test_the_requirements_page_lists_and_changes_a_requirement(self):  # AC-42 (4)
        url = f"/administration/vehicles/{self.vehicle.pk}/item-requirements/"
        page = self.client_.get(url)
        self.assertContains(page, "Synthetic masks")
        self.assertContains(page, "First, Second")
        current = self.masks_version()
        form = self.client_.get(f"{url}?edit={current.requirement_id}")
        self.assertContains(form, "Change Synthetic masks")
        saved = self.post(
            f"{url}?edit={current.requirement_id}",
            requirement_id=str(current.requirement_id),
            place=f"COMPARTMENT:{self.cabinet_row}",
            kind="QUANTITY",
            expected_quantity="8",
            instructions=current.instructions,
            when="now",
            **{"outcome-FAILED": "on", "outcome-UNABLE_TO_INSPECT": "on"},
        )
        self.assertEqual(saved.status_code, 302, saved.content.decode()[:2000])
        changed = self.masks_version()
        self.assertEqual(
            (changed.requirement_id, changed.revision, changed.expected_quantity),
            (current.requirement_id, current.revision + 1, 8),
        )

    def test_crew_cannot_open_the_requirements_page(self):
        crew, _ = self.harness.sign_in("crew")
        page = crew.get(f"/administration/vehicles/{self.vehicle.pk}/item-requirements/")
        self.assertEqual(page.status_code, 403)
