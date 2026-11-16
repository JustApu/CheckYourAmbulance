"""Template creation, new items, the stale-edit check and the impact preview (issue #130 part 6a)."""

from datetime import timedelta
from unittest import mock

from cya.checklists import commands
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistTemplate,
    ChecklistVersion,
    Item,
    Vehicle,
)
from cya.checklists.validation import ChecklistValidationError
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent, CommandReceipt
from cya.kits.keys import KeyRuleError
from cya.kits.positions import revise_position
from cya.kits.tests.support import KitCase
from cya.operations.templates import TemplateError, create_template, publication_impact


class CreateTemplateTests(KitCase):
    def test_a_supervisor_creates_a_template_with_its_key_and_no_versions(self):
        body = create_template(
            label="  Synthetic daily  ", key="daily", **self.context(self.supervisor)
        ).body
        template = ChecklistTemplate.objects.get(pk=body["template_id"])
        self.assertEqual(
            (template.label, template.key, template.active), ("Synthetic daily", "daily", True)
        )
        self.assertFalse(ChecklistVersion.objects.filter(template=template).exists())
        receipt = CommandReceipt.objects.get(command_type="TEMPLATE_CREATE")
        self.assertEqual(str(receipt.actor_user_id), str(self.supervisor.pk))

    def test_a_taken_key_a_bad_key_and_a_blank_name_are_refused(self):
        create_template(label="One", key="daily", **self.context())
        with self.assertRaisesMessage(TemplateError, "another template has that key"):
            create_template(label="Two", key="daily", **self.context())
        with self.assertRaises(KeyRuleError):
            create_template(label="Two", key="Daily!", **self.context())
        with self.assertRaises(TemplateError):
            create_template(label="   ", key="weekly", **self.context())
        self.assertEqual(ChecklistTemplate.objects.filter(key__isnull=False).count(), 1)

    def test_crew_and_supply_officers_are_denied_and_audited(self):
        for actor in (self.crew, self.officer):
            with self.subTest(actor=actor.idp_subject), self.assertRaises(CommandDenied):
                create_template(label="Synthetic", key="denied", **self.context(actor))
        self.assertFalse(ChecklistTemplate.objects.filter(key="denied").exists())
        self.assertEqual(
            AuditEvent.objects.filter(action="TEMPLATE_CREATE", outcome="DENIED").count(), 2
        )


class DraftSaveTests(KitCase):
    def save(self, items, **kw):
        return commands.save_template_draft(
            template_id=str(self.template.pk), items=items, **kw, **self.context()
        )

    def new(self, label, order=40, **overrides):
        spec = self.spec(self.suction, order, **overrides)
        del spec["item_id"]
        return {"new_item": label, **spec}

    def test_a_new_item_by_name_is_created_with_the_draft(self):
        result = self.save([*self.base_items(), self.new("  Synthetic tourniquet ")])
        item = Item.objects.get(label="Synthetic tourniquet")
        self.assertTrue(item.active)
        self.assertEqual(result.body["new_item_ids"], [str(item.pk)])
        self.assertTrue(
            ChecklistItemDef.objects.filter(
                checklist_version_id=result.body["version_id"], item=item
            ).exists()
        )

    def test_a_replayed_save_creates_the_new_item_once(self):
        context = self.context()
        items = [self.new("Synthetic splint")]
        first = commands.save_template_draft(
            template_id=str(self.template.pk), items=items, **context
        )
        again = commands.save_template_draft(
            template_id=str(self.template.pk), items=items, **context
        )
        self.assertEqual(first.body, again.body)
        self.assertEqual(Item.objects.filter(label="Synthetic splint").count(), 1)

    def test_a_name_an_active_item_has_twice_named_or_with_an_item_id_is_refused(self):
        with self.assertRaisesMessage(ChecklistValidationError, "choose it instead"):
            self.save([self.new("Synthetic masks")])
        with self.assertRaisesMessage(ChecklistValidationError, "named twice"):
            self.save([self.new("Synthetic cot"), self.new("Synthetic cot", 50)])
        with self.assertRaisesMessage(ChecklistValidationError, "not both"):
            self.save([{**self.new("Synthetic cot"), "item_id": str(self.masks.pk)}])
        with self.assertRaises(ChecklistValidationError):
            self.save([self.new("  ")])
        self.assertFalse(Item.objects.filter(label="Synthetic cot").exists())
        self.assertFalse(ChecklistVersion.objects.filter(template=self.template).exists())

    def test_an_inactive_item_with_the_name_does_not_block_a_new_one(self):
        Item.objects.filter(pk=self.siren.pk).update(active=False)
        self.save([self.new("Synthetic siren")])
        self.assertEqual(Item.objects.filter(label="Synthetic siren", active=True).count(), 1)

    def test_a_save_based_on_an_older_revision_is_refused(self):
        self.save(self.base_items(), base_revision=0)
        self.save(self.base_items()[:2], base_revision=1)
        with self.assertRaisesMessage(ChecklistValidationError, "changed since this edit started"):
            self.save(self.base_items()[:1], base_revision=1)
        self.assertEqual(ChecklistVersion.objects.filter(template=self.template).count(), 2)
        with self.assertRaises(ChecklistValidationError):
            self.save(self.base_items(), base_revision=-1)

    def test_a_save_without_the_new_inputs_keeps_its_result_and_digest(self):
        result = self.save(self.base_items())
        self.assertEqual(set(result.body), {"code", "version_id", "revision", "item_count"})


class ImpactTests(KitCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600")

    def impact(self, version_id, effective_at=None):
        return publication_impact(str(self.org.pk), version_id, effective_at)

    def test_items_added_removed_and_changed_against_the_version_in_force(self):
        self.published_version()
        changed = [
            self.spec(self.suction, 10, instructions="Synthetic, changed."),
            self.spec(self.kit_item, 40),
        ]
        impact = self.impact(self.draft(changed))
        self.assertEqual(impact.added, ["Synthetic kit contents"])
        self.assertEqual(sorted(impact.removed), ["Synthetic masks", "Synthetic siren"])
        self.assertEqual(impact.changed, [("Synthetic suction", ["instructions"])])
        self.assertEqual(impact.in_force.revision, 1)
        self.assertFalse(impact.refused)

    def test_a_bound_vehicle_with_an_override_needs_a_new_override(self):
        v1 = self.published_version()
        self.bind()
        override = commands.save_override_draft(
            vehicle_id=str(self.vehicle.pk),
            template_version_id=v1,
            entries=[{"item_id": str(self.siren.pk), "action": "REMOVE"}],
            **self.context(),
        ).body["override_version_id"]
        commands.publish_override_version(override_version_id=override, **self.context())
        impact = self.impact(self.draft())
        entries = {str(v.vehicle.pk): v for v in impact.vehicles}
        self.assertEqual(list(entries), [str(self.vehicle.pk)])
        self.assertIn(
            "override (revision 1) overlays an earlier version",
            entries[str(self.vehicle.pk)].notes[0],
        )
        self.assertFalse(impact.refused)

    def test_an_override_scheduled_for_later_is_also_listed(self):
        v1 = self.published_version()
        self.bind(vehicle=self.vehicle_b)
        override = commands.save_override_draft(
            vehicle_id=str(self.vehicle_b.pk),
            template_version_id=v1,
            entries=[{"item_id": str(self.siren.pk), "action": "REMOVE"}],
            **self.context(),
        ).body["override_version_id"]
        commands.publish_override_version(
            override_version_id=override,
            effective_at=self.clock.now + timedelta(days=2),
            **self.context(),
        )
        impact = self.impact(self.draft())
        notes = {str(v.vehicle.pk): v.notes for v in impact.vehicles}[str(self.vehicle_b.pk)]
        self.assertEqual(len(notes), 1)
        self.assertIn("override (revision 1, taking effect 2026-06-03 00:00)", notes[0])

    def test_a_retired_position_on_a_bound_vehicle_and_a_missing_slot_block_it(self):
        self.published_version(
            [self.spec(self.siren, 10, compartment_id=None, system_id=str(self.system.pk))]
        )
        self.bind()
        revise_position(
            vehicle_id=str(self.vehicle.pk),
            kind="COMPARTMENT",
            stable_slot_id=self.slot,
            retired=True,
            **self.context(),
        )
        bare = Vehicle.objects.create(organization=self.org, display_name="Synthetic C")
        draft = self.draft([self.spec(self.masks, 10, compartment_id=str(self.cabinet_b.pk))])
        impact = self.impact(draft)
        entries = {str(v.vehicle.pk): v for v in impact.vehicles}
        self.assertIn(
            "positions are retired on it: Synthetic cabinet",
            entries[str(self.vehicle.pk)].blocking[0],
        )
        self.assertIn(
            "has no position matching Synthetic cabinet", entries[str(bare.pk)].blocking[0]
        )
        self.assertTrue(impact.refused)
        with self.assertRaises(ChecklistValidationError):
            self.publish(draft)

    def test_an_instant_not_after_the_latest_publication_blocks_it(self):
        self.published_version(effective_at=self.clock.now + timedelta(days=3))
        impact = self.impact(self.draft(), self.clock.now + timedelta(days=1))
        self.assertTrue(any("after the latest publication" in r for r in impact.blocking))
        self.assertTrue(impact.refused)

    def test_a_published_version_has_no_preview(self):
        with self.assertRaisesMessage(TemplateError, "already published"):
            self.impact(self.published_version())

    def test_the_preview_reads_a_bounded_set_of_vehicles(self):
        from cya.operations import templates as template_rules

        self.published_version()
        self.bind()
        self.bind(vehicle=self.vehicle_b)
        draft = self.draft()
        with mock.patch.object(template_rules, "VEHICLE_LIMIT", 1):
            impact = self.impact(draft)
        self.assertTrue(impact.more_vehicles)
        self.assertLessEqual(len(impact.vehicles), 1)
        self.assertFalse(self.impact(draft).more_vehicles)
