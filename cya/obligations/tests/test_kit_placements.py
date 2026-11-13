"""Obligations for kits placed by definition (SPEC 4.6 KDEF-4, KDEF-8; issue #129 part 4b)."""

from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.checklists import commands
from cya.checklists.models import Item
from cya.checks.commands import CheckError, correct_check, record_check
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.deficiencies.commands import record_correction, start_work
from cya.deficiencies.models import (
    DeficiencyAffectedItem,
    DeficiencyCategory,
    DeficiencyEvent,
    VerificationScopeRow,
)
from cya.deficiencies.severity import ensure_default_severities
from cya.kits.commands import add_kit_instance
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.kits.models import KitAssignmentEvent, KitDefinitionItem
from cya.kits.tests.support import KitCase
from cya.obligations.models import Obligation, ObligationSelection, RequirementVersion


class PlacedKitCase(KitCase):
    """Vehicle A's daily 06:00 checklist places a "jump kit" definition in the cabinet slot."""

    def setUp(self):
        super().setUp()
        self.gauze = Item.objects.create(organization=self.org, label="Synthetic gauze")
        self.publish_schedule("test-0600")
        self.definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **self.context(self.supervisor)
        ).body["definition_id"]
        self.kit_version = self.kit_draft(quantity=4)
        publish_kit_definition_version(version_id=self.kit_version, **self.context(self.supervisor))
        version_id = commands.save_template_draft(
            template_id=str(self.template.pk),
            items=self.base_items(),
            placements=[
                {
                    "definition_id": self.definition,
                    "compartment_id": str(self.cabinet.pk),
                    "display_order": 90,
                }
            ],
            **self.context(),
        ).body["version_id"]
        self.publish(version_id)
        self.checklist_version = version_id
        self.bind(reconcile=False)
        self.dues = [self.at(hours=6), self.at(days=1, hours=6)]

    def kit_draft(self, quantity):
        return save_kit_definition_draft(
            definition_id=self.definition,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "QUANTITY",
                    "expected_quantity": quantity,
                    "allowed_outcomes": ["PASSED", "FAILED"],
                },
                {
                    "item_id": str(self.gauze.pk),
                    "display_order": 2,
                    "kind": "PRESENCE_CONDITION",
                    "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
                },
            ],
            **self.context(self.supervisor),
        ).body["version_id"]

    def add_placed_kit(self, label, definition_id=None):
        return add_kit_instance(
            label=label,
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            definition_id=definition_id,
            **self.context(self.supervisor),
        ).body["kit_id"]

    def content_rows(self, **kw):
        return [
            o
            for o in self.obligations(**kw)
            if o.kit_key and str(o.item_id) in {str(self.kit_item.pk), str(self.gauze.pk)}
        ]


class PlacedKitObligationTests(PlacedKitCase):
    def test_each_instance_of_the_placed_definition_owes_each_content_entry(self):
        first = self.add_placed_kit("Jump kit 1", self.definition)
        second = self.add_placed_kit("Jump kit 2", self.definition)
        self.add_placed_kit("Legacy kit")  # no definition: not required by the placement
        self.assertEqual(self.generate().failures, [])
        owed = {(str(o.item_id), str(o.kit_instance_id), o.due_at) for o in self.content_rows()}
        expected = {
            (str(item.pk), kit, due)
            for item in (self.kit_item, self.gauze)
            for kit in (first, second)
            for due in self.dues
        }
        self.assertEqual(owed, expected)
        loose = [o for o in self.obligations() if o.kit_instance_id is None]
        self.assertEqual(len(loose), 3 * len(self.dues), "the checklist's own items are unchanged")

    def test_a_requirement_pins_the_content_entry_and_placement_not_an_item_definition(self):
        kit = self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        obligation = Obligation.objects.get(
            kit_instance_id=kit, item=self.kit_item, due_at=self.dues[0]
        )
        version = obligation.requirement_version
        content = KitDefinitionItem.objects.get(version_id=self.kit_version, item=self.kit_item)
        first = KitAssignmentEvent.objects.get(kit_id=kit, sequence=1)
        self.assertIsNone(version.item_def_id)
        self.assertEqual(
            (version.kit_definition_item_id, str(version.kit_instance_id), version.kit_event_id),
            (content.pk, kit, first.pk),
        )
        self.assertIsNotNone(version.placement_id)

    def test_regeneration_is_idempotent(self):
        self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        counts = (
            Obligation.objects.count(),
            ObligationSelection.objects.count(),
            RequirementVersion.objects.count(),
        )
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(
            (
                Obligation.objects.count(),
                ObligationSelection.objects.count(),
                RequirementVersion.objects.count(),
            ),
            counts,
        )

    def test_a_later_definition_version_applies_only_from_its_effective_instant(self):
        kit = self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        first_day = Obligation.objects.get(
            kit_instance_id=kit, item=self.kit_item, due_at=self.dues[0]
        )
        later = self.kit_draft(quantity=9)
        publish_kit_definition_version(
            version_id=later,
            effective_at=self.dues[1] - timedelta(hours=1),
            **self.context(self.supervisor),
        )
        self.assertEqual(self.generate().failures, [])
        rows = {
            o.due_at: KitDefinitionItem.objects.get(
                pk=o.requirement_version.kit_definition_item_id
            ).expected_quantity
            for o in self.content_rows()
            if o.item_id == self.kit_item.pk
        }
        self.assertEqual(rows, {self.dues[0]: 4, self.dues[1]: 9})
        first_day.refresh_from_db()
        self.assertEqual(
            KitDefinitionItem.objects.get(
                pk=first_day.requirement_version.kit_definition_item_id
            ).expected_quantity,
            4,
            "an obligation already created keeps the version it was created under (§5.7)",
        )

    def test_the_database_refuses_kit_content_requirements_that_do_not_match(self):
        kit = self.add_placed_kit("Jump kit 1", self.definition)
        legacy = self.add_placed_kit("Legacy kit")
        self.assertEqual(self.generate().failures, [])
        good = RequirementVersion.objects.filter(kit_instance_id=kit, item=self.kit_item).first()
        fields = {
            f.attname: getattr(good, f.attname)
            for f in RequirementVersion._meta.concrete_fields
            if f.attname != "id"
        }

        def insert(**changes):
            with transaction.atomic():
                RequirementVersion.objects.create(id=str(uuid4()), **{**fields, **changes})

        legacy_event = KitAssignmentEvent.objects.get(kit_id=legacy, sequence=1)
        for name, changes in {
            "kit without the placed definition": {
                "kit_instance_id": legacy,
                "kit_key": legacy,
                "kit_event_id": legacy_event.pk,
            },
            "content of another item": {"item_id": self.gauze.pk},
            "both sources": {
                "item_def_id": RequirementVersion.objects.exclude(item_def_id=None)
                .first()
                .item_def_id
            },
            "no kit instance": {
                "kit_instance_id": None,
                "kit_key": "00000000-0000-0000-0000-000000000000",
                "kit_event_id": None,
            },
        }.items():
            with self.subTest(name), self.assertRaises(DatabaseError):
                insert(**changes)


class NothingPublishedTests(KitCase):
    def test_a_placed_definition_with_no_version_in_force_owes_nothing(self):
        self.publish_schedule("test-0600")
        definition = create_kit_definition(
            key="later-kit", label="Synthetic later kit", **self.context(self.supervisor)
        ).body["definition_id"]
        version = save_kit_definition_draft(
            definition_id=definition,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "PRESENCE_CONDITION",
                    "allowed_outcomes": ["PASSED"],
                }
            ],
            **self.context(self.supervisor),
        ).body["version_id"]
        publish_kit_definition_version(
            version_id=version,
            effective_at=self.at(days=1, hours=7),
            **self.context(self.supervisor),
        )
        checklist = commands.save_template_draft(
            template_id=str(self.template.pk),
            items=self.base_items(),
            placements=[
                {
                    "definition_id": definition,
                    "compartment_id": str(self.cabinet.pk),
                    "display_order": 90,
                }
            ],
            **self.context(),
        ).body["version_id"]
        self.publish(checklist)
        self.bind(reconcile=False)
        add_kit_instance(
            label="Later kit",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            definition_id=definition,
            **self.context(self.supervisor),
        )
        self.assertEqual(self.generate().failures, [])
        self.assertFalse(Obligation.objects.filter(item=self.kit_item).exists())


class PlacedKitCheckTests(PlacedKitCase):
    """Recording and correcting checks of placed kit content (issue #129 part 4c)."""

    def check(self, kit_id, item=None, **fields):
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str((item or self.kit_item).pk),
            checklist_version_id=self.checklist_version,
            kit_id=kit_id,
            purpose="ROUTINE",
            **{"outcome": None, "quantity": 4, **fields},
            **self.context(self.crew),
        )

    def test_a_count_on_placed_content_is_recorded_against_the_content_entry(self):
        kit = self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5, minutes=30))
        result = self.check(kit)
        check = CheckRecord.objects.get(pk=result.body["check_id"])
        content = KitDefinitionItem.objects.get(version_id=self.kit_version, item=self.kit_item)
        self.assertEqual(
            (check.outcome, check.item_def_id, check.kit_definition_item_id),
            ("PASSED", None, content.pk),
        )
        self.assertEqual(str(check.kit_id), kit)
        link = CheckObligationLink.objects.get(check_record=check)
        self.assertEqual(link.obligation.requirement_version.kit_definition_item_id, content.pk)

    def test_a_correction_keeps_the_content_entry(self):
        kit = self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5, minutes=30))
        first = self.check(kit, item=self.gauze, outcome="PASSED", quantity=None)
        corrected = correct_check(
            check_id=first.body["check_id"],
            expected_tip_check_id=first.body["check_id"],
            outcome="PASSED",
            **self.context(self.crew),
        )
        row = CheckRecord.objects.get(pk=corrected.body["check_id"])
        original = CheckRecord.objects.get(pk=first.body["check_id"])
        self.assertEqual(
            (row.item_def_id, row.kit_definition_item_id), (None, original.kit_definition_item_id)
        )

    def test_a_kit_without_the_placed_definition_cannot_take_its_content(self):
        self.add_placed_kit("Jump kit 1", self.definition)
        legacy = self.add_placed_kit("Legacy kit")
        self.clock.set(self.at(hours=5, minutes=30))
        with self.assertRaises(CheckError) as raised:
            self.check(legacy)
        self.assertEqual(raised.exception.code, "KIT_NOT_ON_VEHICLE")
        with self.assertRaises(CheckError) as missing:
            self.check(None)
        self.assertEqual(missing.exception.code, "KIT_REQUIRED")

    def test_a_failed_count_opens_a_deficiency_whose_scope_is_the_kit_content(self):
        kit = self.add_placed_kit("Jump kit 1", self.definition)
        self.assertEqual(self.generate().failures, [])
        ensure_default_severities(self.org.pk, self.at(days=-1))
        category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=str(uuid4()),
            revision=1,
            label="Synthetic stock",
            supply_related=True,
            effective_at=self.at(days=-1),
        )
        self.clock.set(self.at(hours=5, minutes=30))
        failed = self.check(
            kit,
            quantity=3,
            deficiency={
                "create": {
                    "category_id": str(category.stable_category_id),
                    "severity": "HIGH",
                    "summary": "Synthetic short count",
                    "description": "Synthetic: one missing.",
                }
            },
        )
        check = CheckRecord.objects.get(pk=failed.body["check_id"])
        self.assertEqual(check.outcome, "FAILED")
        deficiency_id = failed.body["deficiency_ids"][0]
        affected = DeficiencyAffectedItem.objects.get(deficiency_id=deficiency_id)
        self.assertEqual(
            (affected.definition_id, affected.kit_definition_item_id),
            (None, check.kit_definition_item_id),
        )
        last = (
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id)
            .order_by("-sequence")
            .first()
        )
        started = start_work(
            deficiency_id=deficiency_id,
            expected_event_id=str(last.pk),
            **self.context(self.supervisor),
        )
        corrected = record_correction(
            deficiency_id=deficiency_id,
            expected_event_id=started.body["event_id"],
            **self.context(self.supervisor),
        )
        self.assertIn(corrected.status, (200, 201))
        rows = VerificationScopeRow.objects.filter(deficiency_id=deficiency_id)
        contents = KitDefinitionItem.objects.filter(version_id=self.kit_version)
        # A deficiency on a kit covers all of that kit's content, as for slot-placed kits.
        self.assertEqual(
            sorted(
                (r.definition_id, r.kit_definition_item_id, str(r.kit_instance_id)) for r in rows
            ),
            sorted((None, c.pk, kit) for c in contents),
        )
