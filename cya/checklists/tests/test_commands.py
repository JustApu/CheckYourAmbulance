"""Draft and publish commands: validation, permissions, replay, effective instants."""

from datetime import timedelta

from django.utils import timezone

from cya.checklists import commands
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistVersion,
    Compartment,
    Vehicle,
    VehicleSystem,
)
from cya.checklists.tests.support import ChecklistCase, ids
from cya.checklists.validation import ChecklistValidationError
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent


class DraftTests(ChecklistCase):
    def test_draft_saves_revisions_and_audits(self):
        first, second = self.draft(), self.draft()
        versions = ChecklistVersion.objects.filter(template=self.template).order_by("revision")
        self.assertEqual([v.pk for v in versions], [first, second])
        self.assertEqual([v.revision for v in versions], [1, 2])
        self.assertEqual(ChecklistItemDef.objects.filter(checklist_version_id=first).count(), 3)
        audit = AuditEvent.objects.get(
            action="CHECKLIST_DRAFT_SAVE", new_snapshot__values__revision=1
        )
        self.assertEqual(audit.outcome, "SUCCEEDED")
        self.assertEqual(audit.actor_user_id, self.admin.pk)
        self.assertEqual(audit.roles_at_time, ["ADMINISTRATOR", "CREW"])

    def test_same_operation_replays_without_a_second_revision(self):
        context = self.context()
        args = dict(template_id=str(self.template.pk), items=self.base_items(), **context)
        first = commands.save_template_draft(**args)
        again = commands.save_template_draft(**args)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(ChecklistVersion.objects.count(), 1)

    def test_only_supervisor_or_administrator_may_draft_and_publish(self):
        for actor in (self.crew,):
            with self.subTest(actor=actor.idp_subject):
                with self.assertRaises(CommandDenied):
                    self.draft(actor=actor)
        self.assertEqual(ChecklistVersion.objects.count(), 0)
        denied = AuditEvent.objects.get(action="CHECKLIST_DRAFT_SAVE", outcome="DENIED")
        self.assertEqual(denied.actor_user_id, self.crew.pk)
        version_id = self.draft(actor=self.supervisor)
        with self.assertRaises(CommandDenied):
            self.publish(version_id, actor=self.crew)
        self.publish(version_id, actor=self.supervisor)
        self.assertEqual(ChecklistPublication.objects.get().actor_user_id, self.supervisor.pk)

    def test_rejected_definitions_write_nothing(self):
        valid = self.spec(self.suction)
        bad = {
            "empty": [],
            "not a list": {"item_id": "x"},
            "unknown field": [{**valid, "bulk_eligible": True}],
            "missing field": [{k: v for k, v in valid.items() if k != "kind"}],
            "bad kind": [{**valid, "kind": "OTHER"}],
            "quantity on presence": [{**valid, "expected_quantity": 1}],
            "quantity missing": [{**valid, "kind": "QUANTITY"}],
            "bool quantity": [{**valid, "kind": "QUANTITY", "expected_quantity": True}],
            "negative quantity": [{**valid, "kind": "QUANTITY", "expected_quantity": -1}],
            "measurement without unit": [{**valid, "kind": "MEASUREMENT"}],
            "unit on presence": [{**valid, "unit": "psi"}],
            "not applicable": [{**valid, "allowed_outcomes": ["PASSED", "NOT_APPLICABLE"]}],
            "no passed": [{**valid, "allowed_outcomes": ["FAILED"]}],
            "duplicate outcome": [{**valid, "allowed_outcomes": ["PASSED", "PASSED"]}],
            "bad order": [{**valid, "display_order": -1}],
            "long instructions": [{**valid, "instructions": "x" * 4001}],
            "no placement": [{**valid, "compartment_id": None}],
            "both placements": [{**valid, "system_id": str(self.system.pk)}],
            "kit without compartment": [
                {**valid, "compartment_id": None, "system_id": str(self.system.pk), "kit_id": "x"}
            ],
            "unknown item": [{**valid, "item_id": "00000000-0000-4000-8000-000000000000"}],
            "malformed item": [{**valid, "item_id": "nope"}],
            "duplicate item": [valid, {**valid, "display_order": 11}],
        }
        for name, items in bad.items():
            with self.subTest(name), self.assertRaises(ChecklistValidationError):
                self.draft(items)
        self.assertEqual(ChecklistVersion.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.filter(outcome="FAILED").count(), len(bad))

    def test_inactive_item_and_template_are_rejected(self):
        self.suction.active = False
        self.suction.save()
        with self.assertRaises(ChecklistValidationError):
            self.draft([self.spec(self.suction)])
        self.template.active = False
        self.template.save()
        with self.assertRaises(ChecklistValidationError):
            self.draft([self.spec(self.masks)])


class PublishTests(ChecklistCase):
    def test_publish_assigns_effective_instant_and_freezes(self):
        version_id = self.draft()
        before = timezone.now()
        result = self.publish(version_id)
        publication = ChecklistPublication.objects.get(checklist_version_id=version_id)
        self.assertTrue(result.body["effective_immediately"])
        self.assertEqual(publication.effective_at, publication.recorded_at)
        self.assertGreaterEqual(publication.recorded_at, before)
        self.assertEqual(publication.actor_roles_at_time, ["ADMINISTRATOR", "CREW"])

    def test_cannot_publish_twice_or_publish_unknown_versions(self):
        version_id = self.published_version()
        with self.assertRaises(ChecklistValidationError):
            self.publish(version_id)
        with self.assertRaises(ChecklistValidationError):
            self.publish("00000000-0000-4000-8000-000000000000")
        self.assertEqual(ChecklistPublication.objects.count(), 1)

    def test_effective_instant_rules(self):
        first = self.draft()
        with self.assertRaises(ChecklistValidationError):
            self.publish(first, effective_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(ChecklistValidationError):
            self.publish(first, effective_at=timezone.now().replace(tzinfo=None))
        first_at = timezone.now() + timedelta(days=2)
        self.publish(first, effective_at=first_at)
        second = self.draft()
        for when in (timezone.now(), first_at):
            with self.subTest(when=when), self.assertRaises(ChecklistValidationError):
                self.publish(second, effective_at=when)
        self.publish(second, effective_at=timezone.now() + timedelta(days=3))
        self.assertEqual(ChecklistPublication.objects.count(), 2)

    def test_publish_validates_the_resolved_set_for_every_active_vehicle(self):
        # Vehicle C has no cabinet slot, so the checklist cannot resolve for it.
        Vehicle.objects.create(organization=self.org, display_name="Synthetic C")
        version_id = self.draft()
        with self.assertRaisesMessage(ChecklistValidationError, "no effective slot"):
            self.publish(version_id)
        self.assertFalse(ChecklistPublication.objects.exists())
        Vehicle.objects.filter(display_name="Synthetic C").update(active=False)
        self.publish(version_id)

    def test_slot_effective_only_later_blocks_an_earlier_effective_instant(self):
        vehicle = Vehicle.objects.create(organization=self.org, display_name="Synthetic D")
        later = timezone.now() + timedelta(days=30)
        for model, reference in ((Compartment, self.cabinet), (VehicleSystem, self.system)):
            model.objects.create(
                organization=self.org,
                vehicle=vehicle,
                stable_slot_id=reference.stable_slot_id,
                revision=1,
                label="Synthetic later slot",
                effective_at=later,
            )
        version_id = self.draft()
        with self.assertRaisesMessage(ChecklistValidationError, "no effective slot"):
            self.publish(version_id)
        self.publish(version_id, effective_at=later + timedelta(days=1))

    def test_empty_draft_is_not_publishable(self):
        empty = ChecklistVersion.objects.create(
            organization=self.org, template=self.template, revision=9, created_at=timezone.now()
        )
        with self.assertRaisesMessage(ChecklistValidationError, "without items"):
            self.publish(str(empty.pk))

    def test_publish_replay_is_idempotent(self):
        version_id = self.draft()
        context = self.context()
        first = commands.publish_template_version(version_id=version_id, **context)
        again = commands.publish_template_version(version_id=version_id, **context)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(ChecklistPublication.objects.count(), 1)

    def test_new_operation_ids_are_independent(self):
        self.assertNotEqual(ids()["operation_id"], ids()["operation_id"])
