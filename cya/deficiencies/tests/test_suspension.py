"""Supervisor check suspension for a known deficiency (issue 13
(https://github.com/JustApu/CheckYourAmbulance/issues/13), SPEC 8.9 SUS-1 to SUS-4, AC-18)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from cya.attention.tests.support import AttentionCase
from cya.checklists.models import Compartment, Item
from cya.checks.commands import correct_check, invalidate_chain
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.deficiencies import service
from cya.deficiencies.closure import close_administrator, close_empty_scope, reopen_administrator
from cya.deficiencies.models import CheckSuspension, DeficiencyProjection
from cya.deficiencies.service import DeficiencyError, assignment_state
from cya.deficiencies.suspension import suspend_checks, suspension_state
from cya.deficiencies.tests.test_verification import VerificationBase
from cya.kits.commands import add_kit_instance
from cya.obligations.projections import get_vehicle_obligations_as_of, load_projection_history
from cya.obligations.state import State, rebuild_vehicle_projection
from cya.operations.models import VehicleStatusEvent

NO_KIT = "00000000-0000-0000-0000-000000000000"
REASON = "Synthetic: the unit is out for repair; do not check it until it returns."
CLOSE = "Synthetic administrative closure."


class SuspensionMixin:
    def suspend(self, deficiency_id, items=None, actor=None, **fields):
        context = {**self.context(actor or self.supervisor), **fields.pop("context", {})}
        return suspend_checks(
            deficiency_id=deficiency_id,
            expected_event_id=fields.pop(
                "expected_event_id", str(self.last_event(deficiency_id).pk)
            ),
            items=items if items is not None else [{"item_id": str(self.suction.pk)}],
            **fields,
            **context,
        )

    def starts(self, deficiency_id):
        return list(
            CheckSuspension.objects.filter(deficiency_id=deficiency_id, action="START").order_by(
                "recorded_at", "id"
            )
        )

    def ends(self, deficiency_id):
        return list(CheckSuspension.objects.filter(deficiency_id=deficiency_id, action="END"))

    def in_force(self, deficiency_id, as_of=None):
        return suspension_state(str(self.org.pk), deficiency_id, as_of)["in_force"]

    def close(self, deficiency_id, actor=None, **fields):
        return close_administrator(
            deficiency_id=deficiency_id,
            expected_event_id=str(self.last_event(deficiency_id).pk),
            explanation=CLOSE,
            **{**self.context(actor or self.admin), **fields},
        )

    def cabinet_target(self):
        """A compartment target whose scope holds suction, masks and the cabinet's other items."""
        compartment = Compartment.objects.get(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        return {"compartment_id": str(compartment.pk)}

    def suspension_counts(self):
        return self.counts(), CheckSuspension.objects.count()

    def refused_without_writes(self, code, call):
        before = self.suspension_counts()
        with self.assertRaises(DeficiencyError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.suspension_counts(), before)
        return caught.exception


class StartTests(SuspensionMixin, VerificationBase):
    def test_a_suspension_is_an_event_and_start_rows_and_changes_nothing_else(self):
        deficiency_id = self.report(self.cabinet_target())
        before = DeficiencyProjection.objects.get(deficiency_id=deficiency_id)
        statuses = VehicleStatusEvent.objects.count()
        result = self.suspend(
            deficiency_id,
            [{"item_id": str(self.suction.pk)}, {"item_id": str(self.masks.pk)}],
            explanation=REASON,
        )
        event = self.last_event(deficiency_id)
        self.assertEqual(
            (event.event_type, event.from_status, event.to_status, event.explanation),
            ("CHECKS_SUSPENDED", "OPEN", "OPEN", REASON),
        )
        self.assertEqual((event.actor_user_id, event.suspensions_ended), (self.supervisor.pk, []))
        starts = self.starts(deficiency_id)
        self.assertEqual(
            {(str(s.item_id), s.instance_key, s.kit_instance_id) for s in starts},
            {(str(self.suction.pk), NO_KIT, None), (str(self.masks.pk), NO_KIT, None)},
        )
        for start in starts:
            self.assertEqual(
                (start.deficiency_event_id, start.vehicle_id, start.prior_suspension_id),
                (event.pk, self.vehicle.pk, None),
            )
        self.assertEqual(sorted(result.body["suspension_ids"]), sorted(str(s.pk) for s in starts))
        self.assertEqual(result.body["status"], "OPEN")
        # Only the cache's last applied event moves; no status, assignee or deadline, and the
        # vehicle's operational status is untouched (SUS-2, OPS-2).
        after = DeficiencyProjection.objects.get(deficiency_id=deficiency_id)
        self.assertEqual(
            (after.status, after.assignee_id, after.deadline, after.last_applied_event_id),
            (before.status, before.assignee_id, before.deadline, event.pk),
        )
        self.assertEqual(VehicleStatusEvent.objects.count(), statuses)
        audit = AuditEvent.objects.get(action="DEFICIENCY_CHECKS_SUSPEND", outcome="SUCCEEDED")
        self.assertNotIn("Synthetic: the unit", str(audit.new_snapshot))  # identifiers only
        self.assertEqual(
            audit.new_snapshot["values"]["suspension_ids"], sorted(str(s.pk) for s in starts)
        )
        self.assertEqual(sorted(self.in_force(deficiency_id)), sorted(str(s.pk) for s in starts))

    def test_every_active_status_may_be_suspended(self):
        pending = self.pending({"item_id": str(self.suction.pk)})
        self.suspend(pending)
        self.assertEqual(self.last_event(pending).to_status, "PENDING_VERIFICATION")
        working = self.report({"item_id": str(self.masks.pk)})
        self.work(working)
        self.suspend(working, [{"item_id": str(self.masks.pk)}])  # explanation is optional
        self.assertEqual(self.last_event(working).to_status, "IN_PROGRESS")
        self.assertIsNone(self.last_event(working).explanation)

    def test_refusals_write_nothing(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        other = Item.objects.create(organization=self.org, label="Synthetic off checklist")
        cases = (
            ("ALREADY_SUSPENDED", {"items": [{"item_id": str(self.suction.pk)}]}),
            ("ITEM_NOT_IN_CHECKLIST", {"items": [{"item_id": str(other.pk)}]}),
            (
                "KIT_NOT_ALLOWED",
                {"items": [{"item_id": str(self.masks.pk), "kit_instance_id": str(uuid4())}]},
            ),
            ("DEFICIENCY_CHANGED", {"expected_event_id": str(uuid4())}),
        )
        for code, fields in cases:
            items = fields.pop("items", [{"item_id": str(self.masks.pk)}])
            with self.subTest(code=code):
                self.refused_without_writes(
                    code, lambda i=items, f=fields: self.suspend(deficiency_id, i, **dict(f))
                )
        for items in ([], [{"item_id": str(self.masks.pk)}] * 2, [{"kit_instance_id": NO_KIT}]):
            with self.subTest(items=items), self.assertRaises(DeficiencyError) as caught:
                self.suspend(deficiency_id, items)
            self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        with self.assertRaises(DeficiencyError) as caught:
            self.suspend(deficiency_id, [{"item_id": str(self.masks.pk)}], explanation=" ")
        self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        self.close(deficiency_id)
        self.refused_without_writes(
            "DEFICIENCY_NOT_ACTIVE",
            lambda: self.suspend(deficiency_id, [{"item_id": str(self.masks.pk)}]),
        )

    def test_crew_and_supply_officers_are_refused_and_audited(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        for actor in (self.crew, self.supply):
            before = self.suspension_counts()
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.suspend(deficiency_id, actor=actor)
            self.assertEqual(self.suspension_counts(), before)
        self.assertEqual(
            AuditEvent.objects.filter(action="DEFICIENCY_CHECKS_SUSPEND", outcome="DENIED").count(),
            2,
        )
        self.suspend(deficiency_id, actor=self.admin)  # Administrators hold the permission too
        self.assertEqual(len(self.in_force(deficiency_id)), 1)

    def test_replay_returns_the_stored_result_and_writes_nothing(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        operation_id = str(uuid4())
        first = self.suspend(deficiency_id, context={"operation_id": operation_id})
        counts = self.suspension_counts()
        again = self.suspend(
            deficiency_id,
            context={"operation_id": operation_id},
            expected_event_id=str(self.last_event(deficiency_id).predecessor_event_id),
        )
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.suspension_counts(), counts)

    def test_the_number_in_force_is_bounded(self):
        deficiency_id = self.report(self.cabinet_target())
        with mock.patch.object(service, "MAX_SUSPENSIONS_IN_FORCE", 1):
            self.suspend(deficiency_id)
            self.refused_without_writes(
                "TOO_MANY_SUSPENSIONS",
                lambda: self.suspend(deficiency_id, [{"item_id": str(self.masks.pk)}]),
            )

    def test_only_items_in_the_affected_scope_can_be_suspended(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        error = self.refused_without_writes(
            "ITEM_NOT_IN_SCOPE",
            lambda: self.suspend(
                deficiency_id,
                [{"item_id": str(self.suction.pk)}, {"item_id": str(self.masks.pk)}],
            ),
        )
        self.assertEqual(error.path, "items[1]")
        self.assertEqual(len(self.starts(deficiency_id)), 0)
        self.suspend(deficiency_id)  # the item the deficiency names is in scope
        self.assertEqual(len(self.in_force(deficiency_id)), 1)

    def test_another_deficiency_may_suspend_the_same_item(self):
        first = self.report({"item_id": str(self.suction.pk)})
        second = self.report({"item_id": str(self.suction.pk)})
        self.suspend(first)
        self.suspend(second)
        self.assertEqual((len(self.in_force(first)), len(self.in_force(second))), (1, 1))


class KitStartTests(SuspensionMixin, VerificationBase):
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

    def test_a_kit_item_names_its_physical_instance(self):
        deficiency_id = self.report(self.cabinet_target())
        item = str(self.kit_item.pk)
        self.refused_without_writes(
            "KIT_REQUIRED", lambda: self.suspend(deficiency_id, [{"item_id": item}])
        )
        self.suspend(deficiency_id, [{"item_id": item, "kit_instance_id": self.kit}])
        (start,) = self.starts(deficiency_id)
        self.assertEqual((str(start.kit_instance_id), start.instance_key), (self.kit, self.kit))

    def test_the_database_refuses_a_kit_from_another_slot(self):
        drawer = self.make_slot(Compartment, "drawer")
        other = add_kit_instance(
            label="Synthetic kit elsewhere",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(drawer.pk),
            **self.context(self.supervisor),
        ).body["kit_id"]
        deficiency_id = self.report(self.cabinet_target())
        original = CheckSuspension.objects.bulk_create

        def elsewhere(rows, *rest, **kwargs):
            rows = list(rows)
            rows[0].kit_instance_id = rows[0].instance_key = other
            return original(rows, *rest, **kwargs)

        with (
            mock.patch(
                "cya.deficiencies.suspension.CheckSuspension.objects.bulk_create",
                side_effect=elsewhere,
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.suspend(
                deficiency_id, [{"item_id": str(self.kit_item.pk), "kit_instance_id": self.kit}]
            )
        self.assertIn("kit instance does not fit the item", str(caught.exception))
        self.assertEqual(self.starts(deficiency_id), [])


class EndingTests(SuspensionMixin, VerificationBase):
    def assert_ended_by(self, deficiency_id, event, starts):
        ids = sorted(str(s.pk) for s in starts)
        self.assertEqual(event.suspensions_ended, ids)
        ends = self.ends(deficiency_id)
        self.assertEqual(sorted(str(e.prior_suspension_id) for e in ends), ids)
        for end in ends:
            start = next(s for s in starts if s.pk == end.prior_suspension_id)
            self.assertEqual(
                (
                    end.deficiency_event_id,
                    end.item_id,
                    end.kit_instance_id,
                    end.vehicle_id,
                    end.command_id,
                ),
                (
                    event.pk,
                    start.item_id,
                    start.kit_instance_id,
                    start.vehicle_id,
                    event.command_id,
                ),
            )
        self.assertEqual(self.in_force(deficiency_id), [])

    def test_a_verified_closure_ends_every_suspension_and_lists_it(self):
        deficiency_id = self.pending(self.cabinet_target())
        self.suspend(deficiency_id, [{"item_id": str(self.suction.pk)}])
        self.suspend(deficiency_id, [{"item_id": str(self.masks.pk)}])
        starts = self.starts(deficiency_id)
        for item in (self.suction, self.masks, self.gauge, self.special):
            self.verify(deficiency_id, item)
        self.assertEqual(self.status(deficiency_id), "PENDING_VERIFICATION")
        result = self.verify(deficiency_id, self.limited)
        event = self.last_event(deficiency_id)
        self.assertEqual(
            (event.event_type, self.status(deficiency_id)),
            ("VERIFICATION_CLOSED", "CLOSED_VERIFIED"),
        )
        self.assertEqual(
            result.body["verification"]["suspensions_ended"], sorted(str(s.pk) for s in starts)
        )
        self.assert_ended_by(deficiency_id, event, starts)
        self.assertEqual(self.ends(deficiency_id)[0].actor_user_id, self.crew.pk)

    def test_a_failed_verification_ends_nothing(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        self.verify(deficiency_id, outcome="FAILED")
        self.assertEqual(self.last_event(deficiency_id).suspensions_ended, [])
        self.assertEqual(len(self.in_force(deficiency_id)), 1)

    def test_an_administrator_closure_ends_them_and_a_reopening_resumes_none(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        starts = self.starts(deficiency_id)
        closed = self.close(deficiency_id)
        event = self.last_event(deficiency_id)
        self.assertEqual(closed.body["suspensions_ended"], [str(starts[0].pk)])
        self.assert_ended_by(deficiency_id, event, starts)
        history = assignment_state(str(self.org.pk), deficiency_id)["history"]
        self.assertEqual(history[-1]["suspensions_ended"], [str(starts[0].pk)])
        reopen_administrator(
            deficiency_id=deficiency_id,
            expected_event_id=str(event.pk),
            explanation="Synthetic reopening.",
            **self.context(self.admin),
        )
        self.assertEqual(self.last_event(deficiency_id).suspensions_ended, [])
        self.assertEqual(self.in_force(deficiency_id), [])
        # A new suspension is an explicit new decision, with its own start.
        self.suspend(deficiency_id)
        self.assertEqual(len(self.starts(deficiency_id)), 2)
        self.assertEqual(len(self.in_force(deficiency_id)), 1)

    def test_an_empty_scope_closure_ends_them(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        starts = self.starts(deficiency_id)
        with mock.patch("cya.deficiencies.service.correction_scope", return_value=[]):
            result = close_empty_scope(
                deficiency_id=deficiency_id,
                expected_event_id=str(self.last_event(deficiency_id).pk),
                explanation="Synthetic: the item left the checklist.",
                **self.context(self.supervisor),
            )
        event = self.last_event(deficiency_id)
        self.assertEqual(event.event_type, "EMPTY_SCOPE_CLOSED")
        self.assertEqual(result.body["suspensions_ended"], [str(starts[0].pk)])
        self.assert_ended_by(deficiency_id, event, starts)

    def test_reported_in_error_ends_them_and_the_inv7_reopening_resumes_none(self):
        failed = self.failed()
        root, deficiency_id = failed.body["check_id"], failed.body["deficiency_ids"][0]
        self.suspend(deficiency_id)
        starts = self.starts(deficiency_id)
        correct_check(
            check_id=root, expected_tip_check_id=root, outcome="PASSED", **self.context(self.crew)
        )
        event = self.last_event(deficiency_id)
        # The suspension is neutral for COR-4: it records no repair, so cancellation still happens.
        self.assertEqual(event.event_type, "REPORTED_IN_ERROR")
        self.assert_ended_by(deficiency_id, event, starts)
        invalidate_chain(check_id=root, explanation="Synthetic reason.", **self.context(self.admin))
        self.assertEqual(self.last_event(deficiency_id).event_type, "REOPENED")
        self.assertEqual(self.status(deficiency_id), "OPEN")
        self.assertEqual(self.in_force(deficiency_id), [])

    def test_the_history_reader_answers_as_of_an_instant(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        reported = self.last_event(deficiency_id)
        self.suspend(deficiency_id, explanation=REASON)
        started = self.last_event(deficiency_id)
        self.close(deficiency_id)
        closed = self.last_event(deficiency_id)
        before = suspension_state(str(self.org.pk), deficiency_id, reported.recorded_at)
        self.assertEqual((before["in_force"], before["suspensions"]), ([], []))
        during = suspension_state(str(self.org.pk), deficiency_id, started.recorded_at)
        (row,) = during["suspensions"]
        self.assertEqual(during["in_force"], [row["suspension_id"]])
        self.assertEqual(
            (row["item_id"], row["started_by"], row["explanation"], row["ended_at"]),
            (str(self.suction.pk), str(self.supervisor.pk), REASON, None),
        )
        self.assertEqual(
            (during["deficiency_status"], during["last_event_id"]), ("OPEN", str(started.pk))
        )
        after = suspension_state(str(self.org.pk), deficiency_id)
        self.assertEqual(after["in_force"], [])
        self.assertEqual(
            (
                after["suspensions"][0]["ended_at"],
                after["suspensions"][0]["end_event_id"],
                after["suspensions"][0]["end_event_type"],
            ),
            (closed.recorded_at, str(closed.pk), "ADMINISTRATOR_CLOSED"),
        )
        with self.assertRaises(DeficiencyError) as caught:
            suspension_state(
                str(self.org.pk), deficiency_id, reported.recorded_at - timedelta(seconds=1)
            )
        self.assertEqual(caught.exception.status, 404)


class RaceTests(SuspensionMixin, VerificationBase):
    def hold(self, path, seconds=1.0):
        """Patch path so the call returns and then holds its transaction's locks for a while."""
        module, name = path.rsplit(".", 1)
        original = getattr(__import__(module, fromlist=[name]), name)

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

    def test_a_suspension_committed_first_is_ended_by_the_waiting_closure(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        with self.hold("cya.deficiencies.suspension.resolve_items"):
            suspended, passed = self.run_ordered(
                lambda: self.suspend(deficiency_id),
                lambda: self.verify(deficiency_id),
            )
        self.assertFalse(isinstance(suspended, Exception), suspended)
        self.assertFalse(isinstance(passed, Exception), passed)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.assertEqual(
            passed.body["verification"]["suspensions_ended"], suspended.body["suspension_ids"]
        )
        self.assertEqual(self.in_force(deficiency_id), [])

    def test_closure_first_the_stale_suspension_is_refused(self):
        deficiency_id = self.pending({"item_id": str(self.suction.pk)})
        seen = str(self.last_event(deficiency_id).pk)
        with self.hold("cya.checks.commands._verification_plan"):
            passed, suspended = self.run_ordered(
                lambda: self.verify(deficiency_id),
                lambda: self.suspend(
                    deficiency_id, [{"item_id": str(self.masks.pk)}], expected_event_id=seen
                ),
            )
        self.assertFalse(isinstance(passed, Exception), passed)
        self.assertIsInstance(suspended, DeficiencyError)
        self.assertEqual((suspended.code, suspended.status), ("DEFICIENCY_CHANGED", 409))
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.assertFalse(CheckSuspension.objects.exists())


class DatabaseGuardTests(SuspensionMixin, VerificationBase):
    def close_listing(self, deficiency_id, listed):
        """Administrator closure whose event lists `listed` instead of what the command read."""
        from cya.deficiencies.models import DeficiencyEvent

        original = DeficiencyEvent.objects.create

        def create(**fields):
            return original(**{**fields, "suspensions_ended": listed})

        with mock.patch(
            "cya.deficiencies.closure.DeficiencyEvent.objects.create", side_effect=create
        ):
            return self.close(deficiency_id)

    def test_a_closing_event_lists_exactly_the_suspensions_in_force(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        other_id = self.report({"item_id": str(self.masks.pk)})
        self.suspend(deficiency_id)
        self.suspend(other_id, [{"item_id": str(self.masks.pk)}])
        own = str(self.starts(deficiency_id)[0].pk)
        foreign = str(self.starts(other_id)[0].pk)
        malformed = (
            [],  # incomplete
            [foreign],  # another deficiency's suspension
            [own, foreign],
            [own, own],  # repeated
            ["not-a-suspension"],
            [123],
            [[own]],
            [own + "0"],
            [own.upper()],
            {"id": own},  # not an array
            own,
        )
        for listed in malformed:
            with self.subTest(listed=listed), self.assertRaises(DatabaseError) as caught:
                self.close_listing(deficiency_id, listed)
            self.assertRegex(
                str(caught.exception),
                "exactly the check suspensions in force|only a closing event",
            )
            self.assertEqual(self.status(deficiency_id), "OPEN")
            self.assertEqual(self.ends(deficiency_id), [])
        self.close_listing(deficiency_id, [own])  # the valid list
        self.assertEqual(self.status(deficiency_id), "CLOSED_ADMINISTRATOR")
        self.assertEqual(self.in_force(deficiency_id), [])
        self.assertEqual(len(self.in_force(other_id)), 1)

    def test_a_closure_with_none_in_force_lists_none(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        other_id = self.report({"item_id": str(self.masks.pk)})
        self.suspend(other_id, [{"item_id": str(self.masks.pk)}])
        with self.assertRaises(DatabaseError):
            self.close_listing(deficiency_id, [str(self.starts(other_id)[0].pk)])
        self.close_listing(deficiency_id, [])
        self.assertEqual(self.status(deficiency_id), "CLOSED_ADMINISTRATOR")

    def test_no_other_event_may_end_a_suspension(self):
        from cya.deficiencies.models import DeficiencyEvent

        deficiency_id = self.report(self.cabinet_target())
        self.suspend(deficiency_id)
        own = str(self.starts(deficiency_id)[0].pk)
        original = DeficiencyEvent.objects.create

        def create(**fields):
            return original(**{**fields, "suspensions_ended": [own]})

        with (
            mock.patch(
                "cya.deficiencies.suspension.DeficiencyEvent.objects.create", side_effect=create
            ),
            self.assertRaises(DatabaseError),
        ):
            self.suspend(deficiency_id, [{"item_id": str(self.masks.pk)}])
        self.assertEqual(self.in_force(deficiency_id), [own])

    def test_the_start_event_needs_a_supervisor_or_administrator(self):
        from cya.deficiencies.models import DeficiencyEvent

        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        original = DeficiencyEvent.objects.create

        def crew(**fields):
            return original(**{**fields, "actor_roles_at_time": ["CREW"]})

        with (
            mock.patch(
                "cya.deficiencies.suspension.DeficiencyEvent.objects.create", side_effect=crew
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.suspend(deficiency_id)
        self.assertIn("a supervisor or administrator", str(caught.exception))
        self.assertFalse(CheckSuspension.objects.exists())

    def test_one_check_is_not_suspended_twice_at_once(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        with (
            mock.patch("cya.deficiencies.suspension._not_suspended_yet"),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.suspend(deficiency_id)
        self.assertIn("already suspended", str(caught.exception))
        self.assertEqual(len(self.starts(deficiency_id)), 1)

    def test_a_start_row_must_match_its_kit_key(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        original = CheckSuspension.objects.bulk_create

        def wrong_key(rows, *rest, **kwargs):
            rows = list(rows)
            rows[0].instance_key = str(uuid4())
            return original(rows, *rest, **kwargs)

        with (
            mock.patch(
                "cya.deficiencies.suspension.CheckSuspension.objects.bulk_create",
                side_effect=wrong_key,
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.suspend(deficiency_id)
        self.assertIn("instance key mismatch", str(caught.exception))

    def test_an_end_names_a_start_its_closing_event_lists(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        other_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        self.suspend(other_id)
        foreign = self.starts(other_id)[0]
        original = CheckSuspension.objects.bulk_create

        def elsewhere(rows, *rest, **kwargs):
            rows = list(rows)
            rows[0].prior_suspension = foreign
            return original(rows, *rest, **kwargs)

        with (
            mock.patch(
                "cya.deficiencies.service.CheckSuspension.objects.bulk_create",
                side_effect=elsewhere,
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            self.close(deficiency_id)
        self.assertIn("closing event that lists its start", str(caught.exception))
        self.assertEqual(self.status(deficiency_id), "OPEN")

    def test_rows_are_append_only(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        start = self.starts(deficiency_id)[0]
        with self.assertRaises(DatabaseError):
            CheckSuspension.objects.filter(pk=start.pk).update(recorded_at=start.recorded_at)
        with self.assertRaises(DatabaseError):
            CheckSuspension.objects.filter(pk=start.pk).delete()


class MigrationTests(SuspensionMixin, VerificationBase):
    def trigger(self, name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s",
                [name],
            )
            row = cursor.fetchone()
        return None if row is None else row[0]

    def target(self, leaves):
        return [
            ("cya_deficiencies", "0016_scope_expansion") if leaf[0] == "cya_deficiencies" else leaf
            for leaf in leaves
        ]

    def test_reversal_restores_the_previous_trigger_and_forward_reinstalls_it(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        before = self.trigger("cya_deficiency_event_bi")
        self.assertIn("CHECKS_SUSPENDED", before)
        try:
            executor.migrate(self.target(leaves))
            self.assertNotIn("CHECKS_SUSPENDED", self.trigger("cya_deficiency_event_bi"))
            self.assertIn(
                "suspension endings are not available yet", self.trigger("cya_deficiency_event_bi")
            )
            self.assertIsNone(self.trigger("cya_check_suspension_bi"))
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(before, self.trigger("cya_deficiency_event_bi"))
        finally:
            MigrationExecutor(connection).migrate(leaves)

    def test_reversal_is_refused_once_a_suspension_exists(self):
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        try:
            with self.assertRaisesRegex(RuntimeError, "while suspensions exist"):
                executor.migrate(self.target(leaves))
        finally:
            MigrationExecutor(connection).migrate(leaves)
        self.assertIsNotNone(self.trigger("cya_check_suspension_bi"))


class ObligationTests(SuspensionMixin, AttentionCase, VerificationBase):
    """AC-18 end to end over the controllable clock: a 06:00 daily inspection (E 05:00, G 07:00)."""

    def states(self, item, kit_id=None, as_of=None, days=(0, 1)):
        as_of = as_of or self.clock.now
        return {
            row.obligation.due_at: row
            for row in get_vehicle_obligations_as_of(
                str(self.vehicle.pk),
                as_of,
                organization_id=str(self.org.pk),
                start=self.at(hours=6, days=days[0]),
                end=self.at(hours=6, days=days[-1]),
            )
            if row.obligation.item_id == str(item.pk)
            and row.obligation.kit_instance_id == kit_id
            and row.obligation.recorded_at <= as_of
        }

    def test_ac18_suspended_is_not_satisfied_leaves_the_filter_and_ends_with_verification(self):
        self.clock.set(self.at(hours=5, minutes=30))
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        result = self.suspend(deficiency_id)
        (suspension_id,) = result.body["suspension_ids"]
        row = self.row(self.suction)
        self.assertEqual((row.obligation_state, row.check_suspended), ("SUSPENDED", True))
        self.assertFalse(row.needs_action)
        self.assertTrue(row.attention.red)  # its deficiency is still listed beside it
        self.assertEqual([d["id"] for d in row.deficiencies], [deficiency_id])
        day0 = self.states(self.suction)[self.at(hours=6)]
        self.assertEqual((day0.state, day0.evidence_id), (State.SUSPENDED, suspension_id))
        self.assertIsNone(day0.outcome)
        self.assertEqual(day0.display_text, "Known Deficiency — Check Suspended")
        # Excluded from Needs Attention Only although red; unaffected checks stay required.
        only = self.item_ids(needs_attention_only=True)
        self.assertNotIn((str(self.suction.pk), None), only)
        self.assertIn((str(self.masks.pk), None), only)
        self.assertEqual(self.row(self.masks).obligation_state, "CURRENT")
        for view in ("NORMAL", "PRIORITY"):
            self.assertIn((str(self.suction.pk), None), self.item_ids(view=view))
        # A check recorded anyway does not satisfy it, and nothing changes the vehicle's status.
        self.record(self.suction)
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.SUSPENDED)
        self.assertFalse(VehicleStatusEvent.objects.filter(vehicle=self.vehicle).exists())

        # Past G the obligation stays Suspended (never Overdue, never Satisfied).
        self.clock.set(self.at(hours=8))
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.SUSPENDED)
        suspended_at = self.clock.now

        # The next morning, before the window opens, the repair is recorded and verified.
        self.clock.set(self.at(days=1, hours=3))
        self.work(deficiency_id)
        self.correct(deficiency_id)
        self.clock.set(self.at(days=1, hours=4))
        self.verify(deficiency_id)
        self.assertEqual(self.status(deficiency_id), "CLOSED_VERIFIED")
        self.assertEqual(self.last_event(deficiency_id).suspensions_ended, [suspension_id])

        # The new window needs its check again; the suspended one stays Suspended in history.
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        states = self.states(self.suction)
        self.assertEqual(states[self.at(hours=6)].state, State.SUSPENDED)
        self.assertEqual(states[self.at(days=1, hours=6)].state, State.CURRENT)
        self.assertEqual(self.row(self.suction).obligation_state, "CURRENT")
        self.assertIn((str(self.suction.pk), None), self.item_ids(needs_attention_only=True))
        # As of the earlier instant the answer is what it was then (AC-27).
        then = self.row(self.suction, as_of=suspended_at)
        self.assertEqual(then.obligation_state, "SUSPENDED")
        self.assertNotIn(
            (str(self.suction.pk), None),
            self.item_ids(needs_attention_only=True, as_of=suspended_at),
        )
        # Once checked, the row shows its current obligation rather than the ended suspension
        # kept in history, and a new deficiency on the item is back in Needs Attention Only.
        self.record(self.suction)
        states = self.states(self.suction)
        end = CheckSuspension.objects.get(prior_suspension_id=suspension_id)
        self.assertEqual(states[self.at(hours=6)].suspension_ended_at, end.recorded_at)
        self.assertFalse(states[self.at(hours=6)].suspension_in_force)
        row = self.row(self.suction)
        self.assertEqual((row.obligation_state, row.check_suspended), ("SATISFIED", False))
        self.report({"item_id": str(self.suction.pk)})
        self.assertIn((str(self.suction.pk), None), self.item_ids(needs_attention_only=True))

    def test_only_the_named_kit_instance_is_suspended(self):
        self.clock.set(self.at(hours=5, minutes=30))
        # A report on the kit item without an instance covers both physical kits.
        deficiency_id = self.report({"item_id": str(self.kit_item.pk)})
        self.suspend(
            deficiency_id,
            [{"item_id": str(self.kit_item.pk), "kit_instance_id": self.reference}],
        )
        self.assertEqual(self.row(self.kit_item, self.reference).obligation_state, "SUSPENDED")
        self.assertEqual(self.row(self.kit_item, self.kit_b).obligation_state, "CURRENT")
        only = self.item_ids(needs_attention_only=True)
        self.assertNotIn((str(self.kit_item.pk), self.reference), only)
        self.assertIn((str(self.kit_item.pk), self.kit_b), only)

    def test_a_pass_recorded_before_the_suspension_keeps_its_obligation_satisfied(self):
        self.clock.set(self.at(hours=5, minutes=30))
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.record(self.suction)
        self.clock.set(self.at(hours=5, minutes=40))
        self.suspend(deficiency_id)
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.SATISFIED)
        # Past G and after the suspension ends, the earlier Pass still counts.
        self.clock.set(self.at(hours=8))
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.SATISFIED)
        self.close(deficiency_id)
        self.clock.set(self.at(hours=9))
        satisfied = self.states(self.suction)[self.at(hours=6)]
        self.assertEqual((satisfied.state, satisfied.outcome), (State.SATISFIED, "PASSED"))

    def test_a_suspension_cannot_erase_an_inspection_already_missed(self):
        self.clock.set(self.at(hours=8))  # past G of the 06:00 inspection, never checked
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.OVERDUE)
        # The next window is suspended, but the row still shows the miss and stays actionable.
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        states = self.states(self.suction)
        self.assertEqual(states[self.at(days=1, hours=6)].state, State.SUSPENDED)
        self.assertEqual(self.row(self.suction).obligation_state, "OVERDUE")

    def test_an_administrator_closure_during_the_window_makes_the_check_required_again(self):
        self.clock.set(self.at(hours=5, minutes=15))
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        self.suspend(deficiency_id)
        self.clock.set(self.at(hours=6, minutes=15))
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.SUSPENDED)
        self.close(deficiency_id)
        self.clock.set(self.at(hours=6, minutes=30))
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.DUE)
        self.clock.set(self.at(hours=8))
        self.assertEqual(self.states(self.suction)[self.at(hours=6)].state, State.OVERDUE)

    def test_replaying_the_final_history_matches_every_earlier_answer(self):
        """AC-26/27: the export at the end replays each earlier answer without the database."""
        observed = []
        start, end = self.at(hours=6), self.at(days=1, hours=6)

        def observe():
            states = get_vehicle_obligations_as_of(
                str(self.vehicle.pk),
                self.clock.now,
                organization_id=str(self.org.pk),
                start=start,
                end=end,
            )
            observed.append(
                (self.clock.now, [(s.obligation.id, s.state, s.evidence_id) for s in states])
            )

        self.clock.set(self.at(hours=5, minutes=30))
        deficiency_id = self.report({"item_id": str(self.suction.pk)})
        observe()
        self.suspend(deficiency_id)
        observe()
        self.clock.set(self.at(hours=8))
        observe()
        self.clock.set(self.at(days=1, hours=3))
        self.work(deficiency_id)
        self.correct(deficiency_id)
        self.clock.set(self.at(days=1, hours=4))
        self.verify(deficiency_id)
        observe()
        self.clock.set(self.at(days=1, hours=5, minutes=30))
        observe()
        self.assertIn(State.SUSPENDED, {s for _t, rows in observed for _o, s, _e in rows})
        history = load_projection_history(
            str(self.vehicle.pk),
            self.clock.now,
            organization_id=str(self.org.pk),
            start=start,
            end=end,
        )
        self.assertTrue(any(s.ended_at is not None for s in history.suspensions))
        with self.assertNumQueries(0):
            for instant, expected in observed:
                replayed = rebuild_vehicle_projection(
                    history, str(self.vehicle.pk), instant, start, end
                )
                self.assertEqual(
                    [(s.obligation.id, s.state, s.evidence_id) for s in replayed], expected, instant
                )


class SuspensionMigrationOrderTests(TransactionTestCase):
    """The migration's trigger text keeps the previous rules it does not change."""

    def test_the_composed_trigger_relaxes_only_the_suspension_rule(self):
        from importlib import import_module

        current = import_module("cya.deficiencies.migrations.0017_check_suspension").event_insert()
        previous = import_module("cya.deficiencies.migrations.0016_scope_expansion").event_insert()
        self.assertNotIn("suspension endings are not available yet", current)
        self.assertIn("suspension endings are not available yet", previous)
        for rule in ("verification closure needs every scope row", "scope expansion keeps"):
            self.assertIn(rule, current)
