"""Requirement resolution (bindings, schedule revisions, pins) and the as-of read helpers."""

from datetime import time, timedelta

from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistTemplate
from cya.obligations.queries import (
    effective_obligations,
    effective_selection,
    odometer_obligations,
)
from cya.obligations.requirements import (
    desired_at,
    inspection_key,
    is_exempt,
    occurrences,
    requirement_id,
)
from cya.obligations.state import NO_KIT
from cya.obligations.tests.support import ObligationCase
from cya.operations import commands as operation_commands


class OccurrenceTests(ObligationCase):
    def setUp(self):
        super().setUp()
        self.published_version()
        self.publish_schedule("test-0600")
        self.org_id = str(self.org.pk)
        self.vehicle_id = str(self.vehicle.pk)

    def dues(self, start_days, end_days):
        found = occurrences(
            self.org_id, self.vehicle_id, self.at(days=start_days), self.at(days=end_days, hours=12)
        )
        return [o.due_at for o in found]

    def test_nothing_is_scheduled_until_a_binding_is_in_force(self):
        self.assertEqual(self.dues(0, 5), [])
        self.bind(effective_at=self.at(days=2), reconcile=False)
        self.assertEqual(
            self.dues(0, 4),
            [self.at(days=2, hours=6), self.at(days=3, hours=6), self.at(days=4, hours=6)],
        )

    def test_a_binding_must_be_in_force_at_the_due_instant_itself(self):
        self.bind(reconcile=False)  # effective now (06-01 00:00)
        self.clock.set(self.at(days=1))
        self.bind(action="UNBIND", effective_at=self.at(days=2, hours=3), reconcile=False)
        # Unbound at 06-03 03:00, before that day's 06:00 due instant, so 06-03 is excluded.
        self.assertEqual(self.dues(0, 4), [self.at(hours=6), self.at(days=1, hours=6)])

    def test_each_schedule_revision_governs_only_until_the_next_takes_effect(self):
        self.bind(reconcile=False)
        self.publish_schedule("test-0600", wall_time=time(6, 30), effective_at=self.at(days=1))
        self.assertEqual(
            self.dues(0, 2),
            [
                self.at(hours=6),
                self.at(days=1, hours=6, minutes=30),
                self.at(days=2, hours=6, minutes=30),
            ],
        )

    def test_windows_come_from_the_revision_in_force(self):
        self.bind(reconcile=False)
        self.publish_schedule(
            "test-0600", effective_at=self.at(days=1), early_seconds=1800, grace_seconds=7200
        )
        first, second = occurrences(
            self.org_id, self.vehicle_id, self.at(), self.at(days=1, hours=12)
        )
        self.assertEqual(first.due_at - first.early_at, timedelta(hours=1))
        self.assertEqual(second.due_at - second.early_at, timedelta(minutes=30))
        self.assertEqual(second.grace_at - second.due_at, timedelta(hours=2))

    def test_occurrences_are_ordered_and_bounded_by_the_requested_range(self):
        self.publish_schedule("test-1800", wall_time=time(18))
        self.bind(reconcile=False)
        self.bind(slot="test-1800", reconcile=False)
        found = occurrences(
            self.org_id, self.vehicle_id, self.at(hours=7), self.at(days=1, hours=6)
        )
        self.assertEqual([o.due_at for o in found], [self.at(hours=18), self.at(days=1, hours=6)])
        self.assertEqual([o.slot for o in found], ["test-1800", "test-0600"])


class DesiredTests(ObligationCase):
    def setUp(self):
        super().setUp()
        self.version_id = self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.org_id, self.vehicle_id = str(self.org.pk), str(self.vehicle.pk)
        self.due = self.at(hours=6)

    def desired(self):
        at_due = occurrences(self.org_id, self.vehicle_id, self.due, self.due)
        return desired_at(self.org_id, self.vehicle_id, self.due, at_due)

    def test_every_item_of_the_checklist_is_wanted_with_the_schedule_window(self):
        desired = self.desired()
        self.assertEqual(
            set(desired.items),
            {(str(i.pk), NO_KIT) for i in (self.suction, self.masks, self.siren)},
        )
        for item in desired.items.values():
            self.assertEqual(item.early_at, self.due - timedelta(hours=1))
            self.assertEqual(item.grace_at, self.due + timedelta(hours=1))
        self.assertEqual({item.slot for item in desired.items.values()}, {"test-0600"})

    def test_nothing_is_wanted_before_any_version_is_effective(self):
        self.clock.set(self.at(hours=-2))
        early = self.at(hours=-1)
        found = occurrences(self.org_id, self.vehicle_id, early, early)
        self.assertEqual(found, [])  # no occurrence at all, hence nothing to resolve

    def test_two_templates_on_one_slot_never_produce_two_requirements_for_an_item(self):
        other = ChecklistTemplate.objects.create(organization=self.org, label="Second")
        draft = checklist_commands.save_template_draft(
            template_id=str(other.pk), items=self.base_items(), **self.context()
        ).body["version_id"]
        checklist_commands.publish_template_version(version_id=draft, **self.context())
        self.bind(template=other, reconcile=False)
        desired = self.desired()
        self.assertEqual(len(desired.items), 3)
        winner = min(str(self.template.pk), str(other.pk))
        publication_templates = {
            str(self.pin_row(d).checklist_version.template_id) for d in desired.items.values()
        }
        self.assertEqual(publication_templates, {winner})

    @staticmethod
    def pin_row(item):
        from cya.checklists.models import ChecklistPublication

        return ChecklistPublication.objects.select_related("checklist_version").get(
            pk=item.publication_id
        )

    def test_requirement_ids_are_a_pure_function_of_the_pinned_rows(self):
        pin = {
            "binding": "b",
            "conditional": None,
            "item_def": "d",
            "override": None,
            "publication": "p",
            "schedule": "s",
        }
        same = requirement_id("o", "v", "i", dict(pin))
        self.assertEqual(same, requirement_id("o", "v", "i", dict(pin)))
        for key, value in (
            ("binding", "b2"),
            ("conditional", "c"),
            ("override", "x"),
            ("schedule", "s2"),
        ):
            self.assertNotEqual(same, requirement_id("o", "v", "i", {**pin, key: value}), key)
        self.assertNotEqual(same, requirement_id("o", "v2", "i", dict(pin)))
        self.assertNotEqual(same, requirement_id("o", "v", "i2", dict(pin)))

    def test_inspection_keys_are_stable_and_distinguish_slot_instant_and_schedule_row(self):
        self.assertEqual(
            inspection_key("a-slot", self.due, "rule-1"),
            "a-slot@2026-06-01T06:00:00.000000Z#rule-1",
        )
        base = inspection_key("a", self.due, "r")
        self.assertNotEqual(base, inspection_key("b", self.due, "r"))
        self.assertNotEqual(base, inspection_key("a", self.due + timedelta(days=1), "r"))
        self.assertNotEqual(base, inspection_key("a", self.due, "r2"))


class ExemptionTests(ObligationCase):
    def setUp(self):
        super().setUp()
        self.org_id, self.vehicle_id = str(self.org.pk), str(self.vehicle.pk)

    def status(self, status, at):
        self.clock.set(at)
        operation_commands.set_vehicle_status(
            vehicle_id=self.vehicle_id,
            status=status,
            explanation="Synthetic.",
            **self.context(self.supervisor),
        )

    def test_a_vehicle_with_no_recorded_status_is_not_exempt(self):
        self.assertFalse(is_exempt(self.org_id, self.vehicle_id, self.at(hours=6), self.at()))

    def test_a_future_due_instant_follows_the_status_now(self):
        self.status("OUT_OF_SERVICE", self.at(hours=1))
        self.assertTrue(is_exempt(self.org_id, self.vehicle_id, self.at(days=1), self.at(hours=2)))
        self.status("IN_SERVICE", self.at(hours=3))
        self.assertFalse(is_exempt(self.org_id, self.vehicle_id, self.at(days=1), self.at(hours=4)))

    def test_a_reached_due_instant_follows_the_status_at_that_instant(self):
        self.status("OUT_OF_SERVICE", self.at(hours=1))
        self.status("IN_SERVICE", self.at(hours=8))
        due = self.at(hours=6)
        later = self.at(hours=9)  # back in service now, but the vehicle was out at D
        self.assertTrue(is_exempt(self.org_id, self.vehicle_id, due, later))
        self.assertFalse(is_exempt(self.org_id, self.vehicle_id, self.at(hours=9), later))

    def test_limited_service_is_not_out_of_service(self):
        self.clock.set(self.at(hours=1))
        operation_commands.set_vehicle_status(
            vehicle_id=self.vehicle_id,
            status="LIMITED_SERVICE",
            explanation="Synthetic.",
            restrictions="Synthetic restriction.",
            **self.context(self.supervisor),
        )
        self.assertFalse(
            is_exempt(self.org_id, self.vehicle_id, self.at(hours=6), self.at(hours=2))
        )


class QueryTests(ObligationCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.generate()
        self.org_id = str(self.org.pk)

    def test_effective_obligations_are_bounded_ordered_and_filterable(self):
        first_day = effective_obligations(self.org_id, self.at(hours=5), self.at(hours=7))
        self.assertEqual(len(first_day), 3)
        self.assertTrue(all(o.due_at == self.at(hours=6) for o in first_day))
        self.assertEqual(
            effective_obligations(
                self.org_id, self.at(), self.at(days=2), vehicle_id=str(self.vehicles[1].pk)
            ),
            [],
        )
        both = effective_obligations(self.org_id, self.at(), self.at(days=2))
        self.assertEqual([o.due_at for o in both], sorted(o.due_at for o in both))

    def test_as_of_before_generation_sees_nothing(self):
        self.assertEqual(self.obligations(as_of=self.at(hours=-1)), [])

    def test_effective_selection_returns_the_tail_recorded_by_the_instant(self):
        due = self.at(hours=6)
        before = self.clock.now
        self.clock.set(self.at(hours=4))
        self.published_version()
        tail = effective_selection(self.org_id, str(self.vehicle.pk), str(self.suction.pk), due)
        past = effective_selection(
            self.org_id, str(self.vehicle.pk), str(self.suction.pk), due, as_of=before
        )
        self.assertEqual((tail.sequence, past.sequence), (2, 1))
        self.assertIsNone(
            effective_selection(
                self.org_id,
                str(self.vehicle.pk),
                str(self.suction.pk),
                due,
                as_of=self.at(hours=-1),
            )
        )

    def test_odometer_obligations_are_ranged_and_as_of(self):
        rows = odometer_obligations(self.org_id, self.at(), self.at(days=2))
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            len(
                odometer_obligations(
                    self.org_id, self.at(), self.at(days=2), as_of=self.at(hours=-1)
                )
            ),
            0,
        )
        self.assertEqual(odometer_obligations(self.org_id, self.at(days=5), self.at(days=6)), [])

    def test_bounds_must_be_aware_and_ordered(self):
        for call in (
            lambda: effective_obligations(
                self.org_id, self.at().replace(tzinfo=None), self.at(days=1)
            ),
            lambda: effective_obligations(self.org_id, self.at(days=1), self.at()),
            lambda: effective_obligations(
                self.org_id, self.at(), self.at(days=1), as_of=self.at().replace(tzinfo=None)
            ),
            lambda: odometer_obligations(
                self.org_id, self.at(), self.at(days=1).replace(tzinfo=None)
            ),
        ):
            with self.assertRaises(ValueError):
                call()
