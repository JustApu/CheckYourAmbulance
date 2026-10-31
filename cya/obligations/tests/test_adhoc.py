"""Offline one-off orders: attribution, isolation, linking, replay and historical truth."""

from dataclasses import replace
from datetime import timedelta
from unittest import mock

from django.db import DatabaseError, transaction

from cya.attention.checklist import vehicle_checklist
from cya.checks.commands import record_check
from cya.checks.models import CheckObligationLink
from cya.core.audit_commands import AuditPersistenceFailure, CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.obligations.adhoc import request_inspection
from cya.obligations.models import Obligation, ObligationSelection, RequirementVersion
from cya.obligations.projections import get_vehicle_obligations_as_of, load_projection_history
from cya.obligations.state import State, rebuild_vehicle_projection
from cya.obligations.tests.test_generation import GenerationCase
from cya.obligations.tests.test_kits import KitObligationCase
from cya.operations.commands import StatusError


class AdHocTests(GenerationCase):
    def order(self, actor=None, items=None, **kw):
        return request_inspection(
            vehicle_id=str(self.vehicle.pk),
            items=items
            if items is not None
            else [{"item_id": str(self.suction.pk), "kit_instance_id": None}],
            due_at=kw.pop("due_at", self.at(hours=6)),
            explanation=kw.pop("explanation", "Synthetic return inspection."),
            **(kw.pop("context", None) or self.context(actor or self.supervisor)),
            **kw,
        )

    def project(self, instant=None):
        return get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            instant or self.clock.now,
            organization_id=str(self.org.pk),
            start=self.at(),
            end=self.at(days=1),
        )

    def check(self, actor=None, **kw):
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.version_id,
            outcome="PASSED",
            **kw,
            **self.context(actor or self.crew),
        )

    def test_attributed_pinned_window_and_no_routine_selection(self):
        result = self.order()
        row = Obligation.objects.get(pk=result.body["obligation_ids"][0])
        self.assertFalse(row.routine)
        self.assertEqual(row.actor_user, self.supervisor)
        self.assertEqual(row.explanation, "Synthetic return inspection.")
        self.assertEqual(row.early_at, row.recorded_at)
        self.assertEqual(row.grace_at, row.due_at)
        self.assertIsNone(row.requirement_version.schedule_rule_id)
        self.assertIsNone(row.requirement_version.binding_id)
        self.assertEqual(
            str(row.requirement_version.publication.checklist_version_id), self.version_id
        )
        self.assertFalse(ObligationSelection.objects.exists())
        self.assertTrue(
            AuditEvent.objects.filter(command_id=row.command_id, outcome="SUCCEEDED").exists()
        )
        state = self.project()[0]
        self.assertEqual(state.state, State.CURRENT)
        self.assertEqual(state.obligation.explanation, row.explanation)
        checklist = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), as_of=self.clock.now)
        self.assertEqual(
            next(r for r in checklist.rows if r.item_id == str(self.suction.pk)).obligation_state,
            "CURRENT",
        )

    def test_creation_replay_after_due_and_distinct_requests(self):
        context = self.context(self.supervisor)
        first = self.order(context=context)
        before = Obligation.objects.count(), RequirementVersion.objects.count()
        self.clock.set(self.at(days=2))
        replay = self.order(context=context)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.body, replay.body)
        self.assertEqual((Obligation.objects.count(), RequirementVersion.objects.count()), before)
        self.clock.set(self.at(hours=1))
        self.order()
        self.assertEqual(Obligation.objects.filter(routine=False).count(), 2)

    def test_each_role_and_union_authorization(self):
        supply = self.make_user("supply")
        self.grant(supply, "SUPPLY_OFFICER")
        for actor, allowed in [
            (self.crew, False),
            (supply, False),
            (self.supervisor, True),
            (self.admin, True),
        ]:
            with self.subTest(actor=actor.pk):
                if allowed:
                    self.order(actor)
                else:
                    with self.assertRaises(CommandDenied):
                        self.order(actor)
        self.grant(supply, "SUPERVISOR")
        self.order(supply)
        self.assertEqual(Obligation.objects.count(), 3)

    def test_shape_scope_deadline_and_failure_rollback(self):
        for bad in [
            [],
            [{}],
            [{"item_id": "bad", "kit_instance_id": None}],
            [{"item_id": str(self.suction.pk), "kit_instance_id": None}] * 2,
        ]:
            with self.subTest(items=bad), self.assertRaises(StatusError):
                self.order(items=bad)
        for kw in [
            {"due_at": self.at()},
            {"explanation": " "},
            {"items": [{"item_id": str(self.suction.pk), "kit_instance_id": str(self.vehicle.pk)}]},
        ]:
            with self.subTest(kw=kw), self.assertRaises(StatusError):
                self.order(**kw)
        context = self.context(self.supervisor)
        with mock.patch(
            "cya.obligations.adhoc.Obligation.objects.create",
            side_effect=DatabaseError("synthetic write failure"),
        ):
            with self.assertRaises(DatabaseError):
                self.order(context=context)
        self.assertFalse(Obligation.objects.exists())
        self.assertFalse(RequirementVersion.objects.exists())
        self.assertFalse(
            CommandReceipt.objects.filter(operation_id=context["operation_id"]).exists()
        )
        self.order(context=context)

    def test_generation_and_publication_both_orders_keep_same_instant_orders(self):
        for routine_first in (False, True):
            if routine_first:
                self.generate()
            result = self.order()
            oid = result.body["obligation_ids"][0]
            self.generate()
            self.generate()
            matching = [
                o
                for o in self.obligations()
                if o.due_at == self.at(hours=6) and o.item_id == str(self.suction.pk)
            ]
            self.assertTrue(any(o.pk == oid for o in matching))
            self.assertTrue(any(o.routine for o in matching))
            # Publication reconciliation is after commit and changes routine pins only.
            self.published_version()
            self.generate()
            self.assertTrue(any(o.pk == oid for o in self.obligations()))

    def test_real_checks_satisfy_only_one_of_colliding_obligations(self):
        self.generate()
        order = self.order()
        self.clock.set(self.at(hours=6))
        first = self.check()
        self.assertEqual(
            CheckObligationLink.objects.filter(check_record_id=first.body["check_id"]).count(), 1
        )
        matching = [
            s
            for s in self.project()
            if s.obligation.item_id == str(self.suction.pk)
            and s.obligation.due_at == self.at(hours=6)
        ]
        self.assertEqual(sum(s.state == State.SATISFIED for s in matching), 1)
        # Acknowledging the displayed basis records an independent check, never a session completion.
        self.check(
            actor=self.supervisor,
            basis_check_id=first.body["check_id"],
            acknowledges_check_id=first.body["check_id"],
        )
        matching = [
            s
            for s in self.project()
            if s.obligation.item_id == str(self.suction.pk)
            and s.obligation.due_at == self.at(hours=6)
        ]
        self.assertEqual(sum(s.state == State.SATISFIED for s in matching), 2)
        self.assertEqual(len({s.obligation.id for s in matching}), 2)
        self.assertIn(order.body["obligation_ids"][0], {s.obligation.id for s in matching})

    def test_outage_return_and_shuffled_replay_preserve_historical_answers(self):
        before = self.clock.now
        self.go_out_of_service(self.at(hours=1))
        self.order()
        t1 = self.clock.now
        self.assertEqual(self.project(before), ())
        expected = self.project(t1)
        self.assertEqual(expected[0].state, State.CURRENT)
        self.assertFalse(expected[0].obligation.routine)
        self.clock.set(self.at(hours=2))
        self.check()
        t2 = self.clock.now
        self.assertEqual(self.project(t2)[0].state, State.SATISFIED)
        count = Obligation.objects.count()
        self.return_to_service(self.at(hours=3))
        self.assertEqual(Obligation.objects.count(), count)
        self.assertEqual(self.project(t1), expected)
        history = load_projection_history(
            str(self.vehicle.pk),
            self.clock.now,
            organization_id=str(self.org.pk),
            start=self.at(),
            end=self.at(days=1),
        )
        shuffled = replace(
            history,
            obligations=tuple(reversed(history.obligations)),
            check_records=tuple(reversed(history.check_records)),
        )
        self.assertEqual(
            rebuild_vehicle_projection(
                shuffled, str(self.vehicle.pk), t1, self.at(), self.at(days=1)
            ),
            expected,
        )

    def test_insert_guards_reject_window_origin_and_receipt_tampering(self):
        self.order()
        row = Obligation.objects.get()
        for changes in [
            {"early_at": row.recorded_at - timedelta(seconds=1)},
            {"routine": True, "explanation": None},
            {"explanation": ""},
        ]:
            fields = {
                f.attname: getattr(row, f.attname)
                for f in row._meta.concrete_fields
                if not f.primary_key
            }
            fields.update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises(DatabaseError),
                transaction.atomic(),
            ):
                Obligation.objects.create(**fields)

    def test_far_future_and_older_overdue_orders_remain_on_checklist(self):
        self.order(due_at=self.at(days=45))
        view = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), as_of=self.clock.now)
        self.assertEqual(
            next(r for r in view.rows if r.item_id == str(self.suction.pk)).obligation_state,
            "CURRENT",
        )
        self.clock.set(self.at(days=60))
        view = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), as_of=self.clock.now)
        self.assertEqual(
            next(r for r in view.rows if r.item_id == str(self.suction.pk)).obligation_state,
            "OVERDUE",
        )

    def test_removed_item_remains_visible_with_its_immutable_order(self):
        self.order()
        self.clock.set(self.at(hours=1))
        self.published_version(
            [row for row in self.base_items() if row["item_id"] != str(self.suction.pk)]
        )
        view = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), as_of=self.clock.now)
        row = next(r for r in view.rows if r.item_id == str(self.suction.pk))
        self.assertEqual((row.item_label, row.obligation_state), (self.suction.label, "CURRENT"))

    def test_pre_order_check_is_not_retroactively_linked_and_reused_content_conflicts(self):
        self.check()
        context = self.context(self.supervisor)
        self.order(context=context)
        self.assertEqual(self.project()[0].state, State.CURRENT)
        self.assertFalse(CheckObligationLink.objects.exists())
        different = self.order(context=context, explanation="Changed synthetic order.")
        self.assertTrue(different.operation_reused)
        self.assertEqual(Obligation.objects.count(), 1)


class AdHocKitTests(KitObligationCase):
    def test_explicit_instances_are_independent_and_survive_assignment_changes(self):
        other = self.add_kit("Other").body["kit_id"]
        pairs = [
            {"item_id": str(self.kit_item.pk), "kit_instance_id": kit}
            for kit in (self.reference, other)
        ]
        request_inspection(
            vehicle_id=str(self.vehicle.pk),
            items=pairs,
            due_at=self.at(hours=6),
            explanation="Synthetic per-kit order.",
            **self.context(self.supervisor),
        )
        self.clock.set(self.at(hours=1))
        result = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.kit_item.pk),
            kit_id=self.reference,
            checklist_version_id=self.version_id,
            outcome="PASSED",
            **self.context(self.crew),
        )
        links = CheckObligationLink.objects.filter(check_record_id=result.body["check_id"])
        self.assertEqual(links.count(), 1)
        self.assertEqual(links.get().obligation.kit_instance_id, self.reference)
        self.move(other, self.vehicle_b)
        self.generate()
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            self.clock.now,
            organization_id=str(self.org.pk),
            start=self.at(),
            end=self.at(hours=6),
        )
        ordered = [s for s in states if not s.obligation.routine]
        self.assertEqual({s.obligation.kit_instance_id for s in ordered}, {self.reference, other})
        self.assertEqual({s.state for s in ordered}, {State.CURRENT, State.SATISFIED})


class AdHocHintTests(GenerationCase):
    order = AdHocTests.order

    def test_after_commit_hint_and_rollback_silence(self):
        from cya.live_updates.tests import PublishAfterCommitTests

        layer = PublishAfterCommitTests.RecordingLayer()
        with mock.patch("cya.live_updates.publish.get_channel_layer", return_value=layer):
            context = self.context(self.supervisor)
            self.order(context=context)
            self.assertEqual(len(layer.sent), 1)
            self.assertEqual(layer.sent[0][1]["kind"], "obligations.changed")
            self.order(context=context)
            self.assertEqual(len(layer.sent), 1)
            with mock.patch(
                "cya.core.audit_commands.append_audit",
                side_effect=DatabaseError("synthetic audit failure"),
            ):
                with self.assertRaises(AuditPersistenceFailure):
                    self.order()
            self.assertEqual(len(layer.sent), 1)


class AdHocPinTests(GenerationCase):
    order = AdHocTests.order

    def test_order_pins_known_future_publication_and_ignores_later_publication(self):
        future = self.published_version(effective_at=self.at(hours=4))
        result = self.order()
        obligation = Obligation.objects.select_related("requirement_version__publication").get(
            pk=result.body["obligation_ids"][0]
        )
        self.assertEqual(
            str(obligation.requirement_version.publication.checklist_version_id), future
        )
        self.clock.set(self.at(hours=2))
        self.published_version(effective_at=self.at(hours=5))
        self.generate()
        retained = next(o for o in self.obligations() if o.pk == obligation.pk)
        self.assertEqual(str(retained.requirement_version.publication.checklist_version_id), future)

    def test_replay_requires_current_permission_and_denial_is_audited(self):
        from cya.authorization.commands import change_role

        context = self.context(self.supervisor)
        self.order(context=context)
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(self.supervisor.pk),
            role="SUPERVISOR",
            action="REVOKE",
            **{k: v for k, v in self.context().items() if k not in ("organization_id", "actor_id")},
        )
        with self.assertRaises(CommandDenied):
            self.order(context=context)
        self.assertEqual(Obligation.objects.count(), 1)
        self.assertTrue(
            AuditEvent.objects.filter(action="AD_HOC_INSPECTION_REQUEST", outcome="DENIED").exists()
        )


class AdHocRaceTests(GenerationCase):
    order = AdHocTests.order
    check = AdHocTests.check
    project = AdHocTests.project

    def test_pre_order_check_holds_item_lock_until_its_evidence_is_committed(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from django.db import connection

        from cya.checks import commands as check_commands

        real = check_commands.choose_links
        entered, release = Event(), Event()

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=20):
                raise RuntimeError("synthetic check was not released")
            return real(*args, **kwargs)

        def run(function):
            try:
                return function()
            finally:
                connection.close()

        with (
            mock.patch.object(check_commands, "choose_links", side_effect=paused),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            checking = pool.submit(run, self.check)
            try:
                self.assertTrue(entered.wait(timeout=20))
                ordering = pool.submit(run, self.order)
                with self.assertRaises(TimeoutError):
                    ordering.result(timeout=1)
            finally:
                release.set()
            check = checking.result(timeout=30)
            ordered = ordering.result(timeout=30)
        self.assertFalse(
            CheckObligationLink.objects.filter(check_record_id=check.body["check_id"]).exists()
        )
        self.assertEqual(self.project()[0].obligation.id, ordered.body["obligation_ids"][0])
        self.assertEqual(self.project()[0].state, State.CURRENT)
