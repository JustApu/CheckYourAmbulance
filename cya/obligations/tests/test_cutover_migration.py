"""The SPEC 5.0.0 migration to vehicle-owned checklists (18.2, AC-40(2), (5), (8); issue #220).

One synthetic legacy history covers the cases the migration must carry over unchanged:
- vehicle A follows template T on two slots and template T2 on one of them, and both require
  the masks (a shared routine anchor, CHK-10);
- vehicle B follows T with an override removing the suction;
- T has a second version published for day 3 (with B's override republished over it), and B's
  binding is unbound from day 4.
Obligations for the first two days are generated before the cutover at day 1, 12:00.
"""

import io
from datetime import time
from unittest import mock
from uuid import uuid4

from django.core.management import CommandError, call_command

from cya.checklists import commands as checklist_commands
from cya.checklists.cutover import CutoverError, migrate_to_vehicle_checklists, plan_cutover
from cya.checklists.models import (
    ChecklistCutover,
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
    LegacyChecklistMap,
    LegacyPositionMap,
)
from cya.core.audit_commands import CommandDenied
from cya.kits import keys
from cya.kits.models import VehiclePositionKey
from cya.obligations.models import ObligationSelection, RequirementBinding
from cya.obligations.requirements import desired_at, occurrences
from cya.obligations.state import NO_KIT
from cya.obligations.tests.support import ObligationCase
from cya.obligations.unbinding import unbind_preview


class MigrationCase(ObligationCase):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.vehicles
        self.v1 = self.published_version()
        self.second = ChecklistTemplate.objects.create(organization=self.org, label="Second")
        draft = checklist_commands.save_template_draft(
            template_id=str(self.second.pk),
            items=[self.spec(self.masks, 5, kind="QUANTITY", expected_quantity=2)],
            **self.context(),
        ).body["version_id"]
        checklist_commands.publish_template_version(version_id=draft, **self.context())
        self.publish_schedule("test-0600")
        self.publish_schedule("test-1800", wall_time=time(18))
        self.override(self.v1)
        self.v2 = self.published_version(
            items=[
                self.spec(self.suction, 10),
                self.spec(self.masks, 20, kind="QUANTITY", expected_quantity=8),
                self.spec(self.siren, 30, compartment_id=None, system_id=str(self.system.pk)),
            ],
            effective_at=self.at(days=3),
        )
        self.override(self.v2, effective_at=self.at(days=3))
        self.bind(self.a, reconcile=False)
        self.bind(self.a, slot="test-1800", reconcile=False)
        self.bind(self.a, template=self.second, reconcile=False)
        self.bind(self.b, reconcile=False)
        self.bind(self.b, action="UNBIND", effective_at=self.at(days=4), reconcile=False)
        keys.set_position_key(
            kind="COMPARTMENT",
            stable_slot_id=str(self.cabinet.stable_slot_id),
            key="cabinet",
            **self.context(),
        )
        self.generate()
        self.org_id = str(self.org.pk)
        self.clock.set(self.at(days=1, hours=12))

    def override(self, template_version, effective_at=None):
        draft = checklist_commands.save_override_draft(
            vehicle_id=str(self.b.pk),
            template_version_id=template_version,
            entries=[{"item_id": str(self.suction.pk), "action": "REMOVE"}],
            **self.context(),
        ).body["override_version_id"]
        checklist_commands.publish_override_version(
            override_version_id=draft, effective_at=effective_at, **self.context()
        )

    def cut_over(self, actor=None, operation_id=None):
        return migrate_to_vehicle_checklists(
            organization_id=self.org_id,
            actor_id=str((actor or self.admin).pk),
            operation_id=operation_id or str(uuid4()),
        )

    def checklist(self, vehicle, legacy):
        return LegacyChecklistMap.objects.get(vehicle=vehicle, legacy_template=legacy).checklist

    def chain(self, checklist, slot="test-0600"):
        return list(
            RequirementBinding.objects.filter(template=checklist, slot=slot)
            .order_by("revision")
            .values_list("action", "effective_at")
        )

    def items_of(self, checklist, revision):
        version = ChecklistVersion.objects.get(template=checklist, revision=revision)
        return {
            str(d.item_id): d for d in ChecklistItemDef.objects.filter(checklist_version=version)
        }


class MigrationShapeTests(MigrationCase):
    def test_each_vehicle_template_pair_becomes_one_checklist_without_merging(self):
        result = self.cut_over()
        self.assertEqual(result.body["checklists"], 3)
        own_t, own_t2 = self.checklist(self.a, self.template), self.checklist(self.a, self.second)
        self.assertNotEqual(own_t.pk, own_t2.pk)
        # One template on two slots stays one checklist with two assignments (OQ-80).
        self.assertEqual(self.chain(own_t), [("BIND", self.clock.now)])
        self.assertEqual(self.chain(own_t, "test-1800"), [("BIND", self.clock.now)])
        self.assertEqual(self.chain(own_t2), [("BIND", self.clock.now)])
        for checklist in (own_t, own_t2):
            self.assertEqual(str(checklist.vehicle_id), str(self.a.pk))

    def test_precedence_follows_the_legacy_template_identity_order(self):
        self.cut_over()
        first, second = sorted([self.template, self.second], key=lambda t: str(t.pk))
        self.assertEqual(self.checklist(self.a, first).precedence, 1)
        self.assertEqual(self.checklist(self.a, second).precedence, 2)

    def test_the_override_is_folded_into_the_vehicle_checklist(self):
        self.cut_over()
        own_a, own_b = self.checklist(self.a, self.template), self.checklist(self.b, self.template)
        self.assertIn(str(self.suction.pk), self.items_of(own_a, 1))
        self.assertNotIn(str(self.suction.pk), self.items_of(own_b, 1))
        placed = self.items_of(own_b, 1)[str(self.masks.pk)]
        self.assertEqual(str(placed.compartment.vehicle_id), str(self.b.pk))

    def test_future_versions_and_assignment_ends_keep_their_instants(self):
        self.cut_over()
        own_b = self.checklist(self.b, self.template)
        publications = ChecklistPublication.objects.filter(
            checklist_version__template=own_b
        ).order_by("effective_at")
        self.assertEqual([p.effective_at for p in publications], [self.clock.now, self.at(days=3)])
        self.assertEqual(self.items_of(own_b, 2)[str(self.masks.pk)].expected_quantity, 8)
        self.assertEqual(self.chain(own_b), [("BIND", self.clock.now), ("UNBIND", self.at(days=4))])

    def test_positions_and_keys_become_vehicle_local(self):
        self.cut_over()
        for vehicle in (self.a, self.b):
            key = VehiclePositionKey.objects.get(vehicle=vehicle, kind="COMPARTMENT")
            self.assertEqual(
                (key.key, key.stable_slot_id), ("cabinet", self.cabinet.stable_slot_id)
            )
        self.assertEqual(LegacyPositionMap.objects.filter(organization=self.org).count(), 4)

    def test_legacy_rows_are_left_as_they_were(self):
        before = (
            ChecklistTemplate.objects.filter(vehicle__isnull=True).count(),
            ChecklistVersion.objects.filter(template__vehicle__isnull=True).count(),
            RequirementBinding.objects.filter(template__vehicle__isnull=True).count(),
        )
        self.cut_over()
        after = (
            ChecklistTemplate.objects.filter(vehicle__isnull=True).count(),
            ChecklistVersion.objects.filter(template__vehicle__isnull=True).count(),
            RequirementBinding.objects.filter(template__vehicle__isnull=True).count(),
        )
        self.assertEqual(before, after)


class CarryOverTests(MigrationCase):
    def selections(self):
        return list(
            ObligationSelection.objects.filter(organization=self.org)
            .order_by("vehicle_id", "due_at", "item_id", "sequence")
            .values_list("selected_obligation_id", flat=True)
        )

    def test_existing_obligations_are_not_recomputed(self):
        before = self.selections()
        self.assertTrue(before)
        self.cut_over()
        self.generate()
        # Later due instants are added, but no earlier selection is replaced or retired: every
        # obligation from before the cutover keeps its legacy requirement (SPEC 18.2 step 3).
        self.assertTrue(set(before) <= set(self.selections()))
        self.assertFalse(
            ObligationSelection.objects.filter(organization=self.org)
            .exclude(selection_type="SELECT")
            .exists()
        )

    def test_new_obligations_come_from_the_vehicle_checklists(self):
        self.cut_over()
        self.clock.set(self.at(days=2, hours=12))
        self.generate()
        own = {str(self.checklist(self.a, t).pk) for t in (self.template, self.second)}
        later = [o for o in self.obligations(vehicle=self.a) if o.due_at >= self.at(days=3)]
        self.assertTrue(later)
        for obligation in later:
            template = obligation.requirement_version.publication.checklist_version.template_id
            self.assertIn(str(template), own)

    def test_the_shared_anchor_keeps_its_winner(self):
        due = self.at(days=2, hours=6)
        a = str(self.a.pk)

        def winner(cutoff):
            found = occurrences(self.org_id, a, due, due, cutoff)
            desired = desired_at(self.org_id, a, due, found, cutoff)
            return desired.items[(str(self.masks.pk), NO_KIT)].publication_id

        before = ChecklistPublication.objects.get(pk=winner(None)).checklist_version.template_id
        self.cut_over()
        after = ChecklistPublication.objects.get(pk=winner(None)).checklist_version.template
        self.assertEqual(
            str(LegacyChecklistMap.objects.get(checklist=after).legacy_template_id), str(before)
        )

    def test_ending_a_migrated_assignment_lists_the_legacy_pinned_obligations(self):
        self.cut_over()
        own = self.checklist(self.a, self.template)
        preview = unbind_preview(
            self.org_id, str(self.a.pk), str(own.pk), "test-1800", self.clock.now
        )
        # Day 1 at 18:00 was generated before the cutover; its window has not opened yet.
        self.assertGreaterEqual(preview.superseded_count, 1)


class CommandTests(MigrationCase):
    def test_a_rerun_replays_and_writes_nothing(self):
        operation = str(uuid4())
        first = self.cut_over(operation_id=operation)
        count = ChecklistTemplate.objects.count()
        again = self.cut_over(operation_id=operation)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(ChecklistTemplate.objects.count(), count)

    def test_only_administrators_may_migrate(self):
        with self.assertRaises(CommandDenied):
            self.cut_over(actor=self.supervisor)
        self.assertFalse(ChecklistCutover.objects.exists())

    def test_a_second_migration_is_refused(self):
        self.cut_over()
        self.grant(self.supervisor, "ADMINISTRATOR")
        with self.assertRaisesRegex(CutoverError, "already moved"):
            self.cut_over(actor=self.supervisor)

    def test_a_difference_in_resolution_refuses_everything(self):
        with (
            mock.patch("cya.obligations.legacy.carried_over", return_value=False),
            self.assertRaisesRegex(CutoverError, "would change future requirements"),
        ):
            self.cut_over()
        self.assertFalse(ChecklistCutover.objects.exists())
        self.assertFalse(ChecklistTemplate.objects.filter(vehicle__isnull=False).exists())

    def test_an_unresolvable_legacy_checklist_refuses_the_migration(self):
        # A third version for day 5 without B's override republished: B's override is stale.
        self.clock.set(self.at(hours=1))
        self.published_version(effective_at=self.at(days=5))
        self.clock.set(self.at(days=1, hours=12))
        plan = plan_cutover(self.org_id)
        self.assertTrue(any("override" in p for p in plan.problems), plan.problems)
        with self.assertRaises(CutoverError):
            self.cut_over()
        self.assertFalse(ChecklistCutover.objects.exists())

    def test_the_management_command_dry_run_writes_nothing(self):
        out = io.StringIO()
        call_command(
            "migrate_vehicle_checklists",
            organization=self.org_id,
            actor_user_id=str(self.admin.pk),
            dry_run=True,
            stdout=out,
        )
        self.assertIn("checklists: 3", out.getvalue())
        self.assertFalse(ChecklistCutover.objects.exists())
        call_command(
            "migrate_vehicle_checklists",
            organization=self.org_id,
            actor_user_id=str(self.admin.pk),
            stdout=out,
        )
        self.assertTrue(ChecklistCutover.objects.exists())
        with self.assertRaisesRegex(CommandError, "Administrators only"):
            call_command(
                "migrate_vehicle_checklists",
                organization=self.org_id,
                actor_user_id=str(self.crew.pk),
                stdout=out,
            )
