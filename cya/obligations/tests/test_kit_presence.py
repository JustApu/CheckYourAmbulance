"""Physical kit presence obligations and checks (issue #273)."""

from importlib import import_module
from uuid import uuid4

from django.db import DatabaseError, transaction
from django.test import tag

from cya.checklists.models import Compartment
from cya.checks.commands import CheckError, correct_check, record_check
from cya.checks.conflicts import CheckConflict
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.kits.commands import record_kit_assignment
from cya.kits.models import KitDefinition
from cya.obligations.models import Obligation, RequirementVersion
from cya.obligations.tests.test_kit_placements import PlacedKitCase


class PresenceCase(PlacedKitCase):
    def setUp(self):
        super().setUp()
        self.drawer = self.make_slot(Compartment, "drawer")
        self.presence_item = str(KitDefinition.objects.get(pk=self.definition).presence_item_id)

    def add_in_drawer(self, label):
        kit_id = self.add_placed_kit(label, self.definition)
        record_kit_assignment(
            kit_id=kit_id,
            active=True,
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.drawer.pk),
            **self.context(self.supervisor),
        )
        return kit_id

    def presence(self):
        return [o for o in self.obligations() if str(o.item_id) == self.presence_item]


class PresenceObligationTests(PresenceCase):
    def test_each_assigned_physical_kit_owes_one_presence_obligation(self):
        cabinet_kit = self.add_placed_kit("Jump kit 1", self.definition)
        drawer_kit = self.add_in_drawer("Jump kit 2")
        self.assertEqual(self.generate().failures, [])
        owed = {(str(o.kit_instance_id), o.due_at) for o in self.presence()}
        self.assertEqual(
            owed, {(kit, due) for kit in (cabinet_kit, drawer_kit) for due in self.dues}
        )
        for obligation in self.presence():
            version = obligation.requirement_version
            self.assertEqual(str(version.kit_instance_id), str(obligation.kit_instance_id))
            self.assertTrue(version.kit_event_id)
            self.assertIsNone(version.item_def_id)
            self.assertIsNone(version.kit_definition_item_id)
            self.assertIsNone(version.kit_requirement_id)
            self.assertIsNotNone(version.placement_id)
            self.assertTrue(version.kit_presence)

    def test_unassigned_stock_and_empty_vehicle_owe_no_presence(self):
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.presence(), [])
        kit = self.add_placed_kit("Another stock kit", self.definition)
        record_kit_assignment(kit_id=kit, active=False, **self.context(self.supervisor))
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.presence(), [])

    def test_regeneration_is_idempotent(self):
        self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        first = {(o.pk, o.requirement_version_id) for o in self.presence()}
        self.assertEqual(self.generate().failures, [])
        self.assertEqual({(o.pk, o.requirement_version_id) for o in self.presence()}, first)

    def test_database_refuses_a_presence_requirement_without_its_physical_kit(self):
        self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        good = RequirementVersion.objects.get(pk=self.presence()[0].requirement_version_id)
        fields = {
            f.attname: getattr(good, f.attname)
            for f in RequirementVersion._meta.concrete_fields
            if f.attname != "id"
        }
        with self.assertRaises(DatabaseError), transaction.atomic():
            RequirementVersion.objects.create(**{**fields, "kit_instance_id": None})

    def test_presence_items_are_not_generated_as_kit_contents(self):
        self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        self.assertFalse(
            Obligation.objects.filter(
                item_id=self.presence_item,
                requirement_version__kit_definition_item__isnull=False,
            ).exists()
        )


class PresenceCheckTests(PresenceCase):
    def setUp(self):
        super().setUp()
        self.cabinet_kit = self.add_placed_kit("Jump kit 1", self.definition)
        self.drawer_kit = self.add_in_drawer("Jump kit 2")
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5, minutes=30))

    def check(self, kit_id, quantity=1, actor=None, **fields):
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=self.presence_item,
            checklist_version_id=self.checklist_version,
            kit_id=kit_id,
            kit_presence=True,
            purpose="ROUTINE",
            outcome=None,
            quantity=quantity,
            **fields,
            **self.context(actor or self.crew),
        )

    def test_presence_check_satisfies_only_its_physical_kit(self):
        result = self.check(self.cabinet_kit)
        check = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertEqual(
            (check.outcome, str(check.kit_id), check.kit_presence),
            ("PASSED", self.cabinet_kit, True),
        )
        (link,) = CheckObligationLink.objects.filter(check_record=check)
        self.assertEqual(str(link.obligation.kit_instance_id), self.cabinet_kit)
        other_due = [
            o
            for o in self.presence()
            if str(o.kit_instance_id) == self.drawer_kit and o.due_at == self.dues[0]
        ]
        self.assertFalse(CheckObligationLink.objects.filter(obligation__in=other_due).exists())

    def test_presence_count_must_equal_one(self):
        for quantity in (0, 2):
            with self.subTest(quantity=quantity), self.assertRaises(CheckError) as raised:
                self.check(self.cabinet_kit, quantity)
            self.assertEqual(raised.exception.code, "DEFICIENCY_REQUIRED")

    def test_an_unassigned_or_wrong_definition_kit_is_refused(self):
        stock = self.add_placed_kit("Stock kit", self.definition)
        from cya.kits.commands import record_kit_assignment

        record_kit_assignment(kit_id=stock, active=False, **self.context(self.supervisor))
        with self.assertRaises(CheckError) as raised:
            self.check(stock)
        self.assertEqual(raised.exception.code, "ITEM_NOT_IN_CHECKLIST")
        legacy = self.add_placed_kit("Legacy kit")
        with self.assertRaises(CheckError) as raised:
            self.check(legacy)
        self.assertEqual(raised.exception.code, "ITEM_NOT_IN_CHECKLIST")

    def test_database_rejects_a_kit_presence_row_without_its_flag(self):
        check = CheckRecord.objects.get(pk=self.check(self.cabinet_kit).body["check_id"])
        fields = {
            f.attname: getattr(check, f.attname)
            for f in CheckRecord._meta.concrete_fields
            if f.attname not in ("id", "operation_id")
        }
        with self.assertRaises(DatabaseError), transaction.atomic():
            CheckRecord.objects.create(
                id=str(uuid4()), operation_id=str(uuid4()), **{**fields, "kit_presence": False}
            )

    def test_a_correction_keeps_the_physical_kit(self):
        check_id = self.check(self.cabinet_kit).body["check_id"]
        self.clock.set(self.at(hours=5, minutes=40))
        corrected = correct_check(
            check_id=check_id,
            expected_tip_check_id=check_id,
            outcome="PASSED",
            quantity=1,
            **self.context(self.crew),
        )
        row = CheckRecord.objects.get(pk=corrected.body["check_id"])
        self.assertEqual(str(row.kit_id), self.cabinet_kit)
        (link,) = CheckObligationLink.objects.filter(check_record=row)
        self.assertEqual(str(link.obligation.kit_instance_id), self.cabinet_kit)

    def test_conflict_warning_is_scoped_to_the_physical_kit(self):
        self.check(self.cabinet_kit)
        with self.assertRaises(CheckConflict):
            self.check(self.cabinet_kit, actor=self.supervisor)
        self.check(self.drawer_kit, actor=self.supervisor)

    def test_each_physical_kit_has_its_own_latest_check(self):
        from django.utils import timezone

        from cya.attention.facts import latest_effective_checks

        self.check(self.cabinet_kit)
        self.clock.set(self.at(hours=5, minutes=45))
        keys = [(self.presence_item, self.cabinet_kit), (self.presence_item, self.drawer_kit)]
        found = {
            f.kit_key: f.outcome
            for f in latest_effective_checks(
                str(self.org.pk), str(self.vehicle.pk), keys, timezone.now()
            )
        }
        self.assertEqual(found, {self.cabinet_kit: "PASSED"})

    @tag("migration_rollback")
    def test_missing_kit_failure_creates_deficiency_for_that_instance(self):
        from cya.deficiencies.commands import record_correction, start_work
        from cya.deficiencies.models import (
            Deficiency,
            DeficiencyCategory,
            DeficiencyEvent,
            VerificationScopeRow,
        )
        from cya.deficiencies.service import correction_scope
        from cya.deficiencies.severity import ensure_default_severities

        ensure_default_severities(self.org.pk, self.at(days=-1))
        category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=str(uuid4()),
            revision=1,
            label="Synthetic stock",
            supply_related=True,
            effective_at=self.at(days=-1),
        )
        failed = self.check(
            self.cabinet_kit,
            quantity=0,
            deficiency={
                "create": {
                    "category_id": str(category.stable_category_id),
                    "severity": "HIGH",
                    "summary": "Synthetic missing kit",
                    "description": "Synthetic: one assigned kit is missing.",
                }
            },
        )
        check = CheckRecord.objects.get(pk=failed.body["check_id"])
        self.assertEqual(check.outcome, "FAILED")
        deficiency = Deficiency.objects.get(pk=failed.body["deficiency_ids"][0])
        self.assertEqual(
            (str(deficiency.item_id), str(deficiency.kit_id)),
            (self.presence_item, self.cabinet_kit),
        )
        self.assertTrue(
            any(
                (row.item_id, row.definition_id, row.kit_id)
                == (self.presence_item, None, self.cabinet_kit)
                for row in correction_scope(
                    str(self.org.pk), deficiency, self.at(hours=5, minutes=30)
                )
            )
        )
        last = DeficiencyEvent.objects.filter(deficiency=deficiency).order_by("-sequence").first()
        started = start_work(
            deficiency_id=str(deficiency.pk),
            expected_event_id=str(last.pk),
            **self.context(self.supervisor),
        )
        corrected = record_correction(
            deficiency_id=str(deficiency.pk),
            expected_event_id=started.body["event_id"],
            **self.context(self.supervisor),
        )
        self.assertIn(corrected.status, (200, 201))
        row = VerificationScopeRow.objects.get(deficiency=deficiency, item_id=self.presence_item)
        self.assertEqual(str(row.kit_instance_id), self.cabinet_kit)
        self.assertIsNone(row.definition_id)
        from django.apps import apps

        reverse_guard = import_module(
            "cya.deficiencies.migrations.0028_physical_kit_presence_scope"
        ).forbid_reverse_with_physical_presence
        message = "cannot reverse physical kit presence scope"
        with self.assertRaisesRegex(RuntimeError, message):
            reverse_guard(apps, None)

    def test_checklist_and_outage_sheet_show_each_assigned_kit_once(self):
        from cya.attention.checklist import vehicle_checklist
        from cya.inspections.printable import outage_sheet

        self.check(self.cabinet_kit)
        rows = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk)).rows
        presence = {r.kit_id: r for r in rows if r.item_id == self.presence_item}
        self.assertEqual(set(presence), {self.cabinet_kit, self.drawer_kit})
        self.assertEqual(presence[self.cabinet_kit].expected_quantity, 1)
        self.assertEqual(presence[self.cabinet_kit].obligation_state, "SATISFIED")
        self.assertEqual(presence[self.drawer_kit].obligation_state, "CURRENT")
        sections, kits = outage_sheet(rows)
        self.assertEqual({kit.label for kit in kits}, {"Jump kit 1", "Jump kit 2"})
        self.assertEqual({kit.place for kit in kits}, {self.cabinet.label, self.drawer.label})

    def test_presence_row_precedes_the_same_kits_contents(self):
        from cya.attention.checklist import vehicle_checklist

        rows = list(vehicle_checklist(str(self.org.pk), str(self.vehicle.pk)).rows)
        for kit_id in (self.cabinet_kit, self.drawer_kit):
            presence = [
                i
                for i, row in enumerate(rows)
                if row.kit_id == kit_id and row.item_id == self.presence_item
            ]
            contents = [
                i
                for i, row in enumerate(rows)
                if row.kit_id == kit_id
                and row.item_id in {str(self.kit_item.pk), str(self.gauze.pk)}
            ]
            self.assertTrue(presence and contents)
            self.assertLess(max(presence), min(contents))


class PresenceItemTests(PresenceCase):
    """A presence item is not an ordinary checklist item or kit content."""

    def test_validation_refuses_a_presence_item_in_a_template(self):
        from cya.checklists import commands
        from cya.checklists.validation import ChecklistValidationError

        items = self.base_items() + [
            {**self.base_items()[0], "item_id": self.presence_item, "display_order": 99}
        ]
        with self.assertRaises(ChecklistValidationError):
            commands.save_template_draft(
                template_id=str(self.template.pk), items=items, **self.context()
            )

    def test_the_database_refuses_a_presence_item_definition(self):
        from cya.checklists import commands
        from cya.checklists.models import ChecklistItemDef

        draft = commands.save_template_draft(
            template_id=str(self.template.pk), items=self.base_items(), **self.context()
        ).body["version_id"]
        source = ChecklistItemDef.objects.filter(checklist_version_id=draft).first()
        fields = {
            f.attname: getattr(source, f.attname)
            for f in ChecklistItemDef._meta.concrete_fields
            if f.attname not in ("id", "item_id")
        }
        ChecklistItemDef.objects.create(**fields, item_id=str(self.kit_item.pk))
        with self.assertRaises(DatabaseError), transaction.atomic():
            ChecklistItemDef.objects.create(**fields, item_id=self.presence_item)
