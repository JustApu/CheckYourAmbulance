"""Kits placed by definition in a checklist version (SPEC 4.6 KDEF-4, issue #129 part 4a)."""

from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.checklists import commands
from cya.checklists.models import Compartment
from cya.checklists.tests.support import ChecklistCase
from cya.checklists.validation import ChecklistValidationError
from cya.kits.commands import add_kit_instance
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
    set_kit_definition_active,
)
from cya.kits.models import ChecklistKitPlacement


class PlacementCase(ChecklistCase):
    def definition(self, key="jump-kit", publish=True):
        definition_id = create_kit_definition(
            key=key, label=f"Synthetic {key}", **self.context()
        ).body["definition_id"]
        version_id = save_kit_definition_draft(
            definition_id=definition_id,
            contents=[
                {
                    "item_id": str(self.masks.pk),
                    "display_order": 1,
                    "kind": "QUANTITY",
                    "expected_quantity": 6,
                    "allowed_outcomes": ["PASSED", "FAILED"],
                }
            ],
            **self.context(),
        ).body["version_id"]
        if publish:
            publish_kit_definition_version(version_id=version_id, **self.context())
        return definition_id

    def placement(self, definition_id, order=5, compartment=None):
        return {
            "definition_id": definition_id,
            "compartment_id": str((compartment or self.cabinet).pk),
            "display_order": order,
        }

    def draft_with(self, placements):
        return commands.save_template_draft(
            template_id=str(self.template.pk),
            items=self.base_items(),
            placements=placements,
            **self.context(),
        ).body["version_id"]


class DraftAndPublishTests(PlacementCase):
    def test_a_draft_stores_placements_and_publication_freezes_them(self):
        jump = self.definition()
        version_id = self.draft_with([self.placement(jump)])
        row = ChecklistKitPlacement.objects.get(checklist_version_id=version_id)
        self.assertEqual(
            (str(row.definition_id), row.compartment_id, row.display_order),
            (jump, self.cabinet.pk, 5),
        )
        self.publish(version_id)
        for operation in (
            lambda: ChecklistKitPlacement.objects.filter(pk=row.pk).update(display_order=9),
            lambda: ChecklistKitPlacement.objects.filter(pk=row.pk).delete(),
            lambda: ChecklistKitPlacement.objects.create(
                organization=self.org,
                checklist_version_id=version_id,
                definition_id=jump,
                compartment=Compartment.objects.filter(vehicle=self.vehicles[0])
                .exclude(pk=self.cabinet.pk)
                .first()
                or self.cabinet,
                display_order=1,
            ),
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaises(DatabaseError),
                transaction.atomic(),
            ):
                operation()

    def test_a_draft_without_placements_keeps_its_request_digest(self):
        context = self.context()
        first = commands.save_template_draft(
            template_id=str(self.template.pk), items=self.base_items(), **context
        )
        again = commands.save_template_draft(
            template_id=str(self.template.pk), items=self.base_items(), placements=None, **context
        )
        self.assertTrue(again.replayed, "omitting placements is the same request as before")
        self.assertEqual(again.body, first.body)
        self.assertFalse(ChecklistKitPlacement.objects.exists())

    def test_malformed_placements_are_refused(self):
        jump = self.definition()
        inactive = self.definition("retired-kit")
        set_kit_definition_active(definition_id=inactive, active=False, **self.context())
        cases = {
            "not a list": {"definition_id": jump},
            "unknown field": [{**self.placement(jump), "colour": "red"}],
            "missing field": [{"definition_id": jump, "display_order": 1}],
            "bad order": [{**self.placement(jump), "display_order": -1}],
            "bad definition id": [self.placement("not-a-uuid")],
            "unknown definition": [self.placement(str(uuid4()))],
            "inactive definition": [self.placement(inactive)],
            "unknown compartment": [{**self.placement(jump), "compartment_id": str(uuid4())}],
            "twice in one slot": [self.placement(jump, 1), self.placement(jump, 2)],
        }
        for name, placements in cases.items():
            with self.subTest(name), self.assertRaises(ChecklistValidationError):
                self.draft_with(placements)
        self.assertFalse(ChecklistKitPlacement.objects.exists())

    def test_publishing_needs_a_published_active_definition(self):
        draft_only = self.definition("draft-only", publish=False)
        with self.assertRaises(ChecklistValidationError):
            self.publish(self.draft_with([self.placement(draft_only)]))
        jump = self.definition()
        version_id = self.draft_with([self.placement(jump)])
        set_kit_definition_active(definition_id=jump, active=False, **self.context())
        with self.assertRaises(ChecklistValidationError):
            self.publish(version_id)


class ResolutionTests(PlacementCase):
    def add_kit(self, label, definition_id=None, vehicle=None):
        vehicle = vehicle or self.vehicles[0]
        compartment = Compartment.objects.get(
            vehicle=vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        return add_kit_instance(
            label=label,
            vehicle_id=str(vehicle.pk),
            compartment_id=str(compartment.pk),
            definition_id=definition_id,
            **self.context(),
        ).body["kit_id"]

    def test_each_vehicle_resolves_its_own_slot_and_kits_of_that_definition(self):
        jump, other = self.definition(), self.definition("other-kit")
        version_id = self.draft_with([self.placement(jump)])
        self.publish(version_id)
        first = self.add_kit("Jump kit 1", jump)
        second = self.add_kit("Jump kit 2", jump)
        self.add_kit("Other kit", other)
        self.add_kit("Legacy kit")  # no definition
        resolved = self.resolve()
        (placement,) = resolved.placements
        self.assertEqual((placement.definition_id, placement.definition_key), (jump, "jump-kit"))
        self.assertEqual(placement.compartment_id, str(self.cabinet.pk))
        self.assertEqual([k for k, _ in placement.kits], [first, second])
        other_vehicle = self.resolve(vehicle=self.vehicles[1]).placements[0]
        self.assertNotEqual(
            other_vehicle.compartment_id, str(self.cabinet.pk), "each vehicle's own slot row"
        )
        self.assertEqual(other_vehicle.kits, ())
        self.assertEqual(len(resolved.items), 3, "placements do not change the resolved items")

    def test_a_version_without_placements_resolves_none(self):
        self.published_version()
        self.assertEqual(self.resolve().placements, ())
