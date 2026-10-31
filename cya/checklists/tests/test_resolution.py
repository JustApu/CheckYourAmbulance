"""Overlay resolution as of an instant, and stocking deviations (CHK-6, SPEC 4.5)."""

from datetime import timedelta

from django.utils import timezone

from cya.checklists import commands
from cya.checklists.models import ChecklistPublication, StockingDeviation
from cya.checklists.overlay import (
    MissingSlotError,
    NoEffectiveVersionError,
    StaleOverrideError,
    stale_override_vehicle_ids,
)
from cya.checklists.tests.support import ChecklistCase
from cya.checklists.validation import ChecklistValidationError
from cya.core.audit_commands import CommandDenied


class ResolutionTests(ChecklistCase):
    def override(self, version_id, entries, vehicle=None, effective_at=None):
        draft = commands.save_override_draft(
            vehicle_id=str((vehicle or self.vehicles[0]).pk),
            template_version_id=version_id,
            entries=entries,
            **self.context(),
        ).body["override_version_id"]
        commands.publish_override_version(
            override_version_id=draft, effective_at=effective_at, **self.context()
        )
        return draft

    def labels(self, resolved):
        return {i.requirement.item_id: i for i in resolved.items}

    def test_template_only_resolution_maps_slots_to_each_vehicle(self):
        version_id = self.published_version()
        for vehicle in self.vehicles:
            resolved = self.resolve(vehicle)
            self.assertEqual(resolved.template_version_id, version_id)
            self.assertIsNone(resolved.override_version_id)
            self.assertEqual([i.requirement.display_order for i in resolved.items], [10, 20, 30])
            item = self.labels(resolved)[str(self.suction.pk)]
            self.assertIn(vehicle.display_name, item.compartment_label)
            self.assertIn(
                vehicle.display_name, self.labels(resolved)[str(self.siren.pk)].system_label
            )

    def test_nothing_effective_before_the_first_publication(self):
        with self.assertRaises(NoEffectiveVersionError):
            self.resolve()
        self.draft()  # drafts never resolve
        with self.assertRaises(NoEffectiveVersionError):
            self.resolve()
        with self.assertRaises(ValueError):
            self.resolve(as_of=timezone.now().replace(tzinfo=None))

    def test_future_version_governs_only_from_its_effective_instant(self):
        first = self.published_version()
        start = timezone.now() + timedelta(days=1)
        second = self.published_version(items=[self.spec(self.suction, 10)], effective_at=start)
        self.assertEqual(self.resolve(as_of=timezone.now()).template_version_id, first)
        self.assertEqual(
            self.resolve(as_of=start - timedelta(seconds=1)).template_version_id, first
        )
        self.assertEqual(self.resolve(as_of=start).template_version_id, second)
        self.assertEqual(len(self.resolve(as_of=start).items), 1)
        self.assertEqual(len(self.resolve(as_of=timezone.now()).items), 3)

    def test_override_add_remove_replace_applies_to_one_vehicle(self):
        version_id = self.published_version()
        extra = type(self.masks).objects.create(organization=self.org, label="Synthetic extra")
        self.override(
            version_id,
            [
                {"item_id": str(self.suction.pk), "action": "REMOVE"},
                {
                    "item_id": str(self.masks.pk),
                    "action": "REPLACE",
                    "definition": self.spec(self.masks, 20, kind="QUANTITY", expected_quantity=4),
                },
                {
                    "item_id": str(extra.pk),
                    "action": "ADD",
                    "definition": self.spec(extra, 5),
                },
            ],
        )
        first = self.labels(self.resolve(self.vehicles[0]))
        self.assertEqual(set(first), {str(self.masks.pk), str(self.siren.pk), str(extra.pk)})
        self.assertEqual(first[str(self.masks.pk)].effective_quantity, 4)
        self.assertEqual(first[str(self.masks.pk)].requirement.source, "OVERRIDE")
        self.assertEqual(first[str(self.siren.pk)].requirement.source, "TEMPLATE")
        other = self.labels(self.resolve(self.vehicles[1]))
        self.assertEqual(set(other), {str(i.pk) for i in (self.suction, self.masks, self.siren)})
        self.assertEqual(other[str(self.masks.pk)].effective_quantity, 6)

    def test_override_applies_only_from_its_effective_instant(self):
        version_id = self.published_version()
        start = timezone.now() + timedelta(days=1)
        self.override(
            version_id, [{"item_id": str(self.suction.pk), "action": "REMOVE"}], effective_at=start
        )
        self.assertEqual(len(self.resolve(as_of=timezone.now()).items), 3)
        resolved = self.resolve(as_of=start)
        self.assertEqual(len(resolved.items), 2)
        self.assertIsNotNone(resolved.override_version_id)

    def test_later_override_revision_replaces_the_earlier_one(self):
        version_id = self.published_version()
        self.override(version_id, [{"item_id": str(self.suction.pk), "action": "REMOVE"}])
        second = self.override(version_id, [{"item_id": str(self.siren.pk), "action": "REMOVE"}])
        resolved = self.resolve()
        self.assertEqual(resolved.override_version_id, second)
        self.assertEqual(
            {i.requirement.item_id for i in resolved.items},
            {str(self.suction.pk), str(self.masks.pk)},
        )

    def test_override_must_fit_the_template_version(self):
        version_id = self.published_version()
        draft_only = self.draft()
        bad = {
            "remove an item the template lacks": [
                {"item_id": str(self.template.pk), "action": "REMOVE"}
            ],
            "add an item already present": [
                {
                    "item_id": str(self.suction.pk),
                    "action": "ADD",
                    "definition": self.spec(self.suction),
                }
            ],
            "replace without definition": [{"item_id": str(self.suction.pk), "action": "REPLACE"}],
            "remove with definition": [
                {
                    "item_id": str(self.suction.pk),
                    "action": "REMOVE",
                    "definition": self.spec(self.suction),
                }
            ],
            "definition for another item": [
                {
                    "item_id": str(self.suction.pk),
                    "action": "REPLACE",
                    "definition": self.spec(self.masks),
                }
            ],
            "unknown action": [{"item_id": str(self.suction.pk), "action": "CHANGE"}],
            "same item twice": [
                {"item_id": str(self.suction.pk), "action": "REMOVE"},
                {"item_id": str(self.suction.pk), "action": "REMOVE"},
            ],
            "no entries": [],
        }
        for name, entries in bad.items():
            with self.subTest(name), self.assertRaises(ChecklistValidationError):
                commands.save_override_draft(
                    vehicle_id=str(self.vehicles[0].pk),
                    template_version_id=version_id,
                    entries=entries,
                    **self.context(),
                )
        with self.assertRaisesMessage(ChecklistValidationError, "published template"):
            commands.save_override_draft(
                vehicle_id=str(self.vehicles[0].pk),
                template_version_id=draft_only,
                entries=[{"item_id": str(self.suction.pk), "action": "REMOVE"}],
                **self.context(),
            )

    def test_override_cannot_precede_its_template_version_or_be_republished(self):
        start = timezone.now() + timedelta(days=2)
        version_id = self.published_version(effective_at=start)
        draft = commands.save_override_draft(
            vehicle_id=str(self.vehicles[0].pk),
            template_version_id=version_id,
            entries=[{"item_id": str(self.suction.pk), "action": "REMOVE"}],
            **self.context(),
        ).body["override_version_id"]
        with self.assertRaisesMessage(ChecklistValidationError, "before the template"):
            commands.publish_override_version(override_version_id=draft, **self.context())
        commands.publish_override_version(
            override_version_id=draft, effective_at=start, **self.context()
        )
        with self.assertRaisesMessage(ChecklistValidationError, "already published"):
            commands.publish_override_version(override_version_id=draft, **self.context())

    def test_override_fails_closed_when_a_newer_template_version_takes_over(self):
        first = self.published_version()
        self.override(first, [{"item_id": str(self.suction.pk), "action": "REMOVE"}])
        start = timezone.now() + timedelta(days=1)
        second = self.published_version(effective_at=start)
        self.assertEqual(self.resolve(as_of=timezone.now()).template_version_id, first)
        with self.assertRaises(StaleOverrideError):
            self.resolve(as_of=start)
        self.assertEqual(
            stale_override_vehicle_ids(str(self.org.pk), str(self.template.pk), start),
            [str(self.vehicles[0].pk)],
        )
        self.override(
            second, [{"item_id": str(self.suction.pk), "action": "REMOVE"}], effective_at=start
        )
        self.assertEqual(self.resolve(as_of=start).template_version_id, second)
        self.assertEqual(
            stale_override_vehicle_ids(str(self.org.pk), str(self.template.pk), start), []
        )

    def test_resolution_requires_the_vehicle_to_have_the_slot(self):
        version_id = self.published_version()
        self.assertTrue(version_id)
        far_past = timezone.now() - timedelta(days=1)
        # A published version exists, but not yet at this earlier instant.
        with self.assertRaises(NoEffectiveVersionError):
            self.resolve(as_of=far_past)
        slot_missing = ChecklistPublication.objects.get().recorded_at
        self.assertIsNotNone(slot_missing)
        self.assertTrue(MissingSlotError.__mro__)


class StockingDeviationTests(ChecklistCase):
    def setUp(self):
        super().setUp()
        self.published_version()
        self.start = timezone.now() + timedelta(hours=1)
        self.end = self.start + timedelta(hours=8)

    def record(self, actor=None, **overrides):
        fields = dict(
            vehicle_id=str(self.vehicles[0].pk),
            item_id=str(self.masks.pk),
            template_id=str(self.template.pk),
            alternate_quantity=3,
            valid_from=self.start,
            valid_until=self.end,
            explanation="Synthetic supply shortage.",
        )
        fields.update(overrides)
        return commands.record_stocking_deviation(**fields, **self.context(actor))

    def quantity(self, as_of, vehicle=None):
        item = {i.requirement.item_id: i for i in self.resolve(vehicle, as_of).items}[
            str(self.masks.pk)
        ]
        return item.effective_quantity, item.deviation_id

    def test_alternate_quantity_applies_only_inside_the_window(self):
        body = self.record().body
        inside = self.start + timedelta(minutes=1)
        self.assertEqual(self.quantity(timezone.now()), (6, None))
        self.assertEqual(self.quantity(inside), (3, body["deviation_id"]))
        self.assertEqual(self.quantity(self.start), (3, body["deviation_id"]))
        self.assertEqual(self.quantity(self.end), (6, None))
        self.assertEqual(self.quantity(inside, self.vehicles[1]), (6, None))
        deviation = StockingDeviation.objects.get()
        self.assertEqual((deviation.normal_quantity, deviation.approver_id), (6, self.admin.pk))

    def test_extension_and_termination_are_new_revisions(self):
        first = self.record().body
        extended_end = self.end + timedelta(hours=4)
        second = self.record(valid_until=extended_end).body
        self.assertEqual(second["revision"], 2)
        revisions = list(StockingDeviation.objects.order_by("revision"))
        self.assertEqual(revisions[1].prior_revision_id, first["deviation_id"])
        self.assertEqual(revisions[0].valid_until, self.end)  # history is kept
        self.assertEqual(self.quantity(self.end + timedelta(hours=1)), (3, second["deviation_id"]))
        # Knowledge as of before the extension was recorded still shows the first window.
        self.assertEqual(
            [r.revision for r in revisions if r.recorded_at <= revisions[0].recorded_at], [1]
        )

    def test_started_deviation_keeps_its_start_and_can_be_terminated(self):
        first = self.record(valid_from=None, valid_until=self.end).body
        original = StockingDeviation.objects.get()
        self.assertEqual(original.valid_from, original.recorded_at)
        with self.assertRaisesMessage(ChecklistValidationError, "original start"):
            self.record(valid_from=self.start)
        terminated = timezone.now() + timedelta(seconds=30)
        revision = self.record(valid_from=None, valid_until=terminated).body
        self.assertEqual(revision["revision"], 2)
        latest = StockingDeviation.objects.get(pk=revision["deviation_id"])
        self.assertEqual(
            (latest.valid_from, latest.prior_revision_id),
            (original.valid_from, first["deviation_id"]),
        )
        after = terminated + timedelta(seconds=1)
        self.assertEqual(self.quantity(after), (6, None))
        # As of the first revision's own start, only revision 1 had been recorded.
        self.assertEqual(self.quantity(original.valid_from), (3, first["deviation_id"]))

    def test_rejected_deviations(self):
        bad = {
            "window reversed": dict(valid_from=self.end, valid_until=self.start),
            "window empty": dict(valid_until=self.start),
            "starts in the past": dict(valid_from=timezone.now() - timedelta(hours=1)),
            "naive time": dict(valid_from=self.start.replace(tzinfo=None)),
            "missing end": dict(valid_until=None),
            "same as normal": dict(alternate_quantity=6),
            "negative": dict(alternate_quantity=-1),
            "bool": dict(alternate_quantity=True),
            "blank explanation": dict(explanation="  "),
            "long explanation": dict(explanation="x" * 4001),
            "not a quantity item": dict(item_id=str(self.suction.pk)),
            "item not on checklist": dict(item_id=str(self.template.pk)),
        }
        for name, fields in bad.items():
            with self.subTest(name), self.assertRaises(ChecklistValidationError):
                self.record(**fields)
        self.assertFalse(StockingDeviation.objects.exists())

    def test_only_approvers_may_record_deviations(self):
        with self.assertRaises(CommandDenied):
            self.record(actor=self.crew)
        self.assertFalse(StockingDeviation.objects.exists())
        self.record(actor=self.supervisor)
        self.assertEqual(StockingDeviation.objects.get().approver_id, self.supervisor.pk)

    def test_replay_does_not_add_a_revision(self):
        context = self.context()
        args = dict(
            vehicle_id=str(self.vehicles[0].pk),
            item_id=str(self.masks.pk),
            template_id=str(self.template.pk),
            alternate_quantity=3,
            valid_from=self.start,
            valid_until=self.end,
            explanation="Synthetic supply shortage.",
        )
        commands.record_stocking_deviation(**args, **context)
        self.assertTrue(commands.record_stocking_deviation(**args, **context).replayed)
        self.assertEqual(StockingDeviation.objects.count(), 1)
