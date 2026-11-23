"""Attention, deficiencies, verification and suspensions per place (SPEC 6.0.0 7.5, 8.1, VER-3,
SUS-2; AC-42 (2), (3); issue #256 C3).

After the item-requirement cutover an item outside a kit kept in two places is two stocks: a
failure, a deficiency, a verification row or a suspension at one place never reaches the other.
Rows recorded before the cutover keep the per-item rule.
"""

from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import tag

from cya.attention.checklist import vehicle_checklist
from cya.attention.facts import deficiency_facts
from cya.attention.state import applies
from cya.checklists.item_requirement_cutover import migrate_item_requirements
from cya.checklists.models import Compartment
from cya.checks.commands import CheckError, record_check
from cya.deficiencies import service
from cya.deficiencies.commands import record_correction, report_deficiency, start_work
from cya.deficiencies.models import (
    CheckSuspension,
    Deficiency,
    DeficiencyAffectedItem,
    DeficiencyCategory,
    DeficiencyEvent,
    DeficiencyProjection,
    DeficiencyRequirementMap,
    VerificationScopeRow,
)
from cya.deficiencies.places import assign_deficiency_place
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.severity import ensure_default_severities
from cya.deficiencies.suspension import suspend_checks
from cya.obligations.projections import (
    MAX_ROWS,
    _history,
    _page_suspensions,
    stored_suspensions,
)
from cya.obligations.tests.test_requirement_obligations import RequirementObligationCase

NO_KIT = "00000000-0000-0000-0000-000000000000"


class PerPlaceCase(RequirementObligationCase):
    """Suction kept in cabinet 1 (checklist A) and cabinet 4 (checklist B)."""

    def setUp(self):
        super().setUp()
        self.cabinet4 = self.make_slot(Compartment, "cabinet 4")
        ensure_default_severities(self.org.pk, self.now - timedelta(days=1))
        self.category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=str(uuid4()),
            revision=1,
            label="Synthetic mechanical",
            supply_related=False,
            effective_at=self.now - timedelta(days=1),
        )

    def fields(self):
        return {
            "category_id": self.category.stable_category_id,
            "severity": "HIGH",
            "summary": "Synthetic summary",
            "description": "Synthetic description of what is wrong.",
        }

    def cabinet4_spec(self):
        return self.suction_spec(compartment_id=str(self.own_row(self.cabinet4, self.vehicle).pk))

    def two_cabinets(self):
        self.version_a = self.publish_checklist(self.a, [self.suction_spec()])
        self.version_b = self.publish_checklist(self.b, [self.cabinet4_spec()])
        self.cabinet1_requirement = str(
            self.version_of(self.version_a, self.suction).requirement_id
        )
        self.cabinet4_requirement = str(
            self.version_of(self.version_b, self.suction).requirement_id
        )

    def due_window(self):
        self.generate()
        self.due = self.at(days=2, hours=6)
        first = self.obligations_of(self.suction, self.due).order_by("early_at").first()
        self.clock.set(first.early_at + timedelta(minutes=1))

    def check(self, version_id, outcome="PASSED", item=None, **fields):
        if outcome in ("FAILED", "UNABLE_TO_INSPECT") and "deficiency" not in fields:
            fields["deficiency"] = {"create": self.fields()}
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str((item or self.suction).pk),
            checklist_version_id=version_id,
            outcome=outcome,
            **fields,
            **self.context(self.crew),
        ).body

    def report(self, target, actor=None):
        return report_deficiency(
            vehicle_id=str(self.vehicle.pk),
            target=target,
            **self.fields(),
            **self.context(actor or self.supervisor),
        ).body["deficiency_id"]

    def last_event(self, deficiency_id):
        return str(
            DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence").pk
        )

    def rows(self, as_of=None):
        rows = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), as_of=as_of).rows
        return {r.item_requirement_id: r for r in rows if r.item_id == str(self.suction.pk)}

    def suspend(self, deficiency_id, items):
        return suspend_checks(
            deficiency_id=deficiency_id,
            expected_event_id=self.last_event(deficiency_id),
            items=items,
            **self.context(self.supervisor),
        )

    def suspended(self, ids, at):
        org, vehicle = str(self.org.pk), str(self.vehicle.pk)
        stored = {
            e.obligation_id
            for e in stored_suspensions(
                organization_id=org, vehicle_id=vehicle, obligation_ids=ids, as_of=at
            )
        }
        by_vehicle = {vehicle: {i: None for i in ids}}
        page = {e.obligation_id for e in _page_suspensions(org, by_vehicle, at)[vehicle]}
        return stored, page

    def streams(self, due):
        obligations = self.obligations_of(self.suction, due)
        return {
            unit: [str(o.pk) for o in obligations if o.kit_key == unit]
            for unit in (self.cabinet1_requirement, self.cabinet4_requirement)
        }


class SharedRequirementTests(PerPlaceCase):
    def setUp(self):
        super().setUp()
        self.record_item_cutover()

    def test_a_pass_in_one_checklist_shows_done_in_the_other(self):  # AC-42 (2)
        version_a = self.publish_checklist(self.a, [self.masks_spec(6)])
        version_b = self.publish_checklist(self.b, [self.masks_spec(6)])
        self.assertEqual(
            self.version_of(version_a, self.masks).requirement_id,
            self.version_of(version_b, self.masks).requirement_id,
        )
        self.generate()
        due = self.at(days=2, hours=6)
        (obligation,) = self.obligations_of(self.masks, due)  # one stock, one obligation
        self.clock.set(obligation.early_at + timedelta(minutes=1))
        check = self.check(version_a, item=self.masks, quantity=6)["check_id"]
        (row,) = [
            r
            for r in vehicle_checklist(str(self.org.pk), str(self.vehicle.pk)).rows
            if r.item_id == str(self.masks.pk)
        ]
        self.assertEqual(row.latest_check_id, check)
        self.assertEqual(row.obligation_state, "SATISFIED")
        # Read as checklist B's row: the same requirement, so the same state and check.
        self.assertIn(row.checklist_version_id, (version_a, version_b))


class TwoCabinetTests(PerPlaceCase):
    def setUp(self):
        super().setUp()
        self.record_item_cutover()
        self.two_cabinets()
        self.due_window()

    def test_a_pass_in_cabinet_1_leaves_cabinet_4_due(self):  # AC-42 (3)
        self.check(self.version_a)
        rows = self.rows()
        self.assertEqual(set(rows), {self.cabinet1_requirement, self.cabinet4_requirement})
        self.assertEqual(rows[self.cabinet1_requirement].obligation_state, "SATISFIED")
        self.assertNotEqual(rows[self.cabinet4_requirement].obligation_state, "SATISFIED")

    def test_a_fail_in_cabinet_1_gives_attention_and_a_deficiency_there_only(self):  # AC-42 (3)
        (deficiency_id,) = self.check(self.version_a, outcome="FAILED")["deficiency_ids"]
        deficiency = Deficiency.objects.get(pk=deficiency_id)
        version = self.version_of(self.version_a, self.suction)
        self.assertEqual(deficiency.item_requirement_version_id, version.pk)
        (affected,) = DeficiencyAffectedItem.objects.filter(deficiency=deficiency)
        self.assertEqual(affected.item_requirement_version_id, version.pk)
        later = self.clock() + timedelta(minutes=5)
        self.clock.set(later)
        for as_of in (None, later):
            rows = self.rows(as_of)
            one, four = rows[self.cabinet1_requirement], rows[self.cabinet4_requirement]
            self.assertTrue(one.attention.red)
            self.assertEqual(one.attention.deficiency_ids, (str(deficiency.pk),))
            self.assertFalse(four.attention.red)
            self.assertEqual(four.attention.deficiency_ids, ())
        (fact,) = deficiency_facts(str(self.org.pk), str(self.vehicle.pk))
        self.assertTrue(applies(fact, str(self.suction.pk), self.cabinet1_requirement))
        self.assertFalse(applies(fact, str(self.suction.pk), self.cabinet4_requirement))

    def test_a_report_names_the_place_of_an_item_kept_in_two(self):  # SPEC 8.1
        with self.assertRaises(DeficiencyError) as refused:
            self.report({"item_id": str(self.suction.pk)})
        self.assertEqual(refused.exception.code, "PLACE_REQUIRED")
        deficiency_id = self.report(
            {"item_id": str(self.suction.pk), "item_requirement_id": self.cabinet4_requirement}
        )
        header = Deficiency.objects.select_related("item_requirement_version").get(pk=deficiency_id)
        self.assertEqual(
            str(header.item_requirement_version.requirement_id), self.cabinet4_requirement
        )
        rows = self.rows()
        self.assertTrue(rows[self.cabinet4_requirement].attention.red)
        self.assertFalse(rows[self.cabinet1_requirement].attention.red)

    def pending(self):
        (deficiency_id,) = self.check(self.version_a, outcome="FAILED")["deficiency_ids"]
        start_work(
            deficiency_id=deficiency_id,
            expected_event_id=self.last_event(deficiency_id),
            **self.context(self.supervisor),
        )
        record_correction(
            deficiency_id=deficiency_id,
            expected_event_id=self.last_event(deficiency_id),
            **self.context(self.crew),
        )
        self.clock.set(self.clock() + timedelta(minutes=1))
        return deficiency_id

    def test_verification_is_per_place(self):  # VER-3
        deficiency_id = self.pending()
        (row,) = VerificationScopeRow.objects.filter(deficiency_id=deficiency_id)
        self.assertEqual(row.instance_key, self.cabinet1_requirement)
        self.assertIsNotNone(row.item_requirement_version_id)
        with self.assertRaises(CheckError) as refused:
            self.check(
                self.version_b, purpose="VERIFICATION", verification_for_deficiency_id=deficiency_id
            )
        self.assertEqual(refused.exception.code, "NOT_IN_VERIFICATION_SCOPE")
        self.check(
            self.version_a, purpose="VERIFICATION", verification_for_deficiency_id=deficiency_id
        )
        status = DeficiencyProjection.objects.get(deficiency_id=deficiency_id).status
        self.assertEqual(status, "CLOSED_VERIFIED")

    def test_the_check_guard_refuses_a_verification_at_another_place(self):  # trigger
        from cya.checks import commands

        deficiency_id = self.pending()
        original = commands._verification_plan

        def at_cabinet_1(organization_id, shape, chosen_kit, item_requirement_id=None):
            return original(organization_id, shape, chosen_kit, self.cabinet1_requirement)

        with (
            mock.patch.object(commands, "_verification_plan", at_cabinet_1),
            self.assertRaisesRegex(DatabaseError, "verification check is not for a pending scope"),
        ):
            self.check(
                self.version_b, purpose="VERIFICATION", verification_for_deficiency_id=deficiency_id
            )

    def test_the_scope_guard_refuses_a_place_row_with_the_per_item_key(self):  # trigger
        (deficiency_id,) = self.check(self.version_a, outcome="FAILED")["deficiency_ids"]
        start_work(
            deficiency_id=deficiency_id,
            expected_event_id=self.last_event(deficiency_id),
            **self.context(self.supervisor),
        )
        with (
            mock.patch.object(service.ScopeRow, "instance_key", property(lambda _row: NO_KIT)),
            self.assertRaisesRegex(DatabaseError, "verification scope instance key mismatch"),
        ):
            record_correction(
                deficiency_id=deficiency_id,
                expected_event_id=self.last_event(deficiency_id),
                **self.context(self.crew),
            )

    def cabinet1_items(self):
        return [{"item_id": str(self.suction.pk), "item_requirement_id": self.cabinet1_requirement}]

    def test_a_suspension_is_per_place(self):  # SUS-2
        deficiency_id = self.report(
            {"item_id": str(self.suction.pk), "item_requirement_id": self.cabinet1_requirement}
        )
        with self.assertRaises(DeficiencyError) as refused:
            self.suspend(
                deficiency_id,
                [
                    {
                        "item_id": str(self.suction.pk),
                        "item_requirement_id": self.cabinet4_requirement,
                    }
                ],
            )
        self.assertEqual(refused.exception.code, "ITEM_NOT_IN_SCOPE")
        with self.assertRaises(DeficiencyError) as refused:
            self.suspend(deficiency_id, [{"item_id": str(self.suction.pk)}])
        self.assertEqual(refused.exception.code, "PLACE_REQUIRED")
        body = self.suspend(deficiency_id, self.cabinet1_items()).body
        self.assertEqual(body["items"][0]["item_requirement_id"], self.cabinet1_requirement)
        (start,) = CheckSuspension.objects.filter(deficiency_id=deficiency_id)
        self.assertEqual(start.instance_key, self.cabinet1_requirement)
        later = self.at(days=3, hours=6)
        streams = self.streams(later)
        ids = streams[self.cabinet1_requirement] + streams[self.cabinet4_requirement]
        for found in self.suspended(ids, later):
            self.assertEqual(found, set(streams[self.cabinet1_requirement]))

    def test_the_suspension_guard_refuses_a_place_row_with_the_per_item_key(self):  # trigger
        deficiency_id = self.report(
            {"item_id": str(self.suction.pk), "item_requirement_id": self.cabinet1_requirement}
        )
        with (
            mock.patch.object(service.ScopeRow, "instance_key", property(lambda _row: NO_KIT)),
            self.assertRaisesRegex(DatabaseError, "check suspension instance key mismatch"),
        ):
            self.suspend(deficiency_id, self.cabinet1_items())

    def test_place_assignment_is_refused_after_the_cutover(self):
        deficiency_id = self.report(
            {"item_id": str(self.suction.pk), "item_requirement_id": self.cabinet1_requirement}
        )
        with self.assertRaises(DeficiencyError) as refused:
            assign_deficiency_place(
                deficiency_id=deficiency_id,
                place_kind="COMPARTMENT",
                position_id=str(self.own_row(self.cabinet, self.vehicle).pk),
                explanation="Synthetic.",
                **self.context(self.supervisor),
            )
        self.assertEqual(refused.exception.code, "CUTOVER_DONE")


class BeforeTheCutoverTests(PerPlaceCase):
    """Rows recorded before the cutover keep the per-item rule, except a recorded place."""

    def setUp(self):
        super().setUp()
        self.version_a = self.publish_checklist(self.a, [self.suction_spec()])

    def in_cabinet_4_too(self):
        self.version_b = self.publish_checklist(self.b, [self.cabinet4_spec()])

    def cut_over(self):
        migrate_item_requirements(
            organization_id=str(self.org.pk), actor_id=str(self.admin.pk), operation_id=str(uuid4())
        )
        self.two_cabinets()  # re-published after the cutover: the requirement streams
        self.due_window()

    def test_a_per_item_suspension_still_suspends_every_place(self):
        self.in_cabinet_4_too()
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id, [{"item_id": str(self.suction.pk)}])
        (start,) = CheckSuspension.objects.filter(deficiency_id=deficiency_id)
        self.assertEqual((start.instance_key, start.item_requirement_version_id), (NO_KIT, None))
        assign_deficiency_place(
            deficiency_id=deficiency_id,
            place_kind="COMPARTMENT",
            position_id=str(self.own_row(self.cabinet, self.vehicle).pk),
            explanation="Synthetic: the cabinet 1 stock.",
            **self.context(self.supervisor),
        )
        self.cut_over()
        later = self.at(days=3, hours=6)
        streams = self.streams(later)
        ids = streams[self.cabinet1_requirement] + streams[self.cabinet4_requirement]
        for found in self.suspended(ids, later):
            self.assertEqual(found, set(ids))

    def test_a_placed_deficiency_applies_at_its_place_after_the_cutover(self):
        self.in_cabinet_4_too()
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        assign_deficiency_place(
            deficiency_id=deficiency_id,
            place_kind="COMPARTMENT",
            position_id=str(self.own_row(self.cabinet4, self.vehicle).pk),
            explanation="Synthetic: the cabinet 4 stock.",
            **self.context(self.supervisor),
        )
        self.cut_over()
        mapped = DeficiencyRequirementMap.objects.get(deficiency_id=deficiency_id)
        self.assertEqual(str(mapped.requirement_id), self.cabinet4_requirement)
        later = self.clock() + timedelta(minutes=1)
        self.clock.set(later)
        for as_of in (None, later):
            rows = self.rows(as_of)
            self.assertTrue(rows[self.cabinet4_requirement].attention.red)
            self.assertFalse(rows[self.cabinet1_requirement].attention.red)

    def test_a_read_before_the_cutover_keeps_a_placed_deficiency_per_item(self):
        self.in_cabinet_4_too()
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        assign_deficiency_place(
            deficiency_id=deficiency_id,
            place_kind="COMPARTMENT",
            position_id=str(self.own_row(self.cabinet4, self.vehicle).pk),
            explanation="Synthetic: the cabinet 4 stock.",
            **self.context(self.supervisor),
        )
        before = self.clock()
        self.clock.set(before + timedelta(minutes=1))
        self.cut_over()
        mapped = DeficiencyRequirementMap.objects.get(deficiency_id=deficiency_id)
        self.assertGreater(mapped.recorded_at, before)
        after = self.clock()
        suction = str(self.suction.pk)
        per_item = (frozenset({suction}), frozenset())
        per_place = (frozenset(), frozenset({(suction, self.cabinet4_requirement)}))
        org, vehicle = str(self.org.pk), str(self.vehicle.pk)
        for instant, expected in ((before, per_item), (after, per_place)):
            (fact,) = deficiency_facts(org, vehicle, as_of=instant)
            self.assertEqual((frozenset(fact.item_ids), fact.item_units), expected, instant)
            (header,) = _history(
                org, {vehicle: {}}, self.now - timedelta(days=1), instant, MAX_ROWS
            )[vehicle]["deficiency_headers"]
            self.assertEqual((header.item_ids, header.item_units), expected, instant)

    def test_an_unplaced_deficiency_on_an_item_kept_in_one_place_stays_per_item(self):
        self.publish_checklist(self.b, [self.suction_spec()])  # one place, two checklists
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        migrate_item_requirements(
            organization_id=str(self.org.pk), actor_id=str(self.admin.pk), operation_id=str(uuid4())
        )
        self.assertFalse(DeficiencyRequirementMap.objects.filter(deficiency_id=deficiency_id))
        (fact,) = deficiency_facts(str(self.org.pk), str(self.vehicle.pk))
        self.assertEqual((fact.item_ids, fact.item_units), ({str(self.suction.pk)}, frozenset()))


@tag("migration_rollback")
class PlaceGuardMigrationTests(PerPlaceCase):
    def test_the_per_place_guards_reverse_and_reapply(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([("cya_deficiencies", "0025_deficiency_place_guards")])
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE "
                    "TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='cya_check_suspension_bi'"
                )
                self.assertNotIn("item_requirement_version_id", cursor.fetchone()[0])
        finally:
            MigrationExecutor(connection).migrate(latest)
        with connection.cursor() as cursor:
            for trigger in (
                "cya_check_suspension_bi",
                "cya_verification_scope_row_bi",
                "cya_deficiency_scope_expansion_bi",
                "cya_deficiency_event_bi",
                "cya_check_record_bi",
                "cya_deficiency_requirement_map_bi",
            ):
                cursor.execute(
                    "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE "
                    "TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s",
                    [trigger],
                )
                (statement,) = cursor.fetchone()
                self.assertIn("item_requirement", statement, trigger)
