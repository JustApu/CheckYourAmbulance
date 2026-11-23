"""SPEC 6.0.0 18.3 migration to vehicle item requirements (AC-42 (5); issue #256 C1)."""

from io import StringIO
from uuid import uuid4

from django.core.management import CommandError, call_command

from cya.checklists.item_requirement_cutover import (
    ItemRequirementMigrationError,
    migrate_item_requirements,
    plan_item_requirements,
    resolve_item_requirement,
)
from cya.checklists.models import (
    ChecklistItemDef,
    ItemRequirementCutover,
    LegacyItemRequirementMap,
    VehicleItemRequirement,
    VehicleItemRequirementVersion,
)
from cya.checklists.tests.test_item_requirements import ItemRequirementCase


class MigrationCase(ItemRequirementCase):
    def system_row(self):
        return str(self.own_row(self.system, self.vehicle).pk)

    def migrate(self, operation=None):
        return migrate_item_requirements(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            operation_id=operation or str(uuid4()),
        )

    def row(self, version_id, item):
        return ChecklistItemDef.objects.get(checklist_version_id=version_id, item=item)

    def nothing_written(self):
        self.assertFalse(ItemRequirementCutover.objects.exists())
        self.assertFalse(VehicleItemRequirement.objects.exists())
        self.assertFalse(LegacyItemRequirementMap.objects.exists())


class ItemRequirementMigrationTests(MigrationCase):
    def test_dry_run_writes_nothing(self):
        self.publish_checklist(self.a, self.own_items())
        out = StringIO()
        call_command(
            "migrate_item_requirements",
            organization=str(self.org.pk),
            actor_user_id=str(self.admin.pk),
            dry_run=True,
            stdout=out,
        )
        self.assertIn("requirements: 3", out.getvalue())
        self.nothing_written()

    def test_equal_checklists_share_and_places_split(self):  # AC-42 (5)
        first = self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        second = self.publish_checklist(
            self.b,
            [
                self.suction_spec(compartment_id=None, system_id=self.system_row()),
                self.masks_spec(6),
            ],
        )
        result = self.migrate()
        self.assertEqual(result.body["requirements"], 3)  # masks shared, suction split by place
        a_masks = resolve_item_requirement(self.row(first, self.masks).pk)
        b_masks = resolve_item_requirement(self.row(second, self.masks).pk)
        self.assertEqual(a_masks, b_masks)
        a_suction = resolve_item_requirement(self.row(first, self.suction).pk)
        b_suction = resolve_item_requirement(self.row(second, self.suction).pk)
        self.assertNotEqual(a_suction[0], b_suction[0])

    def test_a_conflict_refuses_and_lists_it(self):  # AC-42 (5)
        self.publish_checklist(self.a, [self.masks_spec(6)])
        self.publish_checklist(self.b, [self.masks_spec(4)])
        plan = plan_item_requirements(str(self.org.pk))
        self.assertEqual(len(plan.problems), 1)
        self.assertIn("conflict", plan.problems[0])
        self.assertIn("Synthetic checklist A", plan.problems[0])
        with self.assertRaises(ItemRequirementMigrationError):
            self.migrate()
        self.nothing_written()

    def test_a_possible_move_or_split_refuses(self):  # AC-42 (5)
        self.publish_checklist(self.a, [self.suction_spec()])
        self.publish_checklist(self.b, [self.suction_spec()])
        self.publish_checklist(
            self.a, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )
        plan = plan_item_requirements(str(self.org.pk))
        self.assertEqual(len(plan.problems), 1)
        self.assertIn("possible move or split", plan.problems[0])
        with self.assertRaises(ItemRequirementMigrationError):
            self.migrate()
        self.nothing_written()

    def test_a_move_within_one_checklist_keeps_the_stock(self):
        first = self.publish_checklist(self.a, [self.suction_spec()])
        second = self.publish_checklist(
            self.a, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )
        self.migrate()
        old = resolve_item_requirement(self.row(first, self.suction).pk)
        new = resolve_item_requirement(self.row(second, self.suction).pk)
        self.assertEqual(old[0], new[0])
        self.assertNotEqual(old[1], new[1])
        place = VehicleItemRequirementVersion.objects.get(pk=new[1]).place_kind
        self.assertEqual(place, "SYSTEM")

    def test_replay_returns_the_first_result_and_a_second_run_is_refused(self):
        self.publish_checklist(self.a, self.own_items())
        operation = str(uuid4())
        first = self.migrate(operation)
        again = self.migrate(operation)
        self.assertTrue(again.replayed)
        self.assertEqual(first.body, again.body)
        self.assertEqual(VehicleItemRequirement.objects.count(), 3)
        with self.assertRaisesRegex(ItemRequirementMigrationError, "already moved"):
            self.migrate()

    def test_later_publications_continue_the_migrated_requirements(self):
        first = self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        self.migrate()
        second = self.publish_checklist(self.b, [self.masks_spec(6)])
        self.assertEqual(
            resolve_item_requirement(self.row(first, self.masks).pk),
            resolve_item_requirement(self.row(second, self.masks).pk),
        )
        self.assertTrue(
            LegacyItemRequirementMap.objects.filter(item_def=self.row(first, self.suction)).exists()
        )

    def test_a_deficiency_on_an_item_kept_in_two_places_is_listed(self):  # AC-42 (5)
        from datetime import timedelta

        from cya.deficiencies.commands import report_deficiency
        from cya.deficiencies.models import DeficiencyCategory
        from cya.deficiencies.severity import ensure_default_severities

        ensure_default_severities(self.org.pk, self.now - timedelta(days=1))
        category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=str(uuid4()),
            revision=1,
            label="Synthetic mechanical",
            supply_related=False,
            effective_at=self.now - timedelta(days=1),
        )
        self.publish_checklist(self.a, [self.suction_spec()])
        self.publish_checklist(
            self.b, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )
        report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str(self.suction.pk)},
            category_id=category.stable_category_id,
            severity="HIGH",
            summary="Synthetic summary",
            description="Synthetic description of what is wrong.",
            **self.context(self.supervisor),
        )
        plan = plan_item_requirements(str(self.org.pk))
        self.assertEqual(plan.problems, [])
        self.assertEqual(len(plan.ambiguous_deficiencies), 1)
        self.assertEqual(self.migrate().body["ambiguous_deficiencies"], 1)  # listed; C3 blocks

    def test_the_command_refuses_and_reports(self):
        self.publish_checklist(self.a, [self.masks_spec(6)])
        self.publish_checklist(self.b, [self.masks_spec(4)])
        with self.assertRaisesRegex(CommandError, "refused, nothing written"):
            call_command(
                "migrate_item_requirements",
                organization=str(self.org.pk),
                actor_user_id=str(self.admin.pk),
                stdout=StringIO(),
            )
        self.nothing_written()
