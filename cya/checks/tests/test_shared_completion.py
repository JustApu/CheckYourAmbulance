"""One check completes its item wherever it is due (SPEC 6.0.0 SCH-2, SCH-5, SCH-6, CON-7, AC-30).

Issue #256 Part B. Two checklists bound to one vehicle list the suction item in the same place with
the same requirements, on different schedule slots, so their windows overlap: checklist A is due at
06:00 (window 05:00-07:00, late limit 19:00) and checklist B at 07:00 (06:00-08:00, late limit
20:00). A check links to every on-time obligation and every late one still within its late limit,
from either checklist or the same one, and to nothing past its late limit.

Until item requirements exist (Part C), an interim guard links only obligations whose requirements
and place match the requirement the check was recorded against, so a check never completes the
same item kept in another place or checked another way.
"""

from datetime import time, timedelta

from django.db import DatabaseError, connection, transaction

from cya.accounts.fields import new_id
from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistTemplate, Compartment
from cya.checks.commands import correct_check, invalidate_chain, record_check
from cya.checks.conflicts import CheckConflict
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.checks.tests.test_obligation_links import ObligationLinkCleanupMixin
from cya.core.models import CommandReceipt
from cya.deficiencies.models import DeficiencyCategory
from cya.deficiencies.severity import ensure_default_severities
from cya.obligations.commands import BindingError, configure_shared_satisfaction_rule
from cya.obligations.models import Obligation, SharedSatisfactionRule
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.obligations.tests.support import ObligationCase


class SharedCompletionCase(ObligationLinkCleanupMixin, ObligationCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600", recurrence="DAILY")
        self.publish_schedule("test-0700", wall_time=time(7), recurrence="DAILY")
        self.version_a = self.published_version(self.base_items())
        self.bind(reconcile=False)
        effective_at = self.clock_start - timedelta(days=1)
        ensure_default_severities(self.org.pk, effective_at)
        self.deficiency_category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=new_id(),
            revision=1,
            label="Synthetic inspection restriction",
            supply_related=False,
            effective_at=effective_at,
        )

    def add_checklist_b(self, **suction):
        """Bind checklist B on the 07:00 slot; it lists suction with the given overrides."""
        self.template_b = ChecklistTemplate.objects.create(
            organization=self.org, label="Synthetic checklist B"
        )
        self.version_b = checklist_commands.save_template_draft(
            template_id=str(self.template_b.pk),
            items=[self.spec(self.suction, 10, **suction)],
            **self.context(self.admin),
        ).body["version_id"]
        checklist_commands.publish_template_version(
            version_id=self.version_b, effective_at=None, **self.context(self.admin)
        )
        self.bind(template=self.template_b, slot="test-0700", reconcile=False)
        self.assertEqual(self.generate().failures, [])

    def obligation(self, hours, days=0):
        return Obligation.objects.get(
            vehicle_id=self.vehicle.pk,
            item_id=self.suction.pk,
            due_at=self.at(days=days, hours=hours),
        )

    def record(self, version_id, *, actor=None, outcome="PASSED", **extra):
        if outcome == "UNABLE_TO_INSPECT":
            extra.setdefault("explanation", "Synthetic access restriction.")
            extra.setdefault(
                "deficiency",
                {
                    "create": {
                        "category_id": str(self.deficiency_category.stable_category_id),
                        "severity": "HIGH",
                        "summary": "Synthetic access restriction",
                        "description": "Synthetic access restriction blocks inspection.",
                    }
                },
            )
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=version_id,
            outcome=outcome,
            purpose="ROUTINE",
            **extra,
            **self.context(actor or self.crew),
        )

    def linked(self, response):
        return {
            (link["obligation_id"], link["timing"]) for link in response.body["obligation_links"]
        }

    def state(self, obligation, as_of=None):
        rows = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            as_of or self.clock.now,
            organization_id=str(self.org.pk),
            start=obligation.due_at,
            end=obligation.due_at,
        )
        return next(r.state for r in rows if r.obligation.id == str(obligation.pk))


class SameRequirementTests(SharedCompletionCase):
    def setUp(self):
        super().setUp()
        self.add_checklist_b()
        self.a, self.b = self.obligation(6), self.obligation(7)

    def test_overlapping_windows_of_two_checklists_are_both_linked(self):
        self.clock.set(self.at(hours=6, minutes=30))
        response = self.record(self.version_b)
        self.assertEqual(
            self.linked(response), {(str(self.a.pk), "ON_TIME"), (str(self.b.pk), "ON_TIME")}
        )
        self.assertEqual(
            [self.state(self.a), self.state(self.b)], [State.SATISFIED, State.SATISFIED]
        )
        rows = CheckObligationLink.objects.filter(check_record_id=response.body["check_id"])
        self.assertEqual(sorted(r.ordinal for r in rows), [0, 1])
        self.assertTrue(all(r.shared_rule_id is None for r in rows))

    def test_an_on_time_and_a_late_obligation_from_another_checklist_are_both_linked(self):
        self.clock.set(self.at(hours=7, minutes=30))  # A is past G (07:00), B is on time
        response = self.record(self.version_b)
        self.assertEqual(
            self.linked(response), {(str(self.a.pk), "LATE"), (str(self.b.pk), "ON_TIME")}
        )
        # The on-time link is the primary one.
        self.assertEqual(response.body["obligation_links"][0]["obligation_id"], str(self.b.pk))

    def test_an_obligation_past_its_late_limit_is_not_linked(self):
        self.clock.set(self.at(hours=19, minutes=30))  # A's L is 19:00, B's is 20:00
        response = self.record(self.version_a)
        self.assertEqual(self.linked(response), {(str(self.b.pk), "LATE")})
        self.assertEqual(self.state(self.a), State.OVERDUE)

    def test_a_repeat_check_links_nothing_once_both_are_satisfied(self):
        self.clock.set(self.at(hours=6, minutes=30))
        self.record(self.version_a)
        self.clock.set(self.at(hours=6, minutes=40))
        self.assertEqual(self.record(self.version_b).body["obligation_links"], [])

    def test_unable_to_inspect_links_both_and_satisfies_neither(self):
        self.clock.set(self.at(hours=6, minutes=30))
        response = self.record(self.version_a, actor=self.supervisor, outcome="UNABLE_TO_INSPECT")
        self.assertEqual(len(response.body["obligation_links"]), 2)
        self.assertNotIn(self.state(self.a), (State.SATISFIED,))
        self.assertNotIn(self.state(self.b), (State.SATISFIED,))

    def test_invalidation_voids_every_link_and_reopens_both(self):
        self.clock.set(self.at(hours=6, minutes=30))
        check_id = self.record(self.version_a).body["check_id"]
        self.clock.set(self.at(hours=6, minutes=45))
        invalidate_chain(
            check_id=check_id, explanation="Synthetic invalid evidence.", **self.context(self.admin)
        )
        self.assertNotEqual(self.state(self.a), State.SATISFIED)
        self.assertNotEqual(self.state(self.b), State.SATISFIED)
        # A fresh check may now take both over.
        self.clock.set(self.at(hours=6, minutes=50))
        self.assertEqual(len(self.record(self.version_a).body["obligation_links"]), 2)

    def test_a_correction_re_derives_all_links_at_the_root_instant(self):
        self.clock.set(self.at(hours=6, minutes=30))
        original = self.record(self.version_a).body["check_id"]
        self.clock.set(self.at(hours=9))
        corrected = correct_check(
            check_id=original,
            expected_tip_check_id=original,
            outcome="PASSED",
            **self.context(self.crew),
        )
        self.assertEqual(
            self.linked(corrected),
            {(str(self.a.pk), "ON_TIME"), (str(self.b.pk), "ON_TIME")},
        )
        replacement = CheckRecord.objects.get(pk=corrected.body["check_id"])
        self.assertEqual(CheckObligationLink.objects.filter(check_record=replacement).count(), 2)

    def test_a_replay_returns_the_same_links_and_writes_nothing(self):
        self.clock.set(self.at(hours=6, minutes=30))
        context = self.context(self.crew)
        args = {
            "vehicle_id": str(self.vehicle.pk),
            "item_id": str(self.suction.pk),
            "checklist_version_id": self.version_a,
            "outcome": "PASSED",
            "purpose": "ROUTINE",
            **context,
        }
        first = record_check(**args)
        before = (CheckObligationLink.objects.count(), CommandReceipt.objects.count())
        self.clock.set(self.at(hours=6, minutes=35))
        again = record_check(**args)
        self.assertEqual(again.body["obligation_links"], first.body["obligation_links"])
        self.assertEqual(
            (CheckObligationLink.objects.count(), CommandReceipt.objects.count()), before
        )

    def test_a_check_from_the_other_checklists_session_warns_of_a_newer_check(self):
        # CON-3 and CON-7: the comparison covers every obligation of the item, so a crew member
        # on checklist B sees the check a colleague just recorded on checklist A.
        self.clock.set(self.at(hours=6, minutes=30))
        first = self.record(self.version_a)
        colleague = self.make_user("crew-two")
        self.clock.set(self.at(hours=6, minutes=40))
        with self.assertRaises(CheckConflict) as caught:
            self.record(self.version_b, actor=colleague)
        self.assertEqual(caught.exception.newest["check_id"], first.body["check_id"])


class SameChecklistLateTests(SharedCompletionCase):
    def test_the_same_checklists_earlier_late_obligation_is_linked_too(self):
        self.bind(slot="test-0700", reconcile=False)  # checklist A on both slots
        self.assertEqual(self.generate().failures, [])
        earlier, later = self.obligation(6), self.obligation(7)
        self.clock.set(self.at(hours=7, minutes=30))
        response = self.record(self.version_a)
        self.assertEqual(
            self.linked(response),
            {(str(earlier.pk), "LATE"), (str(later.pk), "ON_TIME")},
        )


class InterimGuardTests(SharedCompletionCase):
    """Until item requirements (Part C), only matching requirement and place are shared."""

    def test_another_place_is_not_completed(self):
        other = self.make_slot(Compartment, "second cabinet")
        self.add_checklist_b(compartment_id=str(other.pk))
        a, b = self.obligation(6), self.obligation(7)
        # The check is anchored on the requirement of the checklist it was recorded from, so a
        # check in B's place completes B's obligation, not A's earlier-due one in another place.
        self.clock.set(self.at(hours=6, minutes=30))
        from_b = self.record(self.version_b)
        self.assertEqual(self.linked(from_b), {(str(b.pk), "ON_TIME")})
        self.assertNotEqual(self.state(a), State.SATISFIED)
        self.clock.set(self.at(hours=6, minutes=40))
        from_a = self.record(self.version_a, basis_check_id=from_b.body["check_id"])
        self.assertEqual(self.linked(from_a), {(str(a.pk), "ON_TIME")})

    def test_a_different_requirement_is_not_completed(self):
        self.add_checklist_b(kind="QUANTITY", expected_quantity=2)
        a, b = self.obligation(6), self.obligation(7)
        self.clock.set(self.at(hours=6, minutes=30))
        self.assertEqual(self.linked(self.record(self.version_a)), {(str(a.pk), "ON_TIME")})
        self.assertNotEqual(self.state(b), State.SATISFIED)


class LinkGuardTests(SharedCompletionCase):
    def setUp(self):
        super().setUp()
        self.add_checklist_b()

    def insert_link(self, check, obligation, ordinal, **extra):
        with transaction.atomic():
            CheckObligationLink.objects.create(
                organization=self.org,
                check_record=check,
                obligation=obligation,
                ordinal=ordinal,
                command=check.command,
                recorded_at=check.recorded_at,
                actor_user=check.actor_user,
                actor_roles_at_time=check.actor_roles_at_time,
                **extra,
            )

    def test_the_database_accepts_a_second_link_of_the_same_item_without_a_rule(self):
        self.clock.set(self.at(hours=6, minutes=30))
        response = self.record(self.version_a)
        check = CheckRecord.objects.get(pk=response.body["check_id"])
        # Both links exist and the second has no rule: the old second-link constraint is gone.
        self.assertEqual(
            list(
                CheckObligationLink.objects.filter(check_record=check)
                .order_by("ordinal")
                .values_list("ordinal", "shared_rule_id")
            ),
            [(0, None), (1, None)],
        )

    def test_the_database_refuses_a_link_to_another_items_obligation(self):
        self.clock.set(self.at(hours=5, minutes=30))
        response = self.record(self.version_a)
        check = CheckRecord.objects.get(pk=response.body["check_id"])
        masks = Obligation.objects.get(
            vehicle_id=self.vehicle.pk, item_id=self.masks.pk, due_at=self.at(hours=6)
        )
        with self.assertRaises(DatabaseError):
            self.insert_link(check, masks, 1)

    def test_the_database_refuses_a_link_before_its_primary(self):
        self.clock.set(self.at(hours=4, minutes=30))  # too early: no links
        response = self.record(self.version_a)
        check = CheckRecord.objects.get(pk=response.body["check_id"])
        with self.assertRaises(DatabaseError):
            self.insert_link(check, self.obligation(6), 1)

    def test_a_new_shared_satisfaction_rule_is_refused(self):
        a, b = self.obligation(6), self.obligation(7)
        with self.assertRaises(BindingError) as caught:
            configure_shared_satisfaction_rule(
                members=[[str(a.requirement_version_id), str(b.requirement_version_id)]],
                **self.context(self.admin),
            )
        self.assertEqual(caught.exception.code, "SHARED_RULES_RETIRED")
        self.assertFalse(SharedSatisfactionRule.objects.exists())
        # The database refuses one too, whatever the caller (SCH-5: history only).
        with connection.cursor() as cursor:
            cursor.execute("SHOW CREATE TRIGGER cya_shared_rule_bi")
            self.assertIn("shared-satisfaction rules are history only", cursor.fetchone()[2])
