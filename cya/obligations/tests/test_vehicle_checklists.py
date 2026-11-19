"""Vehicle-owned checklists before and after the cutover (SPEC 5.0.0 CHK-1 to CHK-10, CFG-7).

These cover the model and generator of issue #220 part 1: which bindings govern a due instant
on each side of the organization's cutover, the CHK-10 precedence, Not placed items, and the
refusals that keep legacy templates read-only and vehicle checklists on their own vehicle.
"""

from datetime import timedelta
from unittest import mock

from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistItemDef, ChecklistPublication, ChecklistTemplate
from cya.checklists.validation import ChecklistValidationError
from cya.core.audit_commands import CommandDenied
from cya.kits.positions import templates_bound_now_or_later
from cya.obligations.commands import BindingError
from cya.obligations.requirements import bound_template_ids, desired_at, occurrences
from cya.obligations.state import NO_KIT
from cya.obligations.tests.support import ObligationCase
from cya.operations.duplication import DuplicationError, duplicate_template
from cya.operations.templates import TemplateError, create_template, create_vehicle_checklist


class CutoverCase(ObligationCase):
    """A legacy template bound to vehicle A on test-0600, then the cutover at day 1, 12:00."""

    def setUp(self):
        super().setUp()
        self.legacy_version = self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.org_id, self.vehicle_id = str(self.org.pk), str(self.vehicle.pk)
        self.clock.set(self.at(days=1, hours=12))
        self.cutover = self.record_cutover()

    def dues(self, end_days=3, recorded_before=None):
        found = occurrences(
            self.org_id,
            self.vehicle_id,
            self.at(),
            self.at(days=end_days, hours=12),
            recorded_before=recorded_before,
        )
        return [(o.due_at, o.template_id) for o in found]

    def assign(self, checklist, slot="test-0600", vehicle=None, **kw):
        return self.bind(vehicle=vehicle, slot=slot, template=checklist, reconcile=False, **kw)

    def desired(self, due):
        found = occurrences(self.org_id, self.vehicle_id, due, due)
        return desired_at(self.org_id, self.vehicle_id, due, found)

    @staticmethod
    def checklist_of(desired_item):
        return str(
            ChecklistPublication.objects.select_related("checklist_version")
            .get(pk=desired_item.publication_id)
            .checklist_version.template_id
        )


class GoverningTests(CutoverCase):
    def test_legacy_binding_governs_only_due_instants_before_the_cutover(self):
        legacy = str(self.template.pk)
        self.assertEqual(
            self.dues(), [(self.at(hours=6), legacy), (self.at(days=1, hours=6), legacy)]
        )
        self.assertEqual(
            bound_template_ids(self.org_id, self.vehicle_id, self.at(hours=7)), [legacy]
        )
        self.assertEqual(bound_template_ids(self.org_id, self.vehicle_id, self.at(days=2)), [])

    def test_as_the_records_stood_before_the_cutover_the_legacy_binding_still_governs(self):
        # The planner's freeze instant: an obligation whose window opened before the cutover
        # keeps the requirement it had then (SPEC 5.7, 18.2).
        legacy = str(self.template.pk)
        self.assertEqual(
            self.dues(recorded_before=self.cutover),
            [(self.at(days=d, hours=6), legacy) for d in range(4)],
        )

    def test_a_vehicle_checklist_assignment_governs_from_the_cutover(self):
        own = self.vehicle_checklist()
        self.publish_checklist(own, self.own_items())
        self.assign(own)
        legacy = str(self.template.pk)
        self.assertEqual(
            self.dues(),
            [
                (self.at(hours=6), legacy),
                (self.at(days=1, hours=6), legacy),
                (self.at(days=2, hours=6), str(own.pk)),
                (self.at(days=3, hours=6), str(own.pk)),
            ],
        )
        self.assertEqual(
            bound_template_ids(self.org_id, self.vehicle_id, self.at(days=2)), [str(own.pk)]
        )

    def test_a_not_placed_item_is_still_required(self):
        own = self.vehicle_checklist()
        self.publish_checklist(own, self.own_items())
        self.assign(own)
        desired = self.desired(self.at(days=2, hours=6))
        self.assertEqual(
            set(desired.items),
            {(str(i.pk), NO_KIT) for i in (self.suction, self.masks, self.siren)},
        )
        siren = desired.items[(str(self.siren.pk), NO_KIT)]
        definition = ChecklistItemDef.objects.get(pk=siren.item_def_id)
        self.assertIsNone(definition.compartment_id)
        self.assertIsNone(definition.system_id)

    def test_generation_creates_the_obligation_of_a_not_placed_item(self):
        own = self.vehicle_checklist()
        self.publish_checklist(own, self.own_items())
        self.assign(own)
        self.generate()
        siren = [
            o
            for o in self.obligations(vehicle=self.vehicle)
            if str(o.item_id) == str(self.siren.pk) and o.due_at == self.at(days=2, hours=6)
        ]
        self.assertEqual(len(siren), 1)

    def test_legacy_bindings_no_longer_block_a_retirement_after_the_cutover(self):
        self.assertEqual(templates_bound_now_or_later(self.org_id, self.vehicle_id), [])
        own = self.vehicle_checklist()
        self.publish_checklist(own, self.own_items())
        self.assign(own)
        self.assertEqual(templates_bound_now_or_later(self.org_id, self.vehicle_id), [str(own.pk)])


class PrecedenceTests(CutoverCase):
    """CHK-10: lowest slot text, then the checklist's precedence ordinal, never its identity."""

    def two_checklists(self):
        # The second checklist gets the smaller identity, so identity order and precedence
        # order disagree and the test tells them apart.
        ids = iter(["ffffffff-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"])
        with mock.patch("cya.operations.templates.new_id", side_effect=lambda: next(ids)):
            first = self.vehicle_checklist(label="First")
            second = self.vehicle_checklist(label="Second")
        self.assertEqual((first.precedence, second.precedence), (1, 2))
        self.assertGreater(str(first.pk), str(second.pk))
        for checklist in (first, second):
            self.publish_checklist(checklist, self.own_items())
            self.assign(checklist)
        return first, second

    def test_the_lower_precedence_wins_a_shared_anchor(self):
        first, _second = self.two_checklists()
        desired = self.desired(self.at(days=2, hours=6))
        self.assertEqual(len(desired.items), 3)
        self.assertEqual({self.checklist_of(d) for d in desired.items.values()}, {str(first.pk)})

    def test_the_lower_slot_text_wins_before_precedence(self):
        first, second = self.two_checklists()
        # Same due instant on a slot whose text sorts first: "test-0559" < "test-0600".
        self.publish_schedule("test-0559")
        self.assign(second, slot="test-0559")
        desired = self.desired(self.at(days=2, hours=6))
        self.assertEqual({self.checklist_of(d) for d in desired.items.values()}, {str(second.pk)})
        self.assertEqual({d.slot for d in desired.items.values()}, {"test-0559"})
        self.assertNotEqual(str(first.pk), str(second.pk))

    def test_precedence_is_per_vehicle(self):
        self.vehicle_checklist()
        other = self.vehicle_checklist(vehicle=self.vehicles[1])
        self.assertEqual(other.precedence, 1)


class RefusalTests(CutoverCase):
    def test_an_assignment_names_a_checklist_of_the_same_vehicle(self):
        own = self.vehicle_checklist()
        self.publish_checklist(own, self.own_items())
        with self.assertRaisesRegex(BindingError, "its own vehicle"):
            self.assign(own, vehicle=self.vehicles[1])

    def test_items_are_placed_only_on_the_checklist_vehicle(self):
        own = self.vehicle_checklist()
        foreign = self.own_items(self.vehicles[1])
        with self.assertRaisesRegex(ChecklistValidationError, "not on this checklist's vehicle"):
            self.publish_checklist(own, foreign)

    def test_legacy_templates_are_read_only_after_the_cutover(self):
        context = self.context()
        with self.assertRaisesRegex(ChecklistValidationError, "read-only"):
            checklist_commands.save_template_draft(
                template_id=str(self.template.pk), items=self.base_items(), **context
            )
        with self.assertRaisesRegex(BindingError, "read-only"):
            self.bind(action="UNBIND", reconcile=False)
        with self.assertRaisesRegex(TemplateError, "read-only"):
            create_template(label="New shared", key="new-shared", **self.context())
        with self.assertRaisesRegex(ChecklistValidationError, "read-only"):
            checklist_commands.save_override_draft(
                vehicle_id=self.vehicle_id,
                template_version_id=self.legacy_version,
                entries=[{"item_id": str(self.suction.pk), "action": "REMOVE"}],
                **self.context(),
            )
        with self.assertRaisesRegex(DuplicationError, "read-only"):
            duplicate_template(
                source_version_id=self.legacy_version,
                label="Copy",
                key="copy",
                **self.context(),
            )

    def test_a_legacy_draft_from_before_the_cutover_cannot_be_published_after_it(self):
        self.clock.set(self.at(hours=1))
        # Recorded "before" in clock terms is irrelevant: the cutover row exists now.
        with self.assertRaisesRegex(ChecklistValidationError, "read-only"):
            checklist_commands.save_template_draft(
                template_id=str(self.template.pk), items=self.base_items(), **self.context()
            )

    def test_only_supervisors_and_administrators_create_vehicle_checklists(self):
        with self.assertRaises(CommandDenied):
            self.vehicle_checklist(actor=self.crew)
        self.assertEqual(self.vehicle_checklist(actor=self.supervisor).precedence, 1)

    def test_a_vehicle_checklist_has_no_overrides(self):
        own = self.vehicle_checklist()
        version = self.publish_checklist(own, self.own_items())
        with self.assertRaises(ChecklistValidationError):
            checklist_commands.save_override_draft(
                vehicle_id=self.vehicle_id,
                template_version_id=version,
                entries=[{"item_id": str(self.suction.pk), "action": "REMOVE"}],
                **self.context(),
            )


class BeforeCutoverTests(ObligationCase):
    def test_vehicle_checklists_start_at_the_cutover(self):
        with self.assertRaisesRegex(TemplateError, "start at the cutover"):
            create_vehicle_checklist(
                vehicle_id=str(self.vehicle.pk), label="Too early", **self.context()
            )
        self.assertFalse(ChecklistTemplate.objects.filter(vehicle__isnull=False).exists())

    def test_a_legacy_item_still_needs_a_placement(self):
        with self.assertRaisesRegex(ChecklistValidationError, "exactly one of"):
            self.draft([self.spec(self.siren, 30, compartment_id=None)])

    def test_without_a_cutover_bindings_behave_as_before(self):
        self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        found = occurrences(
            str(self.org.pk), str(self.vehicle.pk), self.at(), self.at(days=2) + timedelta(hours=12)
        )
        self.assertEqual(len(found), 3)


class NotPlacedDisplayTests(CutoverCase):
    """SPEC 5.0.0 CHK-9, DEP-11, CAB-2: a Not placed item is shown, grouped and printed as such."""

    def setUp(self):
        super().setUp()
        own = self.vehicle_checklist()
        self.publish_checklist(own, self.own_items())
        self.assign(own)
        self.clock.set(self.at(days=2, hours=5, minutes=30))
        self.generate()

    def checklist(self):
        from cya.attention.checklist import vehicle_checklist

        return vehicle_checklist(self.org_id, self.vehicle_id)

    def test_the_item_is_listed_as_due_under_not_placed(self):
        result = self.checklist()
        siren = next(r for r in result.rows if r.item_id == str(self.siren.pk))
        self.assertTrue(siren.not_placed)
        self.assertIsNotNone(siren.obligation_state)  # still owed, not exempt
        others = [r for r in result.rows if r.item_id != str(self.siren.pk)]
        self.assertFalse(any(r.not_placed for r in others))
        groups = {g["label"]: g for g in result.group_progress}
        self.assertEqual(groups["Not placed"]["total"], 1)

    def test_the_paper_sheet_prints_a_not_placed_section_no_seal_covers(self):
        from cya.inspections import printable

        cabinet = str(self.own_row(self.cabinet, self.vehicle).pk)
        sections, _kits = printable.outage_sheet(self.checklist().rows, frozenset({cabinet}))
        by_heading = {s.heading: s for s in sections}
        self.assertEqual([r.label for r in by_heading["Not placed"].rows], [self.siren.label])
        self.assertFalse(by_heading["Not placed"].sealed)
