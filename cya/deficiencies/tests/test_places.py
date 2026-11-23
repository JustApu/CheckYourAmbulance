"""Place of a loose-item deficiency and the migration block on it (SPEC 6.0.0 18.3 item 2).

Issue #256 part C3: a deficiency on an item kept in more than one place refuses the item-requirement
migration until a Supervisor or Administrator records its place; it is never attached to every
place.
"""

from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import CommandError, call_command
from django.db import DatabaseError, transaction

from cya.checklists.item_requirement_cutover import (
    ItemRequirementMigrationError,
    plan_item_requirements,
)
from cya.checklists.tests.support import ids
from cya.checklists.tests.test_item_requirement_migration import MigrationCase
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.deficiencies.commands import report_deficiency
from cya.deficiencies.models import Deficiency, DeficiencyCategory, DeficiencyPlace
from cya.deficiencies.places import assign_deficiency_place
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.severity import ensure_default_severities


class PlaceCase(MigrationCase):
    def setUp(self):
        super().setUp()
        ensure_default_severities(self.org.pk, self.now - timedelta(days=1))
        self.category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=str(uuid4()),
            revision=1,
            label="Synthetic mechanical",
            supply_related=False,
            effective_at=self.now - timedelta(days=1),
        )
        self.cabinet_row = self.own_row(self.cabinet, self.vehicle)
        self.system_slot = self.own_row(self.system, self.vehicle)

    def two_places(self):
        """Suction in the cabinet on A and on the lights system on B; masks in A's cabinet."""
        self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        self.publish_checklist(
            self.b, [self.suction_spec(compartment_id=None, system_id=self.system_row())]
        )

    def report(self, target=None):
        return report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target={"item_id": str(self.suction.pk)} if target is None else target,
            category_id=self.category.stable_category_id,
            severity="HIGH",
            summary="Synthetic summary",
            description="Synthetic description of what is wrong.",
            **self.context(self.supervisor),
        ).body["deficiency_id"]

    def assign(self, deficiency_id, kind="COMPARTMENT", position=None, actor=None, **extra):
        if position is None and kind == "COMPARTMENT":
            position = self.cabinet_row.pk
        if position is None and kind == "SYSTEM":
            position = self.system_slot.pk
        return assign_deficiency_place(
            deficiency_id=deficiency_id,
            place_kind=kind,
            position_id=None if position is None else str(position),
            explanation=extra.pop("explanation", "Synthetic: the crew meant the cabinet unit."),
            **{**self.context(actor or self.supervisor), **extra},
        )

    def refused(self, code, call):
        with self.assertRaises(DeficiencyError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(DeficiencyPlace.objects.exists())


class AssignmentTests(PlaceCase):
    def test_a_supervisor_records_the_place_once_and_it_is_audited(self):
        self.two_places()
        deficiency_id = self.report()
        result = self.assign(deficiency_id)
        place = DeficiencyPlace.objects.get()
        self.assertEqual(str(place.deficiency_id), deficiency_id)
        self.assertEqual(
            (place.place_kind, str(place.place_slot_id), place.compartment_id, place.system_id),
            ("COMPARTMENT", str(self.cabinet.stable_slot_id), self.cabinet_row.pk, None),
        )
        self.assertEqual((place.vehicle_id, place.item_id), (self.vehicle.pk, self.suction.pk))
        self.assertEqual(place.actor_user_id, self.supervisor.pk)
        self.assertEqual(place.command.command_type, "DEFICIENCY_PLACE_ASSIGN")
        self.assertEqual(result.body["deficiency_place_id"], str(place.pk))
        audit = AuditEvent.objects.get(action="DEFICIENCY_PLACE_ASSIGN", outcome="SUCCEEDED")
        self.assertNotIn("Synthetic: the crew", str(audit.new_snapshot))  # identifiers only
        self.refused_again(deficiency_id)

    def refused_again(self, deficiency_id):
        with self.assertRaises(DeficiencyError) as caught:
            self.assign(deficiency_id, "SYSTEM")
        self.assertEqual(caught.exception.code, "PLACE_ALREADY_ASSIGNED")
        self.assertEqual(DeficiencyPlace.objects.count(), 1)

    def test_an_administrator_may_assign_and_a_system_place_is_kept(self):
        self.two_places()
        deficiency_id = self.report()
        self.assign(deficiency_id, "SYSTEM", actor=self.admin)
        place = DeficiencyPlace.objects.get()
        self.assertEqual(place.place_kind, "SYSTEM")
        self.assertEqual(place.system_id, self.system_slot.pk)

    def test_a_not_placed_place_needs_a_checklist_keeping_it_there(self):
        self.two_places()
        deficiency_id = self.report()
        self.refused("PLACE_NOT_KEPT", lambda: self.assign(deficiency_id, "NOT_PLACED"))
        self.publish_checklist(self.a, [self.suction_spec(compartment_id=None)])
        self.assign(deficiency_id, "NOT_PLACED")
        place = DeficiencyPlace.objects.get()
        self.assertEqual((place.place_kind, place.place_slot_id), ("NOT_PLACED", None))

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        self.two_places()
        deficiency_id = self.report()
        supply = self.make_user("supply")
        self.grant(supply, "SUPPLY_OFFICER")
        for actor in (self.crew, supply):
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.assign(deficiency_id, actor=actor)
        self.assertFalse(DeficiencyPlace.objects.exists())
        self.assertEqual(
            AuditEvent.objects.filter(action="DEFICIENCY_PLACE_ASSIGN", outcome="DENIED").count(),
            2,
        )

    def test_only_a_loose_item_deficiency_has_a_place(self):
        self.two_places()
        compartment = self.report({"compartment_id": str(self.cabinet_row.pk)})
        self.refused("NOT_A_LOOSE_ITEM", lambda: self.assign(compartment))

    def test_the_place_must_be_a_position_of_the_vehicle_keeping_the_item(self):
        self.two_places()
        deficiency_id = self.report()
        other = self.own_row(self.cabinet, self.vehicles[1])
        self.refused("INVALID_PLACE", lambda: self.assign(deficiency_id, position=other.pk))
        self.refused(
            "INVALID_PLACE",
            lambda: self.assign(deficiency_id, "SYSTEM", position=self.cabinet_row.pk),
        )
        masks = self.report({"item_id": str(self.masks.pk)})
        # No checklist keeps masks on the lights system.
        self.refused("PLACE_NOT_KEPT", lambda: self.assign(masks, "SYSTEM"))
        self.refused("NOT_FOUND", lambda: self.assign(str(uuid4())))

    def test_the_request_shape_is_checked_before_anything_is_read(self):
        deficiency_id = str(uuid4())
        receipts = CommandReceipt.objects.count()
        for kind, position, explanation in (
            ("CABINET", None, "Synthetic."),
            ("NOT_PLACED", str(uuid4()), "Synthetic."),
            ("COMPARTMENT", "not-an-id", "Synthetic."),
            ("COMPARTMENT", str(uuid4()), "   "),
        ):
            with self.subTest(kind=kind, position=position):
                self.refused(
                    "MALFORMED_REQUEST",
                    lambda kind=kind, position=position, explanation=explanation: (
                        assign_deficiency_place(
                            deficiency_id=deficiency_id,
                            place_kind=kind,
                            position_id=position,
                            explanation=explanation,
                            **self.context(self.supervisor),
                        )
                    ),
                )
        self.assertEqual(CommandReceipt.objects.count(), receipts)

    def test_a_replay_returns_the_first_answer(self):
        self.two_places()
        deficiency_id = self.report()
        same = ids()
        first = self.assign(deficiency_id, **same)
        again = self.assign(deficiency_id, **same)
        self.assertTrue(again.replayed)
        self.assertEqual(first.body, again.body)
        self.assertEqual(DeficiencyPlace.objects.count(), 1)

    def test_the_database_refuses_rows_the_command_would_not_write(self):
        self.two_places()
        deficiency_id = self.report()
        self.assign(deficiency_id)
        place = DeficiencyPlace.objects.get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            DeficiencyPlace.objects.filter(pk=place.pk).update(explanation="Changed.")
        masks = self.report({"item_id": str(self.masks.pk)})
        second = self.report()
        reported = Deficiency.objects.get(pk=second).command
        # Another command type's receipt; the masks deficiency named under the suction item.
        for deficiency, receipt in ((second, reported), (masks, place.command)):
            with self.subTest(deficiency=deficiency), self.assertRaises(DatabaseError):
                with transaction.atomic():
                    DeficiencyPlace.objects.create(
                        organization=self.org,
                        deficiency_id=deficiency,
                        vehicle=self.vehicle,
                        item=self.suction,
                        place_kind="NOT_PLACED",
                        explanation="Synthetic.",
                        command=receipt,
                        recorded_at=receipt.recorded_at,
                        actor_user=receipt.actor_user,
                        actor_roles_at_time=["CREW", "SUPERVISOR"],
                    )


class MigrationBlockTests(PlaceCase):
    def test_an_unassigned_ambiguous_deficiency_refuses_until_its_place_is_assigned(self):
        self.two_places()
        deficiency_id = self.report()
        plan = plan_item_requirements(str(self.org.pk))
        self.assertEqual(len(plan.problems), 1)
        self.assertIn("ambiguous place", plan.problems[0])
        self.assertIn(deficiency_id, plan.problems[0])
        with self.assertRaisesRegex(ItemRequirementMigrationError, "ambiguous place"):
            self.cutover()
        self.nothing_written()
        out = StringIO()
        with self.assertRaisesRegex(CommandError, "1 problem"):
            call_command(
                "migrate_item_requirements",
                organization=str(self.org.pk),
                actor_user_id=str(self.admin.pk),
                dry_run=True,
                stdout=out,
            )
        self.assertIn("problem: ambiguous place", out.getvalue())

        self.assign(deficiency_id, "SYSTEM")
        plan = plan_item_requirements(str(self.org.pk))
        self.assertEqual(plan.problems, [])
        body = self.cutover().body
        self.assertEqual((body["ambiguous_deficiencies"], body["placed_deficiencies"]), (1, 1))
        requirement = next(
            r for r in plan.requirements if r.id == plan.deficiency_requirements[deficiency_id][2]
        )
        self.assertEqual([v.place[0] for v in requirement.versions], ["SYSTEM"])

    def test_each_assigned_place_maps_to_its_own_requirement(self):
        self.two_places()
        cabinet, lights = self.report(), self.report()
        self.assign(cabinet)
        self.assign(lights, "SYSTEM")
        mapped = plan_item_requirements(str(self.org.pk)).deficiency_requirements
        self.assertNotEqual(mapped[cabinet][2], mapped[lights][2])

    def test_a_deficiency_on_an_item_kept_in_one_place_needs_no_assignment(self):
        self.publish_checklist(self.a, [self.suction_spec(), self.masks_spec(6)])
        self.publish_checklist(self.b, [self.masks_spec(6)])  # the same stock, shared
        self.report()
        self.report({"item_id": str(self.masks.pk)})
        plan = plan_item_requirements(str(self.org.pk))
        self.assertEqual((plan.problems, plan.ambiguous_deficiencies), ([], []))
        self.assertEqual(self.cutover().body["ambiguous_deficiencies"], 0)
