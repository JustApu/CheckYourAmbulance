"""Vehicle item requirements at checklist publication (SPEC 6.0.0 CHK-11, AC-42 (1); issue #256 C1)."""

from unittest import mock

from django.db import DatabaseError, transaction
from django.test import tag

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ItemRequirementCutover,
    ItemRequirementMap,
    VehicleItemRequirement,
    VehicleItemRequirementVersion,
)
from cya.checklists.tests.support import ChecklistCase, ids
from cya.checklists.validation import ChecklistValidationError


class ItemRequirementCase(ChecklistCase):
    def setUp(self):
        super().setUp()
        self.record_cutover()
        self.vehicle = self.vehicles[0]
        self.a = self.vehicle_checklist(label="Synthetic checklist A")
        self.b = self.vehicle_checklist(label="Synthetic checklist B")

    def record_item_cutover(self):
        from cya.authorization.persistence import digest
        from cya.core.audit_commands import Plan, run_command
        from cya.kits.definitions import authorizer

        org = str(self.org.pk)

        def prepare(_actor):
            def apply(receipt, current):
                ItemRequirementCutover.objects.create(
                    organization=self.org,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    actor_user=current.user,
                    actor_roles_at_time=list(current.roles),
                )

            return Plan(
                response_body={"code": "ITEM_REQUIREMENT_CUTOVER"},
                apply=apply,
                target_type="ORGANIZATION",
                target_id=org,
                new_snapshot={"schema_version": 1, "values": {}},
            )

        run_command(
            organization_id=org,
            command_type="ITEM_REQUIREMENT_CUTOVER",
            request_digest=digest({"organization_id": org}),
            authorize=authorizer(org, str(self.admin.pk), "draft_edit_template"),
            prepare=prepare,
            **ids(),
        )

    def rows(self, version_id):
        return {
            str(row.item_id): row
            for row in ChecklistItemDef.objects.filter(checklist_version_id=version_id)
        }

    def requirement(self, version_id, item):
        row = self.rows(version_id)[str(item.pk)]
        return VehicleItemRequirementVersion.objects.get(
            pk=ItemRequirementMap.objects.get(item_def=row).requirement_version_id
        )

    def masks_spec(self, quantity, **overrides):
        cabinet = str(self.own_row(self.cabinet, self.vehicle).pk)
        return self.spec(
            self.masks,
            20,
            kind="QUANTITY",
            expected_quantity=quantity,
            compartment_id=cabinet,
            **overrides,
        )

    def suction_spec(self, **overrides):
        base = {"compartment_id": str(self.own_row(self.cabinet, self.vehicle).pk)}
        base.update(overrides)
        return self.spec(self.suction, 10, **base)


class BeforeCutoverTests(ItemRequirementCase):
    def test_rows_stay_unlinked_until_the_item_requirement_cutover(self):
        version = self.publish_checklist(self.a, self.own_items())
        self.assertFalse(ItemRequirementMap.objects.filter(checklist_version_id=version).exists())
        self.assertFalse(VehicleItemRequirement.objects.exists())


class PublicationTests(ItemRequirementCase):
    def setUp(self):
        super().setUp()
        self.record_item_cutover()

    def test_each_row_outside_a_kit_names_its_requirement(self):
        version = self.publish_checklist(self.a, self.own_items())
        places = {
            str(item): VehicleItemRequirementVersion.objects.get(
                pk=ItemRequirementMap.objects.get(item_def=row).requirement_version_id
            ).place_kind
            for item, row in self.rows(version).items()
        }
        self.assertEqual(
            places,
            {
                str(self.suction.pk): "COMPARTMENT",
                str(self.masks.pk): "COMPARTMENT",
                str(self.siren.pk): "NOT_PLACED",
            },
        )
        self.assertEqual(VehicleItemRequirement.objects.count(), 3)
        masks = self.requirement(version, self.masks)
        self.assertEqual((masks.expected_quantity, masks.revision), (6, 1))

    def test_requirement_ids_are_version_5(self):  # told apart from kit ids (C2 readers)
        from uuid import UUID

        self.publish_checklist(self.a, self.own_items())
        self.assertEqual(
            {
                UUID(str(pk)).version
                for pk in VehicleItemRequirement.objects.values_list("pk", flat=True)
            },
            {5},
        )

    def test_the_database_refuses_a_requirement_id_of_another_uuid_version(self):  # 6016641839
        from uuid import uuid4

        version = self.publish_checklist(self.a, [self.suction_spec()])
        existing = self.requirement(version, self.suction)
        evidence = {
            "organization_id": existing.organization_id,
            "command_id": existing.command_id,
            "recorded_at": existing.recorded_at,
            "actor_user_id": existing.actor_user_id,
            "actor_roles_at_time": existing.actor_roles_at_time,
        }
        with self.assertRaisesRegex(DatabaseError, "v5"), transaction.atomic():
            VehicleItemRequirement.objects.create(
                id=str(uuid4()),
                vehicle_id=existing.vehicle_id,
                item_id=existing.item_id,
                **evidence,
            )

    def test_a_second_checklist_shares_equal_requirements(self):  # AC-42 (2) groundwork
        first = self.publish_checklist(self.a, self.own_items())
        second = self.publish_checklist(self.b, self.own_items())
        for item in (self.suction, self.masks, self.siren):
            self.assertEqual(self.requirement(first, item).pk, self.requirement(second, item).pk)
        self.assertEqual(VehicleItemRequirement.objects.count(), 3)

    def test_another_checklist_cannot_require_something_else(self):  # AC-42 (1)
        self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        before = VehicleItemRequirementVersion.objects.count()
        with self.assertRaisesRegex(ChecklistValidationError, "Synthetic checklist A"):
            self.publish_checklist(self.b, [self.suction_spec(), self.masks_spec(4)])
        self.assertEqual(VehicleItemRequirementVersion.objects.count(), before)
        self.assertFalse(
            ChecklistPublication.objects.filter(checklist_version__template=self.b).exists()
        )

    def test_a_checklist_alone_changes_its_own_requirement(self):
        first = self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        second = self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(4)])
        old, new = self.requirement(first, self.masks), self.requirement(second, self.masks)
        self.assertEqual(old.requirement_id, new.requirement_id)
        self.assertEqual((new.revision, new.expected_quantity), (2, 4))
        self.assertEqual(
            self.requirement(first, self.suction), self.requirement(second, self.suction)
        )

    def test_a_move_keeps_the_stock(self):  # CHK-11
        first = self.publish_checklist(self.a, [self.suction_spec()])
        system = str(self.own_row(self.system, self.vehicle).pk)
        second = self.publish_checklist(
            self.a, [self.suction_spec(compartment_id=None, system_id=system)]
        )
        old, new = self.requirement(first, self.suction), self.requirement(second, self.suction)
        self.assertEqual(old.requirement_id, new.requirement_id)
        self.assertEqual((new.revision, new.place_kind), (2, "SYSTEM"))

    def test_a_move_while_another_checklist_keeps_the_place_adds_a_place(self):
        first = self.publish_checklist(self.a, [self.suction_spec()])
        self.publish_checklist(self.b, [self.suction_spec()])
        system = str(self.own_row(self.system, self.vehicle).pk)
        second = self.publish_checklist(
            self.a, [self.suction_spec(compartment_id=None, system_id=system)]
        )
        old, new = self.requirement(first, self.suction), self.requirement(second, self.suction)
        self.assertNotEqual(old.requirement_id, new.requirement_id)
        self.assertEqual(
            VehicleItemRequirementVersion.objects.filter(requirement_id=old.requirement_id).count(),
            1,
        )

    def test_a_later_version_at_another_place_does_not_block(self):  # review 6015890478
        from datetime import timedelta

        from django.utils import timezone

        later = timezone.now() + timedelta(days=2)
        self.publish_checklist(self.a, [self.suction_spec()], effective_at=later)
        system = str(self.own_row(self.system, self.vehicle).pk)
        version = self.publish_checklist(
            self.b, [self.suction_spec(compartment_id=None, system_id=system)]
        )
        self.assertEqual(self.requirement(version, self.suction).place_kind, "SYSTEM")
        self.assertEqual(VehicleItemRequirement.objects.count(), 2)

    def test_a_later_version_at_the_same_place_still_refuses(self):
        from datetime import timedelta

        from django.utils import timezone

        later = timezone.now() + timedelta(days=2)
        self.publish_checklist(self.a, [self.suction_spec()], effective_at=later)
        with self.assertRaisesRegex(ChecklistValidationError, "later instant here"):
            self.publish_checklist(self.b, [self.suction_spec()])

    def test_the_database_keeps_one_requirement_per_item_and_place(self):  # 18.1 item 8
        version = self.publish_checklist(self.a, [self.suction_spec()])
        existing = self.requirement(version, self.suction)
        evidence = {
            "organization_id": existing.organization_id,
            "command_id": existing.command_id,
            "recorded_at": existing.recorded_at,
            "actor_user_id": existing.actor_user_id,
            "actor_roles_at_time": existing.actor_roles_at_time,
        }
        other = VehicleItemRequirement.objects.create(
            vehicle_id=existing.vehicle_id, item_id=existing.item_id, **evidence
        )
        with (
            self.assertRaisesRegex(DatabaseError, "in force for this item and place"),
            transaction.atomic(),
        ):
            VehicleItemRequirementVersion.objects.create(
                requirement=other,
                vehicle_id=existing.vehicle_id,
                item_id=existing.item_id,
                revision=1,
                effective_at=existing.effective_at,
                place_kind=existing.place_kind,
                place_slot_id=existing.place_slot_id,
                compartment_id=existing.compartment_id,
                kind=existing.kind,
                allowed_outcomes=existing.allowed_outcomes,
                instructions=existing.instructions,
                **evidence,
            )

    def test_the_database_refuses_a_map_row_naming_other_requirements(self):  # AC-42 (1)
        from cya.checklists import commands

        published = self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        masks = self.requirement(published, self.masks)
        draft = commands.save_template_draft(
            template_id=str(self.b.pk), items=[self.masks_spec(4)], **self.context()
        ).body["version_id"]
        row = ChecklistItemDef.objects.get(checklist_version_id=draft)
        with (
            self.assertRaisesRegex(DatabaseError, "names another requirement"),
            transaction.atomic(),
        ):
            ItemRequirementMap.objects.create(
                vehicle_id=self.vehicle.pk,
                checklist_version_id=draft,
                item_def=row,
                requirement_id=masks.requirement_id,
                requirement_version_id=masks.pk,
                organization_id=masks.organization_id,
                command_id=masks.command_id,
                recorded_at=masks.recorded_at,
                actor_user_id=masks.actor_user_id,
                actor_roles_at_time=masks.actor_roles_at_time,
            )

    def plans(self, checklist, items):
        """{item id: RowPlan} of a fresh draft of `checklist`, planned for now (issue #256 C4)."""
        from django.utils import timezone

        from cya.checklists import commands, item_requirements

        draft = commands.save_template_draft(
            template_id=str(checklist.pk), items=items, **self.context()
        ).body["version_id"]
        plans = item_requirements.plan_rows(
            vehicle_id=self.vehicle.pk,
            template_id=checklist.pk,
            rows=list(ChecklistItemDef.objects.filter(checklist_version_id=draft)),
            effective_at=timezone.now(),
        )
        return {plan.item_id: plan for plan in plans.values()}

    def test_the_dry_run_names_a_shared_requirement_and_a_refusal(self):  # C4, AC-42 (1)
        from cya.checklists import item_requirements as rules

        self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        plans = self.plans(self.b, [self.suction_spec(), self.masks_spec(4)])
        self.assertEqual(plans[str(self.suction.pk)].rule, rules.USES)
        refused = plans[str(self.masks.pk)]
        self.assertEqual((refused.rule, refused.users), (rules.REFUSED, ("Synthetic checklist A",)))
        self.assertFalse(VehicleItemRequirementVersion.objects.filter(expected_quantity=4).exists())

    def test_the_dry_run_names_a_change_a_move_and_a_new_requirement(self):  # C4
        from cya.checklists import item_requirements as rules

        self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        system = str(self.own_row(self.system, self.vehicle).pk)
        items = [
            self.suction_spec(compartment_id=None, system_id=system),
            self.masks_spec(4),
            self.spec(self.siren, 30, compartment_id=None),
        ]
        plans = self.plans(self.a, items)
        self.assertEqual(
            {item: plan.rule for item, plan in plans.items()},
            {
                str(self.suction.pk): rules.MOVES,
                str(self.masks.pk): rules.NEW_VERSION,
                str(self.siren.pk): rules.NEW_REQUIREMENT,
            },
        )
        self.assertEqual(plans[str(self.suction.pk)].version.place_kind, "COMPARTMENT")
        before = VehicleItemRequirement.objects.count()
        version = self.publish_checklist(self.a, items)  # the writer applies the same rules
        self.assertEqual(VehicleItemRequirement.objects.count(), before + 1)
        moved = self.requirement(version, self.suction)
        self.assertEqual(
            (str(moved.requirement_id), moved.place_kind),
            (plans[str(self.suction.pk)].requirement_id, "SYSTEM"),
        )

    def test_the_database_refuses_an_unlinked_publication_after_the_cutover(self):
        with (
            mock.patch("cya.checklists.item_requirements.link_rows"),
            self.assertRaisesRegex(DatabaseError, "names its item requirement"),
        ):
            self.publish_checklist(self.a, [self.suction_spec()])


@tag("migration_rollback")
class GuardMigrationTests(ItemRequirementCase):
    def test_the_guard_migration_reverses_and_reapplies(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([("cya_checklists", "0013_item_requirements")])
            MigrationExecutor(connection).migrate(latest)
        finally:
            MigrationExecutor(connection).migrate(latest)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='cya_checklist_publication_itemreq_bi'"
            )
            self.assertEqual(cursor.fetchone()[0], 1)


class PositionRetirementTests(ItemRequirementCase):  # issue #256 C4, SPEC 6.0.0 CFG-2
    def setUp(self):
        super().setUp()
        self.record_item_cutover()

    def retire(self):
        from cya.kits.positions import revise_position

        return revise_position(
            vehicle_id=str(self.vehicle.pk),
            kind="COMPARTMENT",
            stable_slot_id=str(self.cabinet.stable_slot_id),
            retired=True,
            **self.context(),
        )

    def test_a_requirement_no_checklist_inspects_still_blocks_retirement(self):
        from cya.checklists import commands
        from cya.kits.positions import PositionError

        first = self.publish_checklist(self.a, [self.suction_spec()])
        self.publish_checklist(self.a, [self.spec(self.siren, 30, compartment_id=None)])
        with self.assertRaisesRegex(
            PositionError, r"item requirement of Synthetic suction \(revision 1"
        ):
            self.retire()
        suction = self.requirement(first, self.suction)
        commands.publish_item_requirement_version(  # moved to Not placed from now on
            requirement_id=str(suction.requirement_id),
            kind=suction.kind,
            allowed_outcomes=suction.allowed_outcomes,
            instructions=suction.instructions,
            **self.context(),
        )
        self.retire()

    def test_a_requirement_version_published_for_later_blocks_retirement(self):
        from datetime import timedelta

        from django.utils import timezone

        from cya.checklists import commands
        from cya.kits.positions import PositionError

        first = self.publish_checklist(self.a, [self.suction_spec(compartment_id=None)])
        self.publish_checklist(self.a, [self.spec(self.siren, 30, compartment_id=None)])
        suction = self.requirement(first, self.suction)
        commands.publish_item_requirement_version(  # moves into the cabinet tomorrow
            requirement_id=str(suction.requirement_id),
            kind=suction.kind,
            allowed_outcomes=suction.allowed_outcomes,
            instructions=suction.instructions,
            compartment_id=str(self.own_row(self.cabinet, self.vehicle).pk),
            effective_at=timezone.now() + timedelta(days=1),
            **self.context(),
        )
        with self.assertRaisesRegex(PositionError, "revision 2"):
            self.retire()
