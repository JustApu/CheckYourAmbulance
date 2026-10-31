"""One obligation per kit instance (SPEC 1.8.0, TIME-1a; OQ-53, OQ-54; issue 92 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/92))."""

from importlib import import_module
from uuid import uuid4, uuid5

from django.apps import apps as django_apps
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from cya.checklists.models import Compartment
from cya.conditions.models import ObligationDetermination
from cya.kits.commands import record_kit_assignment
from cya.kits.models import KitAssignmentEvent
from cya.kits.tests.support import KitCase
from cya.obligations.models import Obligation, ObligationSelection, RequirementVersion
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.requirements import REQUIREMENT_NAMESPACE, requirement_id
from cya.obligations.state import NO_KIT
from cya.obligations.tests.test_projections import HistoryAssertions


class KitObligationCase(KitCase):
    """Vehicle A is bound to a daily 06:00 checklist that includes one item inside a kit."""

    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600")
        self.reference = self.add_kit("Reference").body["kit_id"]  # names the slot for the item
        self.items = [*self.base_items(), self.spec(self.kit_item, 80, kit_id=self.reference)]
        self.version_id = self.published_version(self.items)
        self.bind(reconcile=False)
        self.dues = [self.at(hours=6), self.at(days=1, hours=6)]

    def kit_rows(self, vehicle=None, as_of=None):
        return [
            o
            for o in self.obligations(vehicle=vehicle, as_of=as_of)
            if str(o.item_id) == str(self.kit_item.pk)
        ]

    def by_kit(self, **kw):
        found = {}
        for o in self.kit_rows(**kw):
            found.setdefault(str(o.kit_instance_id), []).append(o.due_at)
        return {kit: sorted(dues) for kit, dues in found.items()}


class PerInstanceTests(KitObligationCase):
    def test_two_identical_kits_each_owe_their_own_obligation(self):
        other = self.add_kit("B").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.by_kit(), {self.reference: self.dues, other: self.dues})
        non_kit = [o for o in self.obligations() if str(o.item_id) != str(self.kit_item.pk)]
        self.assertEqual({o.kit_key for o in non_kit}, {NO_KIT})
        self.assertEqual(len(non_kit), 3 * len(self.dues))  # unchanged by kits
        self.assertEqual(len(self.odometers()), len(self.dues))  # one per inspection, not per kit

    def test_with_no_instance_the_kit_item_owes_nothing_and_the_rest_is_unaffected(self):
        self.deactivate(self.reference)
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.kit_rows(), [])
        self.assertEqual(len(self.obligations()), 3 * len(self.dues))

    def test_the_reference_kit_moving_or_leaving_does_not_stop_other_instances(self):
        other = self.add_kit("B").body["kit_id"]
        shelf = self.make_slot(Compartment, "shelf")  # a different slot, same vehicle
        record_kit_assignment(
            kit_id=self.reference,
            active=True,
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(shelf.pk),
            **self.context(self.supervisor),
        )
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(set(self.by_kit()), {other})  # the reference is no longer in the slot
        self.deactivate(self.reference)
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.by_kit(), {other: self.dues})

    def test_regeneration_is_idempotent(self):
        self.add_kit("B")
        self.assertEqual(self.generate().failures, [])
        before = (
            Obligation.objects.count(),
            ObligationSelection.objects.count(),
            RequirementVersion.objects.count(),
        )
        again = self.generate()
        self.assertEqual(again.failures, [])
        self.assertEqual(
            (
                Obligation.objects.count(),
                ObligationSelection.objects.count(),
                RequirementVersion.objects.count(),
            ),
            before,
        )

    def test_a_requirement_for_a_kit_pins_its_instance_and_run(self):
        self.assertEqual(self.generate().failures, [])
        obligation = Obligation.objects.get(
            kit_instance_id=self.reference, item=self.kit_item, due_at=self.dues[0]
        )
        version = obligation.requirement_version
        first = KitAssignmentEvent.objects.get(kit_id=self.reference, sequence=1)
        self.assertEqual(
            (str(version.kit_instance_id), version.kit_key, version.kit_event_id),
            (self.reference, self.reference, first.pk),
        )
        self.assertEqual(obligation.kit_key, self.reference)

    def test_a_non_kit_requirement_id_is_unchanged_by_this_feature(self):
        pin = {
            "binding": "b",
            "conditional": None,
            "item_def": "d",
            "override": None,
            "publication": "p",
            "schedule": "s",
        }
        legacy = str(
            uuid5(
                REQUIREMENT_NAMESPACE,
                "|".join(map(str, ["o", "v", "i", "b", "-", "d", "-", "p", "s"])),
            )
        )
        self.assertEqual(requirement_id("o", "v", "i", pin), legacy)


class MidWindowTests(KitObligationCase):
    def test_an_instance_added_inside_an_open_window_is_owed_from_the_next_due_instant(self):
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=4, minutes=30))  # before the 05:00 window of the 06:00 due
        before = self.add_kit("Before").body["kit_id"]
        self.assertEqual(self.by_kit()[before], self.dues)  # added in time: owed for D
        self.clock.set(self.at(hours=5, minutes=30))  # the 06:00 early window is open
        early = self.add_kit("Early").body["kit_id"]
        self.assertEqual(self.by_kit()[early][0], self.dues[1])  # not for the open inspection
        self.assertNotIn(self.dues[0], self.by_kit()[early])
        self.clock.set(self.at(hours=6, minutes=20))  # 06:00 has passed; its window is still open
        late = self.add_kit("Late").body["kit_id"]
        owed = self.by_kit()[late]
        self.assertNotIn(self.dues[0], owed)  # not for the inspection already due
        self.assertEqual(owed[0], self.dues[1])  # but for every later one in the horizon
        self.assertEqual(self.by_kit()[self.reference][:2], self.dues)  # and the later ones

    def test_a_kit_returning_inside_an_open_window_waits_for_the_next_due_instant(self):
        extra = self.add_kit("B").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5, minutes=10))
        self.deactivate(extra)
        self.clock.set(self.at(hours=5, minutes=30))  # window of 06:00 is open
        self.move(extra, self.vehicle)
        self.assertNotIn(self.dues[0], self.by_kit()[extra])
        self.assertEqual(self.by_kit()[extra][0], self.dues[1])

    def test_a_removal_retires_only_obligations_due_after_it_even_in_window(self):
        extra = self.add_kit("B").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=5, minutes=30))  # D=06:00 is ahead and its window is open
        self.deactivate(extra)
        self.assertNotIn(extra, self.by_kit())  # both dues are after the removal
        self.assertEqual(self.by_kit()[self.reference], self.dues)
        retired = ObligationSelection.objects.filter(kit_key=extra, selection_type="RETIRE")
        self.assertEqual(retired.count(), len(self.dues))
        for event in retired:
            self.assertTrue(
                ObligationDetermination.objects.filter(
                    pk=event.supersession_determination_id, determination_type="SUPERSEDED"
                ).exists()
            )

    def test_a_removal_after_the_due_instant_keeps_that_obligation(self):
        extra = self.add_kit("B").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=6, minutes=20))  # the 06:00 inspection is already due
        self.deactivate(extra)
        self.assertEqual(self.by_kit()[extra], self.dues[:1])  # a removal cannot erase a miss

    def test_removing_and_returning_before_the_due_instant_never_revives_the_old_obligation(self):
        extra = self.add_kit("B").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        original = {o.due_at: o for o in self.kit_rows() if str(o.kit_instance_id) == extra}
        self.clock.set(self.at(hours=4))
        self.deactivate(extra)
        self.assertNotIn(extra, self.by_kit())
        self.clock.set(self.at(hours=4, minutes=30))
        self.move(extra, self.vehicle)
        returned = {o.due_at: o for o in self.kit_rows() if str(o.kit_instance_id) == extra}
        self.assertEqual(sorted(returned), self.dues)
        for due, row in returned.items():
            self.assertNotEqual(row.pk, original[due].pk)
            self.assertNotEqual(row.requirement_version_id, original[due].requirement_version_id)
        tail = ObligationSelection.objects.filter(kit_key=extra, due_at=self.dues[0]).order_by(
            "sequence"
        )
        self.assertEqual([e.selection_type for e in tail], ["SELECT", "RETIRE", "REPLACE"])

    def test_a_move_between_vehicles_is_a_removal_plus_an_addition(self):
        self.bind(vehicle=self.vehicle_b, reconcile=False)
        moving = self.add_kit("Mover").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        self.assertEqual(self.by_kit(vehicle=self.vehicle)[moving], self.dues)
        self.assertNotIn(moving, self.by_kit(vehicle=self.vehicle_b))
        self.clock.set(self.at(hours=5, minutes=30))
        self.move(moving, self.vehicle_b)
        self.assertNotIn(moving, self.by_kit(vehicle=self.vehicle))
        # B's 06:00 window is open: the arriving kit is owed from the next due instant
        self.assertEqual(self.by_kit(vehicle=self.vehicle_b)[moving], self.dues[1:])

    def test_a_temporary_absence_leaves_a_gap_with_no_backfill(self):
        extra = self.add_kit("B").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=7))  # after the first inspection's grace
        self.deactivate(extra)
        self.assertEqual(self.by_kit()[extra], self.dues[:1])
        back = self.at(days=1, hours=8)  # back after the second scheduled instant
        self.clock.set(back)
        self.move(extra, self.vehicle)
        owed = self.by_kit()[extra]
        self.assertIn(self.dues[0], owed)  # history kept
        self.assertNotIn(self.dues[1], owed)  # the inspection missed during the gap: no backfill
        self.assertTrue(all(d > back for d in owed if d != self.dues[0]))  # owed again from return
        self.assertGreater(len(owed), 1)

    def test_a_publication_replaces_pre_window_obligations_per_instance(self):
        extra = self.add_kit("B").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        before = {
            (str(o.kit_instance_id), o.due_at): o.requirement_version_id for o in self.kit_rows()
        }
        self.clock.set(self.at(hours=4))  # 06:00's early window (05:00) has not opened
        self.published_version(self.items)
        after = {
            (str(o.kit_instance_id), o.due_at): o.requirement_version_id for o in self.kit_rows()
        }
        self.assertEqual(set(after), set(before))
        self.assertEqual(len({k for k, _ in after}), 2)  # both instances still owe
        for key in after:
            self.assertNotEqual(after[key], before[key])
        self.clock.set(
            self.at(hours=5, minutes=30)
        )  # in-window now: a further publication leaves it
        keep = {k: v for k, v in after.items() if k[1] == self.dues[0]}
        self.published_version(self.items)
        again = {
            (str(o.kit_instance_id), o.due_at): o.requirement_version_id for o in self.kit_rows()
        }
        for key, requirement in keep.items():
            self.assertEqual(again[key], requirement)
        self.assertIn(extra, {k for k, _ in again})


class AsOfTests(KitObligationCase):
    def rows(self, instant):
        return get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            instant,
            organization_id=str(self.org.pk),
            start=self.at(),
            end=self.at(days=3),
        )

    def test_history_reflects_only_what_was_recorded_by_then(self):
        self.assertEqual(self.generate().failures, [])
        before = self.clock.now
        self.clock.set(self.at(hours=1))
        extra = self.add_kit("B").body["kit_id"]
        after = self.clock.now
        kits_before = {
            r.obligation.kit_instance_id
            for r in self.rows(before)
            if r.obligation.item_id == str(self.kit_item.pk)
        }
        kits_after = {
            r.obligation.kit_instance_id
            for r in self.rows(after)
            if r.obligation.item_id == str(self.kit_item.pk)
        }
        self.assertEqual(kits_before, {self.reference})
        self.assertEqual(kits_after, {self.reference, extra})
        self.assertEqual(
            {
                r.obligation.kit_instance_id
                for r in self.rows(before)
                if r.obligation.item_id == str(self.kit_item.pk)
            },
            kits_before,
        )


class GuardTests(KitObligationCase):
    def setUp(self):
        super().setUp()
        self.extra = self.add_kit("B").body["kit_id"]
        self.assertEqual(self.generate().failures, [])
        self.row = Obligation.objects.filter(kit_instance_id=self.reference).first()

    def refused(self, operation, text=None):
        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            operation()
        if text:
            self.assertIn(text, str(caught.exception))

    def clone(self, row, **overrides):
        fields = {f.attname: getattr(row, f.attname) for f in row._meta.concrete_fields}
        fields.update(id=str(uuid4()), **overrides)
        return type(row).objects.create(**fields)

    def test_the_kit_key_and_the_kit_instance_must_agree(self):
        # Rows outside a kit, so no foreign key or trigger refuses them before the CHECK does.
        plain = Obligation.objects.filter(kit_key=NO_KIT).first()
        self.refused(
            lambda: self.clone(plain, kit_instance_id=self.reference), "obligation_kit_key"
        )
        plain_version = RequirementVersion.objects.filter(kit_key=NO_KIT).first()
        self.refused(lambda: self.clone(plain_version, kit_key=str(uuid4())), "reqversion_kit_key")

    def test_two_obligations_cannot_differ_only_by_a_missing_instance(self):
        plain = Obligation.objects.filter(kit_key=NO_KIT).first()
        self.refused(lambda: self.clone(plain))  # same natural key, new id

    def test_a_requirement_for_a_kit_item_must_name_a_kit_instance(self):
        version = RequirementVersion.objects.filter(kit_instance_id=self.reference).first()
        self.refused(
            lambda: self.clone(version, kit_instance_id=None, kit_key=NO_KIT, kit_event_id=None),
            "must name a kit instance",
        )

    def test_a_requirement_cannot_pin_an_event_from_another_vehicle_or_kit(self):
        version = RequirementVersion.objects.filter(kit_instance_id=self.reference).first()
        elsewhere = self.add_kit("Elsewhere", vehicle=self.vehicle_b).body["event_id"]
        self.refused(
            lambda: self.clone(version, kit_event_id=elsewhere), "not active on the vehicle"
        )
        theirs = KitAssignmentEvent.objects.get(kit_id=self.extra).pk
        self.refused(lambda: self.clone(version, kit_event_id=theirs))

    def test_an_obligation_cannot_name_another_instances_requirement(self):
        version = RequirementVersion.objects.filter(kit_instance_id=self.extra).first()
        self.refused(lambda: self.clone(self.row, requirement_version_id=version.pk))

    def test_a_selection_cannot_select_an_obligation_of_another_instance(self):
        event = ObligationSelection.objects.filter(kit_key=self.extra, sequence=1).first()
        mine = Obligation.objects.filter(
            kit_instance_id=self.reference, due_at=event.due_at
        ).first()
        self.refused(lambda: self.clone(event, selected_obligation_id=mine.pk))


class MigrationTests(KitObligationCase):
    def check_record_schema(self):
        """The partial rollback must restore every check key, index and append-only trigger."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='check_record' "
                "ORDER BY CONSTRAINT_NAME,ORDINAL_POSITION"
            )
            keys = cursor.fetchall()
            cursor.execute(
                "SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='check_record' "
                "ORDER BY INDEX_NAME,SEQ_IN_INDEX"
            )
            indexes = cursor.fetchall()
            cursor.execute(
                "SELECT TRIGGER_NAME,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT "
                "FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='check_record' "
                "ORDER BY TRIGGER_NAME"
            )
            triggers = cursor.fetchall()
        return keys, indexes, triggers

    def test_reversing_refuses_before_changing_anything_when_kit_rows_exist(self):
        self.add_kit("B")
        self.assertEqual(self.generate().failures, [])
        leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
        before = self.check_record_schema()
        try:
            with self.assertRaises(RuntimeError) as caught:
                MigrationExecutor(connection).migrate(
                    [("cya_obligations", "0002_obligation_guards")]
                )
            self.assertIn("nothing was changed", str(caught.exception))
        finally:
            MigrationExecutor(connection).migrate(leaves)
        self.assertEqual(self.check_record_schema(), before)
        applied = MigrationExecutor(connection).loader.applied_migrations
        self.assertIn(("cya_obligations", "0003_kit_instance_anchor"), applied)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='obligation' AND COLUMN_NAME='kit_key'"
            )
            self.assertEqual(cursor.fetchone()[0], 1)
        self.assertEqual(
            set(self.by_kit()), {self.reference, *[k for k in self.by_kit() if k != self.reference]}
        )
        self.assertGreaterEqual(len(self.by_kit()), 2)


class KitMigrationTests(KitCase):
    def test_kits_that_predate_assignment_history_are_refused_not_dropped(self):
        migration = import_module("cya.kits.migrations.0001_initial")
        self.add_kit("Existing")

        class Editor:
            pass

        editor = Editor()
        editor.connection = connection
        with self.assertRaisesRegex(RuntimeError, "nothing was changed"):
            migration.refuse_unrecorded_kits(django_apps, editor)


class KitHistoryTests(HistoryAssertions, KitCase):
    """AC-26 and AC-27 over histories in which kit instances are added, removed and returned."""

    def test_replay_matches_live_and_the_past_survives_later_kit_changes(self):
        self.assert_history(2610, kits=True)
        kits = {o.kit_instance_id for o in self.kit_rows() if o.kit_instance_id}
        self.assertGreaterEqual(len(kits), 2)  # the history really had several instances
        self.assertGreater(KitAssignmentEvent.objects.count(), 3)

    def test_another_seed(self):
        self.assert_history(7001, kits=True)

    def kit_rows(self):
        return [
            o
            for o in self.obligations(end=self.at(days=40))
            if str(o.item_id) == str(self.kit_item.pk)
        ]
