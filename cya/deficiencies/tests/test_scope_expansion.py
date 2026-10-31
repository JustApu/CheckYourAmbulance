"""Supervisor scope expansion and added verification after closure (issue 156 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/156), SPEC 1.22.0)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from cya.checklists.models import Compartment, Item
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.deficiencies.closure import close_administrator
from cya.deficiencies.commands import assign_deficiency, set_deadline
from cya.deficiencies.expansion import add_verification, expand_scope
from cya.deficiencies.models import (
    DeficiencyEvent,
    DeficiencyProjection,
    DeficiencyScopeExpansion,
    VerificationScopeRow,
)
from cya.deficiencies.service import DeficiencyError, assignment_state, extension_state
from cya.deficiencies.tests.test_verification import VerificationBase
from cya.kits.commands import add_kit_instance, record_kit_assignment

NO_KIT = "00000000-0000-0000-0000-000000000000"
REASON = "Synthetic reason to check more items"


class ExpansionBase(VerificationBase):
    def setUp(self):
        super().setUp()
        # An item on the vehicle's checklist outside the cabinet, so an expansion can reach past
        # the deficiency's own compartment target.
        self.version_id = self.published_version(
            [
                *self.items,
                self.spec(self.stray, 80, compartment_id=None, system_id=str(self.system.pk)),
            ]
        )

    def compartment_pending(self):
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        return self.pending({"compartment_id": str(compartment.pk)})

    def expand(self, deficiency_id, items=None, actor=None, **fields):
        context = {**self.context(actor or self.supervisor), **fields.pop("context", {})}
        return expand_scope(
            deficiency_id=deficiency_id,
            expected_event_id=fields.pop(
                "expected_event_id", str(self.last_event(deficiency_id).pk)
            ),
            explanation=fields.pop("explanation", REASON),
            items=items if items is not None else [{"item_id": str(self.stray.pk)}],
            **context,
        )

    def add(self, deficiency_id, items=None, actor=None, deadline=..., **fields):
        context = {**self.context(actor or self.supervisor), **fields.pop("context", {})}
        if deadline is ...:
            deadline = timezone.now() + timedelta(days=7)
        return add_verification(
            deficiency_id=deficiency_id,
            expected_event_id=fields.pop(
                "expected_event_id", str(self.last_event(deficiency_id).pk)
            ),
            explanation=fields.pop("explanation", REASON),
            items=items if items is not None else [{"item_id": str(self.masks.pk)}],
            deadline=deadline,
            **context,
        )

    def rows(self, deficiency_id, revision):
        return {
            (str(r.item_id), r.instance_key): r
            for r in VerificationScopeRow.objects.filter(
                deficiency_id=deficiency_id, scope_revision=revision
            )
        }

    def refused_without_writes(self, code, call):
        before = self.counts(), DeficiencyScopeExpansion.objects.count()
        with self.assertRaises(DeficiencyError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.counts(), DeficiencyScopeExpansion.objects.count()), before)
        return caught.exception


class PendingExpansionTests(ExpansionBase):
    def test_expansion_keeps_earlier_passes_and_needs_the_added_row(self):
        deficiency_id = self.compartment_pending()
        for item in (self.suction, self.masks, self.gauge, self.special):
            self.verify(deficiency_id, item)
        before = self.rows(deficiency_id, 1)
        result = self.expand(deficiency_id)
        event = self.last_event(deficiency_id)
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status, event.explanation),
            ("SCOPE_EXPANDED", "PENDING_VERIFICATION", "PENDING_VERIFICATION", REASON),
        )
        self.assertEqual(event.actor_user_id, self.supervisor.pk)
        self.assertEqual(
            (result.body["scope_revision"], result.body["scope_count"], result.body["status"]),
            (2, 6, "PENDING_VERIFICATION"),
        )
        after = self.rows(deficiency_id, 2)
        # Every earlier row repeats with its own instant and correction; the added row is new.
        for key, row in before.items():
            self.assertEqual(after[key].required_after, row.required_after)
            self.assertEqual(after[key].correction_event_id, row.correction_event_id)
        added = after[(str(self.stray.pk), NO_KIT)]
        self.assertEqual(added.required_after, event.recorded_at)
        stored = DeficiencyScopeExpansion.objects.get()
        self.assertEqual((stored.event_id, str(stored.item_id)), (event.pk, str(self.stray.pk)))
        # The four earlier Passes still count: only the last original item and the added one remain.
        last = self.verify(deficiency_id, self.limited)
        self.assertEqual(last.body["verification"]["remaining_scope_rows"], 1)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.verify(deficiency_id, self.stray)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_a_new_correction_keeps_the_expansion_but_carries_no_pass(self):
        deficiency_id = self.compartment_pending()
        self.expand(deficiency_id)
        self.verify(deficiency_id, self.masks)
        self.verify(deficiency_id, self.suction, "FAILED")
        self.assertEqual(self.status(deficiency_id), "IN_PROGRESS")
        self.correct(deficiency_id)
        rows = self.rows(deficiency_id, 3)
        self.assertIn((str(self.stray.pk), NO_KIT), rows)
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({r.correction_event_id for r in rows.values()}), 1)
        first = self.verify(deficiency_id, self.gauge)
        # Nothing from the earlier correction counts: five rows are still open.
        self.assertEqual(first.body["verification"]["remaining_scope_rows"], 5)

    def test_an_item_outside_a_compartment_target_is_kept_for_an_item_deficiency_too(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.expand(deficiency_id, [{"item_id": str(self.masks.pk)}])
        self.verify(deficiency_id, self.suction)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.verify(deficiency_id, self.masks)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_replay_returns_the_stored_result_and_writes_nothing(self):
        deficiency_id = self.compartment_pending()
        operation_id = str(uuid4())
        first = self.expand(deficiency_id, context={"operation_id": operation_id})
        counts = self.counts(), DeficiencyScopeExpansion.objects.count()
        again = self.expand(
            deficiency_id,
            context={"operation_id": operation_id},
            expected_event_id=str(self.last_event(deficiency_id).predecessor_event_id),
        )
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual((self.counts(), DeficiencyScopeExpansion.objects.count()), counts)

    def test_refusals_write_nothing(self):
        deficiency_id = self.compartment_pending()
        other = Item.objects.create(organization=self.org, label="Synthetic off checklist")
        cases = (
            ("ALREADY_IN_SCOPE", {"items": [{"item_id": str(self.masks.pk)}]}),
            ("ITEM_NOT_IN_CHECKLIST", {"items": [{"item_id": str(other.pk)}]}),
            (
                "KIT_NOT_ALLOWED",
                {"items": [{"item_id": str(self.stray.pk), "kit_instance_id": str(uuid4())}]},
            ),
            ("DEFICIENCY_CHANGED", {"expected_event_id": str(uuid4())}),
        )
        for code, fields in cases:
            items = fields.pop("items", None)
            with self.subTest(code=code):
                self.refused_without_writes(
                    code, lambda i=items, f=fields: self.expand(deficiency_id, i, **dict(f))
                )
        for items in ([], [{"item_id": str(self.stray.pk)}] * 2, [{"kit_instance_id": NO_KIT}]):
            with self.subTest(items=items), self.assertRaises(DeficiencyError) as caught:
                self.expand(deficiency_id, items)
            self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        with self.assertRaises(DeficiencyError) as caught:
            self.expand(deficiency_id, explanation=" ")
        self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")

    def test_only_a_pending_deficiency_can_be_expanded(self):
        open_one = self.report({"item_id": str(self.suction.pk)})
        self.refused_without_writes("INVALID_TRANSITION", lambda: self.expand(open_one))
        closed = self.pending({"item_id": str(self.suction.pk)})
        self.verify(closed)
        error = self.refused_without_writes("INVALID_TRANSITION", lambda: self.expand(closed))
        self.assertIn("Pending Verification", str(error))

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        deficiency_id = self.compartment_pending()
        for actor in (self.crew, self.supply):
            before = self.counts()
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.expand(deficiency_id, actor=actor)
            self.assertEqual(self.counts(), before)
        self.assertEqual(
            AuditEvent.objects.filter(action="DEFICIENCY_SCOPE_EXPAND", outcome="DENIED").count(),
            2,
        )
        self.expand(deficiency_id, actor=self.admin)  # Administrators hold the permission too
        self.assertEqual(self.last_event(deficiency_id).event_type, "SCOPE_EXPANDED")


class KitExpansionTests(ExpansionBase):
    def setUp(self):
        super().setUp()
        self.kit_item = Item.objects.create(organization=self.org, label="Synthetic kit item")
        self.kit = add_kit_instance(
            label="Synthetic physical kit",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            **self.context(self.supervisor),
        ).body["kit_id"]
        self.version_id = self.published_version(
            [*self.items, self.spec(self.kit_item, 90, kit_id=self.kit)]
        )
        self.drawer = self.make_slot(Compartment, "drawer")

    def move_kit(self):
        record_kit_assignment(
            kit_id=self.kit,
            active=True,
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.drawer.pk),
            **self.context(self.supervisor),
        )

    def test_a_moved_expanded_kit_drops_out_of_a_later_correction(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.expand(
            deficiency_id, [{"item_id": str(self.kit_item.pk), "kit_instance_id": self.kit}]
        )
        self.verify(deficiency_id, self.suction, "FAILED")
        self.move_kit()  # still active on the vehicle, but no longer holds the item's slot
        self.correct(deficiency_id)  # resolved like any correction-time row: not refused
        self.assertEqual(set(self.rows(deficiency_id, 3)), {(str(self.suction.pk), NO_KIT)})
        self.verify(deficiency_id, self.suction)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_a_pending_expansion_repeats_a_row_whose_kit_has_moved(self):
        deficiency_id = self.pending({"item_id": str(self.kit_item.pk)})
        self.assertIn((str(self.kit_item.pk), self.kit), self.rows(deficiency_id, 1))
        self.move_kit()
        self.expand(deficiency_id, [{"item_id": str(self.masks.pk)}])
        rows = self.rows(deficiency_id, 2)
        # Kept, not dropped: an expansion never shrinks the scope.
        self.assertEqual(
            set(rows), {(str(self.kit_item.pk), self.kit), (str(self.masks.pk), NO_KIT)}
        )

    def test_the_database_refuses_an_expansion_kit_from_another_slot(self):
        other = add_kit_instance(
            label="Synthetic kit elsewhere",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.drawer.pk),
            **self.context(self.supervisor),
        ).body["kit_id"]
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.verify(deficiency_id)
        original = DeficiencyScopeExpansion.objects.bulk_create

        def elsewhere(rows, *rest, **kwargs):
            rows = list(rows)
            rows[0].kit_instance_id = other
            rows[0].instance_key = other
            return original(rows, *rest, **kwargs)

        with (
            mock.patch(
                "cya.deficiencies.expansion.DeficiencyScopeExpansion.objects.bulk_create",
                side_effect=elsewhere,
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.add(
                deficiency_id, [{"item_id": str(self.kit_item.pk), "kit_instance_id": self.kit}]
            )
        self.assertIn("scope expansion kit instance does not fit the item", str(caught.exception))
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_a_kit_item_names_its_physical_instance(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        item = str(self.kit_item.pk)
        self.refused_without_writes(
            "KIT_REQUIRED", lambda: self.expand(deficiency_id, [{"item_id": item}])
        )
        self.refused_without_writes(
            "KIT_REQUIRED",
            lambda: self.expand(deficiency_id, [{"item_id": item, "kit_instance_id": NO_KIT}]),
        )
        self.expand(deficiency_id, [{"item_id": item, "kit_instance_id": self.kit}])
        self.assertIn((item, self.kit), self.rows(deficiency_id, 2))
        self.verify(deficiency_id, self.suction)
        self.verify(deficiency_id, self.kit_item, kit_id=self.kit)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")


class AddedVerificationTests(ExpansionBase):
    def closed(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        assign_deficiency(
            deficiency_id=deficiency_id,
            assignee_id=str(self.crew.pk),
            expected_event_id=str(self.last_event(deficiency_id).pk),
            **self.context(self.supervisor),
        )
        set_deadline(
            deficiency_id=deficiency_id,
            deadline=timezone.now() + timedelta(days=2),
            expected_event_id=str(self.last_event(deficiency_id).pk),
            **self.context(self.supervisor),
        )
        self.work(deficiency_id)
        self.correct(deficiency_id)
        self.verify(deficiency_id)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        return deficiency_id

    def test_adding_verification_reopens_with_a_new_deadline_and_keeps_the_assignee(self):
        deficiency_id = self.closed()
        former = DeficiencyProjection.objects.get(deficiency_id=deficiency_id).deadline
        closed_at = self.last_event(deficiency_id).recorded_at
        deadline = timezone.now() + timedelta(days=9)
        result = self.add(deficiency_id, deadline=deadline)
        event = self.last_event(deficiency_id)
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status, event.deadline),
            ("VERIFICATION_ADDED", "CLOSED_VERIFIED", "OPEN", deadline),
        )
        self.assertEqual(
            (result.body["status"], result.body["assignee_id"]), ("OPEN", str(self.crew.pk))
        )
        projection = DeficiencyProjection.objects.get(deficiency_id=deficiency_id)
        self.assertEqual(
            (
                projection.status,
                projection.deadline,
                projection.assignee_id,
                projection.last_applied_event_id,
            ),
            ("OPEN", deadline, self.crew.pk, event.pk),
        )
        # The former deadline stays in history; an as-of read before the reopening shows it.
        state = assignment_state(str(self.org.pk), deficiency_id)
        self.assertEqual((state["deadline"], state["deficiency_status"]), (deadline, "OPEN"))
        self.assertEqual([h["deadline"] for h in state["history"]][-2:], [former, deadline])
        earlier = assignment_state(str(self.org.pk), deficiency_id, as_of=closed_at)
        self.assertEqual(
            (earlier["deadline"], earlier["deficiency_status"]), (former, "CLOSED_VERIFIED")
        )
        self.assertEqual(
            extension_state(str(self.org.pk), deficiency_id)["governing_deadline"], deadline
        )
        # The added item is verified after the next correction, with the original target.
        self.work(deficiency_id)
        self.correct(deficiency_id)
        rows = self.rows(deficiency_id, 2)
        self.assertEqual(set(rows), {(str(self.suction.pk), NO_KIT), (str(self.masks.pk), NO_KIT)})
        self.verify(deficiency_id, self.suction)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.verify(deficiency_id, self.masks)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")

    def test_replay_and_refusals(self):
        deficiency_id = self.closed()
        self.refused_without_writes(
            "INVALID_DEADLINE",
            lambda: self.add(deficiency_id, deadline=timezone.now() - timedelta(hours=1)),
        )
        with self.assertRaises(DeficiencyError) as caught:
            self.add(deficiency_id, deadline=None)
        self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        self.refused_without_writes(
            "DEFICIENCY_CHANGED", lambda: self.add(deficiency_id, expected_event_id=str(uuid4()))
        )
        with self.assertRaises(CommandDenied):
            self.add(deficiency_id, actor=self.crew)
        operation_id = str(uuid4())
        deadline = timezone.now() + timedelta(days=3)
        first = self.add(deficiency_id, deadline=deadline, context={"operation_id": operation_id})
        counts = self.counts()
        again = self.add(
            deficiency_id,
            deadline=deadline,
            context={"operation_id": operation_id},
            expected_event_id=str(self.last_event(deficiency_id).predecessor_event_id),
        )
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.counts(), counts)

    def test_only_a_verified_closure_takes_added_verification(self):
        pending = self.pending({"item_id": str(self.suction.pk)})
        self.refused_without_writes("INVALID_TRANSITION", lambda: self.add(pending))
        closed = self.report({"item_id": str(self.suction.pk)})
        close_administrator(
            deficiency_id=closed,
            explanation="Synthetic explicit closure",
            expected_event_id=str(self.last_event(closed).pk),
            **self.context(self.admin),
        )
        self.refused_without_writes("INVALID_TRANSITION", lambda: self.add(closed))


class RaceTests(ExpansionBase):
    def hold(self, path, seconds=1.0):
        """Patch path so the call returns and then holds its transaction's locks for a while."""
        original = path_target(path)

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            time.sleep(seconds)
            return result

        return mock.patch(path, side_effect=slow)

    def run_ordered(self, first, second):
        """first takes its locks, then second starts and waits for them (two connections)."""
        started = threading.Event()

        def run(job, wait):
            close_old_connections()
            try:
                if wait:
                    started.wait(timeout=10)
                    time.sleep(0.3)
                else:
                    started.set()
                return job()
            except Exception as error:  # returned to the asserting thread
                return error
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(run, first, False)
            b = pool.submit(run, second, True)
            return a.result(timeout=120), b.result(timeout=120)

    def nearly_verified(self):
        deficiency_id = self.compartment_pending()
        for item in (self.suction, self.masks, self.gauge, self.special):
            self.verify(deficiency_id, item)
        return deficiency_id, str(self.last_event(deficiency_id).pk)

    def test_expansion_first_the_last_old_pass_cannot_close(self):
        deficiency_id, seen = self.nearly_verified()
        with self.hold("cya.deficiencies.expansion.resolve_items"):
            expanded, passed = self.run_ordered(
                lambda: self.expand(deficiency_id, expected_event_id=seen),
                lambda: self.verify(deficiency_id, self.limited),
            )
        self.assertFalse(isinstance(expanded, Exception), expanded)
        self.assertFalse(isinstance(passed, Exception), passed)
        self.assertEqual(passed.body["verification"]["scope_revision"], 2)
        self.assertEqual(passed.body["verification"]["remaining_scope_rows"], 1)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")

    def test_closure_first_the_stale_expansion_is_refused_not_turned_into_a_reopening(self):
        deficiency_id, seen = self.nearly_verified()
        with self.hold("cya.checks.commands._verification_plan"):
            passed, expanded = self.run_ordered(
                lambda: self.verify(deficiency_id, self.limited),
                lambda: self.expand(deficiency_id, expected_event_id=seen),
            )
        self.assertFalse(isinstance(passed, Exception), passed)
        self.assertIsInstance(expanded, DeficiencyError)
        self.assertEqual((expanded.code, expanded.status), ("DEFICIENCY_CHANGED", 409))
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.assertNotIn("SCOPE_EXPANDED", self.events(deficiency_id))
        self.assertFalse(DeficiencyScopeExpansion.objects.exists())


def path_target(path):
    module, name = path.rsplit(".", 1)
    return getattr(__import__(module, fromlist=[name]), name)


class DatabaseGuardTests(ExpansionBase):
    def test_the_event_needs_a_supervisor_or_administrator(self):
        original = DeficiencyEvent.objects.create

        def crew(**fields):
            return original(**{**fields, "actor_roles_at_time": ["CREW"]})

        deficiency_id = self.compartment_pending()
        with (
            mock.patch(
                "cya.deficiencies.expansion.DeficiencyEvent.objects.create", side_effect=crew
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.expand(deficiency_id)
        self.assertIn("a supervisor or administrator", str(caught.exception))
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        self.assertFalse(DeficiencyScopeExpansion.objects.exists())

    def test_a_repeated_row_keeps_its_instant(self):
        original = VerificationScopeRow.objects.bulk_create

        def wrapper(rows, *rest, **kwargs):
            rows = list(rows)
            carried = [r for r in rows if r.required_after != r.recorded_at]
            carried[0].required_after += timedelta(seconds=1)
            return original(rows, *rest, **kwargs)

        deficiency_id = self.compartment_pending()
        with (
            mock.patch(
                "cya.deficiencies.expansion.VerificationScopeRow.objects.bulk_create",
                side_effect=wrapper,
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.expand(deficiency_id)
        self.assertIn("verification scope time or revision is invalid", str(caught.exception))

    def test_a_closure_needs_every_earlier_row_repeated(self):
        original = VerificationScopeRow.objects.bulk_create

        def shrink(rows, *rest, **kwargs):
            rows = [r for r in rows if str(r.item_id) != str(self.limited.pk)]
            return original(rows, *rest, **kwargs)

        deficiency_id = self.compartment_pending()
        with mock.patch(
            "cya.deficiencies.expansion.VerificationScopeRow.objects.bulk_create",
            side_effect=shrink,
        ):
            self.expand(deficiency_id)  # rows are valid one by one; the gap shows at closure
        for item in (self.suction, self.masks, self.gauge, self.special):
            self.verify(deficiency_id, item)
        with self.assertRaises(DatabaseError) as caught:
            self.verify(deficiency_id, self.stray)
        self.assertIn("every scope row satisfied", str(caught.exception))
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")

    def test_an_expansion_row_must_match_its_kit_key(self):
        original = DeficiencyScopeExpansion.objects.bulk_create

        def wrong_key(rows, *rest, **kwargs):
            rows = list(rows)
            rows[0].instance_key = str(uuid4())
            return original(rows, *rest, **kwargs)

        deficiency_id = self.compartment_pending()
        with (
            mock.patch(
                "cya.deficiencies.expansion.DeficiencyScopeExpansion.objects.bulk_create",
                side_effect=wrong_key,
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.expand(deficiency_id)
        self.assertIn("scope expansion instance key mismatch", str(caught.exception))


class MigrationTests(TransactionTestCase):
    def trigger(self, name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s",
                [name],
            )
            row = cursor.fetchone()
        return None if row is None else row[0]

    def test_reversal_restores_the_previous_triggers_and_forward_reinstalls_them(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        names = ("cya_deficiency_event_bi", "cya_verification_scope_row_bi")
        before = [self.trigger(n) for n in names]
        self.assertIn("SCOPE_EXPANDED", before[0])
        target = [
            ("cya_deficiencies", "0015_verification_reopen")
            if leaf[0] == "cya_deficiencies"
            else leaf
            for leaf in leaves
        ]
        try:
            executor.migrate(target)
            self.assertNotIn("SCOPE_EXPANDED", self.trigger(names[0]))
            self.assertNotIn("DEFICIENCY_SCOPE_EXPAND", self.trigger(names[1]))
            self.assertIsNone(self.trigger("cya_deficiency_scope_expansion_bi"))
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(before, [self.trigger(n) for n in names])
        finally:
            MigrationExecutor(connection).migrate(leaves)
