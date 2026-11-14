"""Kit presence obligations (SPEC 2.0.0 4.6 KDEF-10 to KDEF-12; issue #129)."""

from uuid import uuid4

from django.db import DatabaseError, transaction

from cya.checklists.models import Compartment
from cya.checks.commands import CheckError, correct_check, record_check
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.kits.kit_lists import publish_vehicle_kit_list, save_vehicle_kit_list_draft
from cya.kits.models import KitDefinition, VehicleKitRequirement
from cya.kits.presence import line_unit_key
from cya.obligations.models import Obligation, RequirementVersion
from cya.obligations.state import NO_KIT
from cya.obligations.tests.test_kit_placements import PlacedKitCase


class PresenceCase(PlacedKitCase):
    def setUp(self):
        super().setUp()
        self.drawer = self.make_slot(Compartment, "drawer")
        self.presence_item = str(KitDefinition.objects.get(pk=self.definition).presence_item_id)

    def kit_list(self, *lines, vehicle=None):
        version_id = save_vehicle_kit_list_draft(
            vehicle_id=str((vehicle or self.vehicle).pk),
            lines=[
                {
                    "definition_id": self.definition,
                    "compartment_id": str(compartment.pk),
                    "required_count": count,
                    "display_order": order,
                }
                for order, (compartment, count) in enumerate(lines, start=1)
            ],
            **self.context(self.supervisor),
        ).body["version_id"]
        publish_vehicle_kit_list(version_id=version_id, **self.context(self.supervisor))
        return version_id

    def presence(self):
        return [o for o in self.obligations() if str(o.item_id) == self.presence_item]

    def unit(self, compartment):
        return line_unit_key(str(self.vehicle.pk), self.definition, compartment.stable_slot_id)


class PresenceObligationTests(PresenceCase):
    def test_each_kit_list_line_owes_its_own_presence_obligation(self):
        self.kit_list((self.cabinet, 2), (self.drawer, 1))
        self.assertEqual(self.generate().failures, [])
        owed = {(o.kit_key, o.due_at) for o in self.presence()}
        self.assertEqual(
            owed,
            {(self.unit(slot), due) for slot in (self.cabinet, self.drawer) for due in self.dues},
            "two compartments of one definition are two obligations per due instant",
        )
        for obligation in self.presence():
            version = obligation.requirement_version
            self.assertIsNone(obligation.kit_instance_id, "a presence obligation names no kit")
            self.assertIsNone(version.kit_definition_item_id)
            self.assertIsNone(version.item_def_id)
            self.assertIsNotNone(version.kit_requirement_id)
            self.assertIsNotNone(version.placement_id)

    def test_no_kit_list_owes_no_presence_and_contents_still_follow_instances(self):
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.presence(), [])

    def test_regeneration_is_idempotent(self):
        self.kit_list((self.cabinet, 2), (self.drawer, 1))
        self.assertEqual(self.generate().failures, [])
        first = {(o.pk, o.requirement_version_id) for o in self.presence()}
        self.assertEqual(self.generate().failures, [])
        self.assertEqual({(o.pk, o.requirement_version_id) for o in self.presence()}, first)

    def test_a_count_change_stays_on_the_same_stream(self):
        self.kit_list((self.cabinet, 2))
        self.assertEqual(self.generate().failures, [])
        before = {o.kit_key for o in self.presence()}
        self.kit_list((self.cabinet, 3))
        self.assertEqual(self.generate().failures, [])
        self.assertEqual({o.kit_key for o in self.presence()}, before)
        effective = {
            o.due_at: o.requirement_version.kit_requirement.required_count
            for o in self.obligations()
            if str(o.item_id) == self.presence_item
        }
        self.assertEqual(len(effective), len(self.dues), "one effective obligation per due")

    def test_the_database_refuses_mismatched_presence_requirements(self):
        self.kit_list((self.cabinet, 2))
        self.assertEqual(self.generate().failures, [])
        good = RequirementVersion.objects.get(pk=self.presence()[0].requirement_version_id)
        fields = {
            f.attname: getattr(good, f.attname)
            for f in RequirementVersion._meta.concrete_fields
            if f.attname != "id"
        }
        cases = {
            "a different item": {"item_id": str(self.kit_item.pk)},
            "a kit instance": {"kit_key": NO_KIT},
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(DatabaseError), transaction.atomic():
                RequirementVersion.objects.create(**{**fields, **change})

    def test_presence_obligations_are_kept_out_of_contents(self):
        self.kit_list((self.cabinet, 1))
        self.assertEqual(self.generate().failures, [])
        self.assertTrue(all(o.kit_instance_id is None for o in self.presence()))
        self.assertFalse(
            Obligation.objects.filter(
                item_id=self.presence_item, kit_instance__isnull=False
            ).exists()
        )


class PresenceCheckTests(PresenceCase):
    """Recording presence checks against one kit list line (KDEF-12, section 7.2)."""

    def setUp(self):
        super().setUp()
        version_id = self.kit_list((self.cabinet, 2), (self.drawer, 1))
        self.lines = {
            str(line.compartment_id): str(line.pk)
            for line in VehicleKitRequirement.objects.filter(version_id=version_id)
        }
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5, minutes=30))

    def check(self, compartment, quantity, **fields):
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=self.presence_item,
            checklist_version_id=self.checklist_version,
            kit_requirement_id=self.lines[str(compartment.pk)],
            purpose="ROUTINE",
            outcome=None,
            quantity=quantity,
            **fields,
            **self.context(self.crew),
        )

    def test_a_count_satisfies_only_its_own_lines_obligation(self):
        result = self.check(self.cabinet, 2)
        check = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertEqual(
            (check.outcome, str(check.kit_requirement_id), check.kit_id),
            ("PASSED", self.lines[str(self.cabinet.pk)], None),
        )
        (link,) = CheckObligationLink.objects.filter(check_record=check)
        self.assertEqual(link.obligation.kit_key, self.unit(self.cabinet))
        self.assertEqual(
            str(link.obligation.requirement_version.kit_requirement_id),
            self.lines[str(self.cabinet.pk)],
        )
        drawer_due = [
            o
            for o in self.presence()
            if o.kit_key == self.unit(self.drawer) and o.due_at == self.dues[0]
        ]
        self.assertFalse(
            CheckObligationLink.objects.filter(obligation__in=drawer_due).exists(),
            "the same due instant's other line stays unsatisfied",
        )

    def test_the_count_must_match_exactly(self):
        for quantity in (1, 3):
            with self.subTest(quantity=quantity):
                try:
                    self.check(self.cabinet, quantity)
                except CheckError as error:
                    # A failing count needs its deficiency; the derived outcome is what matters.
                    self.assertEqual(error.code, "DEFICIENCY_REQUIRED")
                else:
                    self.fail(f"a count of {quantity} for 2 required must fail")
        self.assertEqual(
            CheckRecord.objects.get(pk=self.check(self.drawer, 1).body["check_id"]).outcome,
            "PASSED",
        )

    def test_a_line_from_another_vehicle_or_definition_is_refused(self):
        with self.assertRaises(CheckError):
            record_check(
                vehicle_id=str(self.vehicle.pk),
                item_id=str(self.kit_item.pk),  # not the presence item
                checklist_version_id=self.checklist_version,
                kit_requirement_id=self.lines[str(self.cabinet.pk)],
                purpose="ROUTINE",
                outcome=None,
                quantity=2,
                **self.context(self.crew),
            )

    def test_the_database_refuses_a_presence_check_naming_a_kit(self):
        check = CheckRecord.objects.get(pk=self.check(self.cabinet, 2).body["check_id"])
        fields = {
            f.attname: getattr(check, f.attname)
            for f in CheckRecord._meta.concrete_fields
            if f.attname not in ("id", "operation_id")
        }
        kit = self.add_placed_kit("Jump kit 1", self.definition)
        with self.assertRaises(DatabaseError), transaction.atomic():
            CheckRecord.objects.create(
                id=str(uuid4()), operation_id=str(uuid4()), **{**fields, "kit_id": kit}
            )

    def test_a_correction_keeps_the_line(self):
        check_id = self.check(self.cabinet, 2).body["check_id"]
        self.clock.set(self.at(hours=5, minutes=40))
        corrected = correct_check(
            check_id=check_id,
            expected_tip_check_id=check_id,
            outcome="PASSED",
            quantity=2,
            **self.context(self.crew),
        )
        row = CheckRecord.objects.get(pk=corrected.body["check_id"])
        self.assertEqual(str(row.kit_requirement_id), self.lines[str(self.cabinet.pk)])

    def test_each_line_has_its_own_latest_check(self):
        """Review of the KDEF-12 keying: one line's check is never another line's latest."""
        from django.utils import timezone

        from cya.attention.facts import latest_effective_checks

        self.check(self.cabinet, 2)
        self.clock.set(self.at(hours=5, minutes=45))
        keys = [
            (self.presence_item, self.unit(self.cabinet)),
            (self.presence_item, self.unit(self.drawer)),
        ]
        found = {
            f.kit_key: f.outcome
            for f in latest_effective_checks(
                str(self.org.pk), str(self.vehicle.pk), keys, timezone.now()
            )
        }
        self.assertEqual(found, {self.unit(self.cabinet): "PASSED"})
