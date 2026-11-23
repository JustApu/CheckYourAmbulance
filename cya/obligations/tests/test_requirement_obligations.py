"""Obligations and links keyed by vehicle item requirement (SPEC 6.0.0 CHK-10, CHK-11, SCH-6;
AC-42 (4), (6); issue #256 C2)."""

from types import SimpleNamespace
from uuid import uuid4

from cya.checklists import commands as checklist_commands
from cya.checklists.item_requirement_cutover import migrate_item_requirements
from cya.checklists.models import ChecklistItemDef, VehicleItemRequirementVersion
from cya.checklists.overlay import resolve_checklist
from cya.checklists.tests.support import ALL_OUTCOMES
from cya.checklists.tests.test_item_requirements import ItemRequirementCase
from cya.checklists.validation import ChecklistValidationError
from cya.checks.linking import choose_links
from cya.obligations.models import Obligation
from cya.obligations.state import NO_KIT
from cya.obligations.tests.test_vehicle_checklists import CutoverCase


class RequirementObligationCase(CutoverCase):
    def setUp(self):
        super().setUp()
        self.a = self.vehicle_checklist(label="Synthetic checklist A")
        self.b = self.vehicle_checklist(label="Synthetic checklist B")
        self.assign(self.a)
        self.assign(self.b)

    record_item_cutover = ItemRequirementCase.record_item_cutover

    def cabinet_row(self):
        return str(self.own_row(self.cabinet, self.vehicle).pk)

    def system_row(self):
        return str(self.own_row(self.system, self.vehicle).pk)

    def suction_spec(self, **place):
        return self.spec(self.suction, 10, **{"compartment_id": self.cabinet_row(), **place})

    def masks_spec(self, quantity=6):
        return self.spec(
            self.masks,
            20,
            kind="QUANTITY",
            expected_quantity=quantity,
            compartment_id=self.cabinet_row(),
        )

    def version_of(self, version_id, item):
        row = ChecklistItemDef.objects.get(checklist_version_id=version_id, item=item)
        return VehicleItemRequirementVersion.objects.get(pk=row.item_requirement_version_id)

    def publish_requirement(self, requirement_id, **fields):
        base = {
            "kind": "PRESENCE_CONDITION",
            "allowed_outcomes": ALL_OUTCOMES,
            "instructions": "Synthetic.",
        }
        return checklist_commands.publish_item_requirement_version(
            requirement_id=str(requirement_id),
            **{**base, **fields},
            **self.context(),
        ).body

    def check_of(self, item_requirement_version_id, item=None):
        return SimpleNamespace(
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle.pk),
            item_id=str((item or self.suction).pk),
            kit_id=None,
            kit_requirement_id=None,
            item_requirement_version_id=str(item_requirement_version_id),
            checklist_version_id=None,
        )

    def obligations_of(self, item, due):
        return Obligation.objects.filter(
            organization_id=self.org.pk, vehicle=self.vehicle, item=item, due_at=due
        )


class RequirementStreamTests(RequirementObligationCase):
    def setUp(self):
        super().setUp()
        self.record_item_cutover()

    def test_two_places_of_one_item_owe_two_obligations(self):  # CHK-10 per requirement
        a = self.publish_checklist(self.a, [self.suction_spec()])
        b = self.publish_checklist(
            self.b, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )
        desired = self.desired(self.at(days=2, hours=6))
        units = {unit for item, unit in desired.items if item == str(self.suction.pk)}
        self.assertEqual(
            units,
            {
                str(self.version_of(a, self.suction).requirement_id),
                str(self.version_of(b, self.suction).requirement_id),
            },
        )

    def test_a_requirement_change_reaches_every_checklist(self):  # AC-42 (4)
        a = self.publish_checklist(self.a, [self.masks_spec(6)])
        self.publish_checklist(self.b, [self.masks_spec(6)])
        requirement = self.version_of(a, self.masks).requirement_id
        body = self.publish_requirement(
            requirement, kind="QUANTITY", expected_quantity=4, compartment_id=self.cabinet_row()
        )
        later = self.at(days=2, hours=6)
        for checklist in (self.a, self.b):
            resolved = resolve_checklist(
                organization_id=str(self.org.pk),
                vehicle_id=str(self.vehicle.pk),
                template_id=str(checklist.pk),
                as_of=later,
            )
            (masks,) = [r for r in resolved.items if r.requirement.item_id == str(self.masks.pk)]
            self.assertEqual(masks.effective_quantity, 4)
            self.assertEqual(
                masks.requirement.item_requirement_version_id, body["requirement_version_id"]
            )
        (desired,) = [
            d for (item, _u), d in self.desired(later).items.items() if item == str(self.masks.pk)
        ]
        self.assertEqual(desired.item_requirement_version_id, body["requirement_version_id"])

    def test_another_requirement_cannot_take_an_occupied_place(self):
        self.publish_checklist(self.a, [self.suction_spec()])
        b = self.publish_checklist(
            self.b, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )
        moved = self.version_of(b, self.suction).requirement_id
        with self.assertRaisesRegex(ChecklistValidationError, "in force at that place"):
            self.publish_requirement(moved, compartment_id=self.cabinet_row())

    def test_a_check_at_the_new_place_completes_the_open_obligation(self):  # AC-42 (6)
        a = self.publish_checklist(self.a, [self.suction_spec()])
        b = self.publish_checklist(
            self.b, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )
        moving = self.version_of(a, self.suction).requirement_id
        staying = self.version_of(b, self.suction).requirement_id
        self.generate()
        due = self.at(days=2, hours=6)
        (open_one,) = self.obligations_of(self.suction, due).filter(kit_key=str(moving))
        self.clock.set(open_one.early_at)
        self.generate()
        moved = self.publish_requirement(moving)  # Not placed from now on
        self.clock.set(due)
        links = choose_links(self.check_of(moved["requirement_version_id"]), recorded_at=due)
        linked = {str(link["obligation"].pk) for link in links}
        self.assertIn(str(open_one.pk), linked)
        self.assertEqual(open_one.requirement_version.item_def.compartment_id is not None, True)
        other = self.obligations_of(self.suction, due).filter(kit_key=str(staying))
        self.assertTrue(other.exists())
        self.assertFalse(linked & {str(o.pk) for o in other})


class TransitionTests(RequirementObligationCase):
    def test_a_pre_cutover_obligation_is_completed_only_by_its_own_requirement(self):
        a = self.publish_checklist(self.a, [self.suction_spec()])
        self.publish_checklist(
            self.b, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )
        self.generate()
        due = self.at(days=2, hours=6)
        (legacy,) = self.obligations_of(self.suction, due).filter(kit_key=NO_KIT)
        self.clock.set(legacy.early_at)
        migrate_item_requirements(
            organization_id=str(self.org.pk), actor_id=str(self.admin.pk), operation_id=str(uuid4())
        )
        row = ChecklistItemDef.objects.get(checklist_version_id=a, item=self.suction)
        from cya.checklists.item_requirement_cutover import resolve_item_requirement

        own_requirement, own_version = resolve_item_requirement(row.pk)
        others = VehicleItemRequirementVersion.objects.filter(item=self.suction).exclude(
            requirement_id=own_requirement
        )
        self.clock.set(due)
        mine = choose_links(self.check_of(own_version), recorded_at=due)
        self.assertIn(str(legacy.pk), {str(link["obligation"].pk) for link in mine})
        for other in others:
            theirs = choose_links(self.check_of(other.pk), recorded_at=due)
            self.assertNotIn(str(legacy.pk), {str(link["obligation"].pk) for link in theirs})
