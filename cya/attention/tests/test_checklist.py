"""Item attention, view ordering, the filter and as-of reproducibility (issue 83 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/83))."""

from datetime import timedelta
from unittest import mock

from cya.attention.checklist import ChecklistQueryError, vehicle_checklist
from cya.attention.facts import item_attention, verify_attention
from cya.attention.tests.support import AttentionCase
from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistTemplate, Item
from cya.checks.commands import correct_check
from cya.deficiencies.commands import regrade_severity
from cya.deficiencies.models import DeficiencyEvent, DeficiencyProjection
from cya.obligations.models import Obligation
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import NO_KIT, CheckEvidence, State, SuspensionEvidence
from cya.operations.commands import set_vehicle_status


class AttentionTests(AttentionCase):
    def test_check_adapter_replaces_satisfaction_but_not_stored_attention_evidence(self):
        self.clock.set(self.at(hours=5, minutes=30))
        failed = self.failed(self.suction)
        check_id, deficiency_id = failed.body["check_id"], failed.body["deficiency_ids"][0]

        def query(**kw):
            states = get_vehicle_obligations_as_of(
                str(self.vehicle.pk),
                self.clock.now,
                organization_id=str(self.org.pk),
                start=self.at(hours=6),
                end=self.at(hours=6),
                **kw,
            )
            return next(row for row in states if row.obligation.item_id == str(self.suction.pk))

        stored = query()
        self.assertEqual((stored.state, stored.outcome), (State.SATISFIED, "FAILED"))
        adapter_pass = CheckEvidence(
            "synthetic-adapter-pass",
            str(self.org.pk),
            str(self.vehicle.pk),
            str(self.suction.pk),
            stored.obligation.id,
            self.clock.now,
            "PASSED",
        )
        for evidence, state, outcome in (
            ([], State.CURRENT, None),
            ([adapter_pass], State.SATISFIED, "PASSED"),
        ):
            with self.subTest(outcome=outcome):
                result = query(check_lookup=mock.Mock(return_value=evidence))
                self.assertEqual((result.state, result.outcome), (state, outcome))
                self.assertEqual(result.attention.check_id, check_id)
                self.assertEqual(result.attention.check_outcome, "FAILED")
                self.assertEqual(result.attention.deficiency_ids, (deficiency_id,))

        self.record(self.suction, basis_check_id=check_id)
        result = query(check_lookup=lambda **kw: [])
        self.assertEqual(result.state, State.CURRENT)
        self.assertTrue(result.attention.red)  # stored Pass clears only the check cause
        self.assertIsNone(result.attention.check_id)
        self.assertEqual(result.attention.deficiency_ids, (deficiency_id,))

    def test_a_fresh_vehicle_is_clear_and_lists_every_item_and_kit_once(self):
        rows = self.rows()
        self.assertTrue(all(not r.attention.red for r in rows))
        kit_rows = [r for r in rows if r.item_id == str(self.kit_item.pk)]
        self.assertEqual(sorted(r.kit_id for r in kit_rows), sorted([self.reference, self.kit_b]))
        self.assertEqual(len(rows), len(self.items) - 1 + 2)  # the kit item has two rows

    def test_a_failed_check_is_red_and_its_deficiency_is_listed_beside_the_item(self):
        self.failed(self.suction)
        row = self.row(self.suction)
        self.assertEqual(
            (row.attention.state, row.attention.check_outcome), ("ATTENTION", "FAILED")
        )
        self.assertEqual([d["summary"] for d in row.deficiencies], ["Synthetic summary"])
        self.assertEqual(row.deficiencies[0]["severity_code"], "HIGH")
        self.assertFalse(self.row(self.masks).attention.red)
        self.assertEqual(self.row(self.masks).deficiencies, ())

    def test_a_later_pass_does_not_clear_it_while_the_deficiency_is_unresolved(self):
        failed = self.failed(self.suction)
        self.record(self.suction, actor=self.supervisor, basis_check_id=failed.body["check_id"])
        row = self.row(self.suction)
        self.assertTrue(row.attention.red)
        self.assertIsNone(row.attention.check_id)  # the latest check is a Pass
        self.assertEqual(len(row.attention.deficiency_ids), 1)

    def test_it_clears_once_the_deficiency_is_resolved_and_the_latest_check_passes(self):
        failed = self.failed(self.suction)
        check_id = failed.body["check_id"]
        correct_check(
            check_id=check_id,
            expected_tip_check_id=check_id,
            outcome="PASSED",
            **self.context(self.crew),
        )  # COR-4: the author's Failed to Passed correction reports the deficiency in error
        self.assertFalse(self.row(self.suction).attention.red)
        self.assertEqual(self.row(self.suction).deficiencies, ())

    def test_unable_to_inspect_is_red(self):
        self.failed(self.suction, outcome="UNABLE_TO_INSPECT", explanation="Synthetic reason.")
        self.assertEqual(self.row(self.suction).attention.check_outcome, "UNABLE_TO_INSPECT")

    def test_a_failure_in_one_kit_is_red_for_that_kit_only(self):
        self.failed(self.kit_item, kit_id=self.reference)
        self.assertTrue(self.row(self.kit_item, self.reference).attention.red)
        self.assertFalse(self.row(self.kit_item, self.kit_b).attention.red)
        self.assertEqual(self.row(self.kit_item, self.kit_b).deficiencies, ())
        self.assertEqual(len(self.row(self.kit_item, self.reference).deficiencies), 1)

    def test_two_identical_kits_fail_independently(self):
        self.failed(self.kit_item, kit_id=self.reference)
        self.failed(self.kit_item, kit_id=self.kit_b)
        states = {r.kit_id: r.attention.red for r in self.rows() if r.kit_id}
        self.assertEqual(states, {self.reference: True, self.kit_b: True})

    def test_the_recompute_check_agrees_and_catches_a_stale_projection(self):
        self.failed(self.suction)
        keys = [(str(self.suction.pk), NO_KIT)]
        args = (str(self.org.pk), str(self.vehicle.pk), keys)
        self.assertEqual(verify_attention(*args, self.clock.now), [])
        DeficiencyProjection.objects.update(status="CLOSED_VERIFIED")  # simulated drift
        # The check cause keeps the key red either way, so compare the deficiency evidence.
        mismatch = verify_attention(*args, self.clock.now)
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["live"].deficiency_ids, ())
        self.assertEqual(len(mismatch[0]["recomputed"].deficiency_ids), 1)


class ViewTests(AttentionCase):
    def order(self, **kw):
        return [r.display_order for r in self.rows(**kw)]

    def test_normal_follows_the_checklist_order(self):
        order = self.order()
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.order(view="NORMAL"), order)

    def test_priority_puts_attention_first_then_actionable_then_the_rest_in_normal_order(self):
        self.clock.set(self.at(hours=5, minutes=30))  # inside the early window: Current
        self.record(self.masks, quantity=6)  # satisfied and clear
        self.failed(self.siren)  # red
        rows = self.rows(view="PRIORITY")
        self.assertEqual(rows[0].item_id, str(self.siren.pk))
        self.assertEqual(rows[-1].item_id, str(self.masks.pk))
        middle = [r.display_order for r in rows[1:-1]]
        self.assertEqual(middle, sorted(middle))
        self.assertEqual({r.item_id for r in rows}, {r.item_id for r in self.rows()})

    def test_needs_attention_only_before_the_window_keeps_red_rows_only(self):
        self.failed(self.siren)
        rows = self.rows(needs_attention_only=True)
        self.assertEqual([r.item_id for r in rows], [str(self.siren.pk)])

    def test_needs_attention_only_in_the_window_adds_unsatisfied_rows_and_drops_done_ones(self):
        self.clock.set(self.at(hours=5, minutes=30))
        self.record(self.masks, quantity=6)
        ids = {r.item_id for r in self.rows(needs_attention_only=True)}
        self.assertNotIn(str(self.masks.pk), ids)
        self.assertIn(str(self.suction.pk), ids)
        self.assertIn(str(self.kit_item.pk), ids)

    def test_overdue_rows_stay_in_the_filter(self):
        self.clock.set(self.at(hours=9))
        row = self.row(self.suction, needs_attention_only=True)
        self.assertEqual(row.obligation_state, "OVERDUE")

    def test_the_filter_works_in_either_view(self):
        self.failed(self.siren)
        for view in ("NORMAL", "PRIORITY"):
            rows = self.rows(view=view, needs_attention_only=True)
            self.assertEqual([r.item_id for r in rows], [str(self.siren.pk)], view)

    def test_a_suspended_obligation_is_not_actionable_but_stays_in_the_full_list(self):
        self.clock.set(self.at(hours=5, minutes=30))

        def lookup(*, organization_id, vehicle_id, obligation_ids, as_of):
            mine = Obligation.objects.filter(pk__in=obligation_ids, item=self.suction)
            return [
                SuspensionEvidence(str(pk), as_of - timedelta(hours=1))
                for pk in mine.values_list("pk", flat=True)
            ]

        full = {r.item_id: r for r in self.rows(suspension_lookup=lookup)}
        self.assertEqual(full[str(self.suction.pk)].obligation_state, "SUSPENDED")
        only = {r.item_id for r in self.rows(needs_attention_only=True, suspension_lookup=lookup)}
        self.assertNotIn(str(self.suction.pk), only)
        self.assertIn(str(self.siren.pk), only)

    def test_a_suspended_item_with_an_unresolved_deficiency_is_red_but_out_of_the_filter(self):
        self.clock.set(self.at(hours=5, minutes=30))
        self.failed(self.suction)

        def lookup(*, organization_id, vehicle_id, obligation_ids, as_of):
            mine = Obligation.objects.filter(pk__in=obligation_ids, item=self.suction)
            return [
                SuspensionEvidence(str(pk), as_of - timedelta(hours=1))
                for pk in mine.values_list("pk", flat=True)
            ]

        full = {r.item_id: r for r in self.rows(suspension_lookup=lookup)}
        self.assertEqual(full[str(self.suction.pk)].obligation_state, "SUSPENDED")
        self.assertTrue(full[str(self.suction.pk)].attention.red)  # its deficiency stays listed
        # AC-18: a supervisor-suspended check is excluded from Needs Attention Only even while red.
        only = {r.item_id for r in self.rows(needs_attention_only=True, suspension_lookup=lookup)}
        self.assertNotIn(str(self.suction.pk), only)


class QueryTests(AttentionCase):
    def test_the_vehicle_status_is_included(self):
        self.assertEqual(vehicle_checklist(*self.args()).vehicle_status["status"], "IN_SERVICE")
        set_vehicle_status(
            vehicle_id=str(self.vehicle.pk),
            status="LIMITED_SERVICE",
            explanation="Synthetic reason.",
            restrictions="Synthetic restriction.",
            **self.context(self.supervisor),
        )
        status = vehicle_checklist(*self.args()).vehicle_status
        self.assertEqual(
            (status["status"], status["restrictions"]),
            ("LIMITED_SERVICE", "Synthetic restriction."),
        )

    def args(self):
        return str(self.org.pk), str(self.vehicle.pk)

    def test_a_re_grade_changes_the_listed_severity_from_its_instant_on_only(self):
        failed = self.failed(self.suction)
        deficiency_id = failed.body["deficiency_ids"][0]
        before = self.clock.now
        expected = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
        result = regrade_severity(
            deficiency_id=deficiency_id,
            severity="LOW",
            explanation="Synthetic reason for the new grade.",
            expected_event_id=expected.pk,
            **self.context(self.supervisor),
        )
        self.assertEqual(result.status, 201)
        after = self.clock.now

        def level(instant=None):
            kw = {} if instant is None else {"as_of": instant}
            return self.row(self.suction, **kw).deficiencies[0]["severity_code"]

        self.assertEqual(level(), "LOW")
        self.assertEqual(level(after), "LOW")
        self.assertEqual(level(before), "HIGH")  # the as-of contract: the past stays as it was
        self.assertNotEqual(
            self.row(self.suction).deficiencies[0]["severity_label"],
            self.row(self.suction, as_of=before).deficiencies[0]["severity_label"],
        )

    def test_an_as_of_query_reproduces_the_past_after_later_events(self):
        before = self.clock.now
        failed = self.failed(self.suction)
        recorded = self.clock.now
        check_id = failed.body["check_id"]
        correct_check(
            check_id=check_id,
            expected_tip_check_id=check_id,
            outcome="PASSED",
            **self.context(self.crew),
        )
        after = self.clock.now

        def red(instant):
            row = [
                r
                for r in vehicle_checklist(*self.args(), as_of=instant).rows
                if r.item_id == str(self.suction.pk)
            ][0]
            return row.attention.red, len(row.deficiencies)

        self.assertEqual(red(before), (False, 0))
        self.assertEqual(red(recorded), (True, 1))
        self.assertEqual(red(after), (False, 0))
        self.assertEqual(red(recorded), (True, 1))  # unchanged by the later correction
        self.assertEqual(
            item_attention(*self.args(), [(str(self.suction.pk), NO_KIT)], recorded)[
                (str(self.suction.pk), NO_KIT)
            ].red,
            True,
        )

    def test_an_invalidated_chain_is_not_the_latest_effective_check(self):
        from cya.checks.commands import invalidate_chain

        first = self.record(self.suction).body["check_id"]
        self.clock.set(self.clock.now + timedelta(minutes=1))
        bad = self.failed(self.suction, deficiency=None, outcome="PASSED")  # a second Pass
        invalidate_chain(
            check_id=bad.body["check_id"],
            explanation="Synthetic reason.",
            **self.context(self.admin),
        )
        self.assertFalse(self.row(self.suction).attention.red)
        self.assertTrue(first)

    def test_bad_input_is_refused(self):
        for kwargs, code in (
            ({"view": "SIDEWAYS"}, "INVALID_VIEW"),
            ({"needs_attention_only": "yes"}, "INVALID_FILTER"),
            ({"as_of": self.clock.now.replace(tzinfo=None)}, "INVALID_AS_OF"),
        ):
            with self.subTest(code=code), self.assertRaises(ChecklistQueryError) as caught:
                vehicle_checklist(*self.args(), **kwargs)
            self.assertEqual(caught.exception.code, code)

    def test_an_unknown_or_foreign_vehicle_is_not_found(self):
        from uuid import uuid4

        with self.assertRaises(ChecklistQueryError) as caught:
            vehicle_checklist(str(self.org.pk), str(uuid4()))
        self.assertEqual((caught.exception.code, caught.exception.status), ("NOT_FOUND", 404))


class BindingAndKitHistoryTests(AttentionCase):
    """Review findings on PR 135 (https://github.com/JustApu/CheckYourAmbulance-archive/pull/135): bound templates only, and kit membership as it was."""

    def second_template(self):
        """A published template with one new item, not bound to any vehicle yet."""
        item = Item.objects.create(organization=self.org, label="Synthetic second-template item")
        template = ChecklistTemplate.objects.create(
            organization=self.org, label="Synthetic second checklist"
        )
        version = checklist_commands.save_template_draft(
            template_id=str(template.pk), items=[self.spec(item, 5)], **self.context(self.admin)
        ).body["version_id"]
        checklist_commands.publish_template_version(
            version_id=version, effective_at=None, **self.context(self.admin)
        )
        return template, item

    def listed(self, item, vehicle=None, **kw):
        vehicle = vehicle or self.vehicle
        rows = vehicle_checklist(str(self.org.pk), str(vehicle.pk), **kw).rows
        return any(r.item_id == str(item.pk) for r in rows)

    def test_a_published_but_unbound_template_adds_nothing_to_the_checklist(self):
        template, item = self.second_template()
        self.assertFalse(self.listed(item))
        self.assertFalse(self.listed(item, self.vehicle_b))

    def test_a_template_bound_to_another_vehicle_does_not_reach_this_one(self):
        template, item = self.second_template()
        self.bind(self.vehicle_b, template=template, reconcile=False)
        self.assertTrue(self.listed(item, self.vehicle_b))
        self.assertFalse(self.listed(item))

    def test_binding_and_unbinding_follow_the_instant(self):
        template, item = self.second_template()
        before = self.clock.now
        self.bind(self.vehicle, template=template, reconcile=False)
        bound = self.clock.now
        self.bind(self.vehicle, template=template, action="UNBIND", reconcile=False)
        self.assertFalse(self.listed(item))
        self.assertTrue(self.listed(item, as_of=bound))  # reproduced after the unbind
        self.assertFalse(self.listed(item, as_of=before))

    def test_a_past_checklist_keeps_the_kits_that_were_there_after_a_move_or_deactivation(self):
        before = self.clock.now
        self.move(self.kit_b, self.vehicle_b)  # kit B leaves vehicle A
        self.deactivate(self.reference)
        self.assertEqual(self.kit_ids(), [])
        self.assertEqual(self.kit_ids(as_of=before), sorted([self.reference, self.kit_b]))

    def test_a_kit_added_later_is_not_in_the_past_checklist(self):
        before = self.clock.now
        added = self.add_kit("Added later").body["kit_id"]
        self.assertIn(added, self.kit_ids())
        self.assertNotIn(added, self.kit_ids(as_of=before))

    def kit_ids(self, **kw):
        rows = vehicle_checklist(str(self.org.pk), str(self.vehicle.pk), **kw).rows
        return sorted(r.kit_id for r in rows if r.item_id == str(self.kit_item.pk))
