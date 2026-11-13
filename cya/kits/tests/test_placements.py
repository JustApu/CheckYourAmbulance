"""Kit placements and vehicle kit lists (SPEC 2.0.0 4.6 KDEF-4, KDEF-9; issue #129 part 4a)."""

from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction
from django.utils import timezone

from cya.checklists import commands
from cya.checklists.models import Compartment
from cya.checklists.overlay import resolve_checklist
from cya.checklists.tests.support import ChecklistCase
from cya.checklists.validation import ChecklistValidationError
from cya.core.audit_commands import CommandDenied
from cya.kits.commands import add_kit_instance, record_kit_assignment
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
    set_kit_definition_active,
)
from cya.kits.kit_lists import (
    KitListError,
    kit_list_effective_at,
    publish_vehicle_kit_list,
    save_vehicle_kit_list_draft,
)
from cya.kits.models import (
    ChecklistKitPlacement,
    VehicleKitListPublication,
    VehicleKitListVersion,
    VehicleKitRequirement,
)


class PlacementCase(ChecklistCase):
    def definition(self, key="jump-kit", publish=True):
        definition_id = create_kit_definition(
            key=key, label=f"Synthetic {key}", **self.context()
        ).body["definition_id"]
        version_id = save_kit_definition_draft(
            definition_id=definition_id,
            contents=[
                {
                    "item_id": str(self.masks.pk),
                    "display_order": 1,
                    "kind": "QUANTITY",
                    "expected_quantity": 6,
                    "allowed_outcomes": ["PASSED", "FAILED"],
                }
            ],
            **self.context(),
        ).body["version_id"]
        if publish:
            publish_kit_definition_version(version_id=version_id, **self.context())
        return definition_id

    def placement(self, definition_id, order=5):
        return {"definition_id": definition_id, "display_order": order}

    def draft_with(self, placements):
        return commands.save_template_draft(
            template_id=str(self.template.pk),
            items=self.base_items(),
            placements=placements,
            **self.context(),
        ).body["version_id"]

    def own_cabinet(self, vehicle=None):
        return Compartment.objects.get(
            vehicle=vehicle or self.vehicles[0], stable_slot_id=self.cabinet.stable_slot_id
        )

    def line(self, definition_id, count=1, compartment=None, order=1):
        return {
            "definition_id": definition_id,
            "compartment_id": str((compartment or self.own_cabinet()).pk),
            "required_count": count,
            "display_order": order,
        }

    def kit_list(self, lines, vehicle=None, publish=True, actor=None):
        version_id = save_vehicle_kit_list_draft(
            vehicle_id=str((vehicle or self.vehicles[0]).pk), lines=lines, **self.context(actor)
        ).body["version_id"]
        if publish:
            publish_vehicle_kit_list(version_id=version_id, **self.context(actor))
        return version_id

    def add_kit(self, label, definition_id=None, vehicle=None, compartment=None):
        vehicle = vehicle or self.vehicles[0]
        compartment = compartment or self.own_cabinet(vehicle)
        return add_kit_instance(
            label=label,
            vehicle_id=str(vehicle.pk),
            compartment_id=str(compartment.pk),
            definition_id=definition_id,
            **self.context(),
        ).body["kit_id"]


class DraftAndPublishTests(PlacementCase):
    def test_a_draft_stores_placements_and_publication_freezes_them(self):
        jump, other = self.definition(), self.definition("other-kit")
        version_id = self.draft_with([self.placement(jump)])
        row = ChecklistKitPlacement.objects.get(checklist_version_id=version_id)
        self.assertEqual((str(row.definition_id), row.display_order), (jump, 5))
        self.publish(version_id)
        for operation in (
            lambda: ChecklistKitPlacement.objects.filter(pk=row.pk).update(display_order=9),
            lambda: ChecklistKitPlacement.objects.filter(pk=row.pk).delete(),
            lambda: ChecklistKitPlacement.objects.create(
                organization=self.org,
                checklist_version_id=version_id,
                definition_id=other,
                display_order=1,
            ),
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaises(DatabaseError),
                transaction.atomic(),
            ):
                operation()

    def test_a_draft_without_placements_keeps_its_request_digest(self):
        context = self.context()
        first = commands.save_template_draft(
            template_id=str(self.template.pk), items=self.base_items(), **context
        )
        again = commands.save_template_draft(
            template_id=str(self.template.pk), items=self.base_items(), placements=None, **context
        )
        self.assertTrue(again.replayed, "omitting placements is the same request as before")
        self.assertEqual(again.body, first.body)
        self.assertFalse(ChecklistKitPlacement.objects.exists())

    def test_malformed_placements_are_refused(self):
        jump = self.definition()
        inactive = self.definition("retired-kit")
        set_kit_definition_active(definition_id=inactive, active=False, **self.context())
        cases = {
            "not a list": {"definition_id": jump},
            "unknown field": [{**self.placement(jump), "colour": "red"}],
            "a compartment is not a placement field": [
                {**self.placement(jump), "compartment_id": str(self.cabinet.pk)}
            ],
            "missing field": [{"definition_id": jump}],
            "bad order": [{**self.placement(jump), "display_order": -1}],
            "bad definition id": [self.placement("not-a-uuid")],
            "unknown definition": [self.placement(str(uuid4()))],
            "inactive definition": [self.placement(inactive)],
            "twice": [self.placement(jump, 1), self.placement(jump, 2)],
        }
        for name, placements in cases.items():
            with self.subTest(name), self.assertRaises(ChecklistValidationError):
                self.draft_with(placements)
        self.assertFalse(ChecklistKitPlacement.objects.exists())

    def test_publishing_needs_a_published_active_definition(self):
        draft_only = self.definition("draft-only", publish=False)
        with self.assertRaises(ChecklistValidationError):
            self.publish(self.draft_with([self.placement(draft_only)]))
        jump = self.definition()
        version_id = self.draft_with([self.placement(jump)])
        set_kit_definition_active(definition_id=jump, active=False, **self.context())
        with self.assertRaises(ChecklistValidationError):
            self.publish(version_id)

    def test_publishing_does_not_need_any_vehicle_to_carry_the_kit(self):
        jump = self.definition()
        self.publish(self.draft_with([self.placement(jump)]))  # no kit list, no compartment
        self.assertEqual(self.resolve().placements[0].lines, ())


class KitListTests(PlacementCase):
    def test_a_kit_list_is_drafted_published_and_frozen(self):
        jump, other = self.definition(), self.definition("other-kit")
        version_id = self.kit_list([self.line(jump, 2), self.line(other, 1, order=2)])
        lines = VehicleKitRequirement.objects.filter(version_id=version_id).order_by(
            "display_order"
        )
        self.assertEqual(
            [(str(x.definition_id), x.required_count) for x in lines], [(jump, 2), (other, 1)]
        )
        line = lines[0]
        version = VehicleKitListVersion.objects.get(pk=version_id)
        for operation in (
            lambda: VehicleKitRequirement.objects.filter(pk=line.pk).update(required_count=3),
            lambda: VehicleKitRequirement.objects.filter(pk=line.pk).delete(),
            lambda: VehicleKitRequirement.objects.create(
                organization=self.org,
                version_id=version_id,
                definition_id=jump,
                compartment=self.make_slot(Compartment, "drawer"),
                required_count=1,
                display_order=3,
            ),
            lambda: VehicleKitListVersion.objects.filter(pk=version_id).update(revision=9),
            lambda: VehicleKitListVersion.objects.filter(pk=version_id).delete(),
            lambda: VehicleKitListPublication.objects.filter(version=version).update(
                effective_at=timezone.now() + timedelta(days=1)
            ),
            lambda: VehicleKitListPublication.objects.filter(version=version).delete(),
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaises(DatabaseError),
                transaction.atomic(),
            ):
                operation()

    def test_each_save_is_the_next_revision_and_an_empty_list_is_allowed(self):
        jump = self.definition()
        first = self.kit_list([self.line(jump)], publish=False)
        second = self.kit_list([], publish=False)
        revisions = dict(VehicleKitListVersion.objects.values_list("pk", "revision"))
        self.assertEqual((revisions[first], revisions[second]), (1, 2))
        publish_vehicle_kit_list(version_id=second, **self.context())
        self.assertFalse(VehicleKitRequirement.objects.filter(version_id=second).exists())

    def test_malformed_lines_are_refused(self):
        jump = self.definition()
        inactive = self.definition("retired-kit")
        set_kit_definition_active(definition_id=inactive, active=False, **self.context())
        elsewhere = self.own_cabinet(self.vehicles[1])
        cases = {
            "not a list": self.line(jump),
            "unknown field": [{**self.line(jump), "colour": "red"}],
            "missing field": [{"definition_id": jump, "required_count": 1, "display_order": 1}],
            "zero count": [self.line(jump, 0)],
            "too many": [self.line(jump, 100)],
            "not a number": [self.line(jump, "2")],
            "a boolean is not a count": [self.line(jump, True)],
            "bad order": [self.line(jump, order=-1)],
            "bad definition id": [self.line("not-a-uuid")],
            "unknown definition": [self.line(str(uuid4()))],
            "inactive definition": [self.line(inactive)],
            "another vehicle's compartment": [self.line(jump, compartment=elsewhere)],
            "unknown compartment": [{**self.line(jump), "compartment_id": str(uuid4())}],
            "twice in one slot": [self.line(jump, 1), self.line(jump, 2, order=2)],
        }
        for name, lines in cases.items():
            with self.subTest(name), self.assertRaises(KitListError):
                self.kit_list(lines, publish=False)
        self.assertFalse(VehicleKitListVersion.objects.exists())

    def test_the_database_refuses_a_line_in_another_vehicles_compartment(self):
        jump = self.definition()
        version_id = self.kit_list([], publish=False)
        with self.assertRaises(DatabaseError), transaction.atomic():
            VehicleKitRequirement.objects.create(
                organization=self.org,
                version_id=version_id,
                definition_id=jump,
                compartment=self.own_cabinet(self.vehicles[1]),
                required_count=1,
                display_order=1,
            )

    def test_publication_rules(self):
        jump = self.definition()
        later = timezone.now() + timedelta(days=2)
        first = self.kit_list([self.line(jump)], publish=False)
        publish_vehicle_kit_list(version_id=first, effective_at=later, **self.context())
        with self.assertRaises(KitListError):
            publish_vehicle_kit_list(version_id=first, **self.context())  # twice
        second = self.kit_list([self.line(jump, 2)], publish=False)
        with self.assertRaises(KitListError):  # not after the latest effective instant
            publish_vehicle_kit_list(version_id=second, effective_at=later, **self.context())
        with self.assertRaises(KitListError):  # retroactive
            publish_vehicle_kit_list(
                version_id=second,
                effective_at=timezone.now() - timedelta(hours=1),
                **self.context(),
            )
        with self.assertRaises(KitListError):
            publish_vehicle_kit_list(
                version_id=second, effective_at=later.replace(tzinfo=None), **self.context()
            )
        set_kit_definition_active(definition_id=jump, active=False, **self.context())
        with self.assertRaises(KitListError):
            publish_vehicle_kit_list(
                version_id=second, effective_at=later + timedelta(days=1), **self.context()
            )

    def test_the_list_in_force_follows_effective_and_recorded_instants(self):
        jump = self.definition()
        before = timezone.now()
        first = self.kit_list([self.line(jump)])
        middle = timezone.now()
        second = self.kit_list([self.line(jump, 2)])
        now = timezone.now()
        vehicle = str(self.vehicles[0].pk)
        org = str(self.org.pk)
        self.assertIsNone(kit_list_effective_at(org, vehicle, before))
        self.assertEqual(str(kit_list_effective_at(org, vehicle, middle).pk), first)
        self.assertEqual(str(kit_list_effective_at(org, vehicle, now).pk), second)
        self.assertEqual(
            str(kit_list_effective_at(org, vehicle, now, recorded_before=middle).pk), first
        )
        self.assertIsNone(kit_list_effective_at(org, str(self.vehicles[1].pk), now))

    def test_only_supervisors_and_administrators_manage_kit_lists(self):
        jump = self.definition()
        self.kit_list([self.line(jump)], actor=self.supervisor)
        with self.assertRaises(CommandDenied):
            self.kit_list([self.line(jump)], actor=self.crew)

    def test_a_replayed_draft_returns_the_original_result(self):
        jump = self.definition()
        context = self.context()
        request = {"vehicle_id": str(self.vehicles[0].pk), "lines": [self.line(jump)]}
        first = save_vehicle_kit_list_draft(**request, **context)
        again = save_vehicle_kit_list_draft(**request, **context)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(VehicleKitListVersion.objects.count(), 1)


class ResolutionTests(PlacementCase):
    def test_each_vehicle_resolves_its_own_kit_list_and_kits_of_that_definition(self):
        jump, other = self.definition(), self.definition("other-kit")
        self.publish(self.draft_with([self.placement(jump)]))
        drawer = self.make_slot(Compartment, "drawer")
        self.kit_list([self.line(jump, 2), self.line(jump, 1, compartment=drawer, order=2)])
        self.kit_list(
            [self.line(other, 1, compartment=self.own_cabinet(self.vehicles[1]))],
            vehicle=self.vehicles[1],
        )
        first = self.add_kit("Jump kit 1", jump)
        second = self.add_kit("Jump kit 2", jump, compartment=drawer)
        self.add_kit("Other kit", other)
        self.add_kit("Legacy kit")  # no definition
        (placement,) = self.resolve().placements
        self.assertEqual((placement.definition_id, placement.definition_key), (jump, "jump-kit"))
        self.assertEqual(
            [(x.compartment_id, x.required_count) for x in placement.lines],
            [(str(self.own_cabinet().pk), 2), (str(drawer.pk), 1)],
        )
        self.assertEqual(
            placement.kits,
            (
                (first, "Jump kit 1", str(self.own_cabinet().pk)),
                (second, "Jump kit 2", str(drawer.pk)),
            ),
            "the definition's kits anywhere on the vehicle, with where each is",
        )
        other_vehicle = self.resolve(vehicle=self.vehicles[1]).placements[0]
        self.assertEqual((other_vehicle.lines, other_vehicle.kits), ((), ()))
        self.assertEqual(len(self.resolve().items), 3, "placements do not change the items")

    def test_a_vehicle_without_a_kit_list_has_no_lines_but_its_kits_still_resolve(self):
        jump = self.definition()
        self.publish(self.draft_with([self.placement(jump)]))
        kit = self.add_kit("Jump kit 1", jump)
        (placement,) = self.resolve().placements
        self.assertEqual(placement.lines, ())
        self.assertEqual([k for k, *_ in placement.kits], [kit])

    def test_a_version_without_placements_resolves_none(self):
        self.published_version()
        self.assertEqual(self.resolve().placements, ())


class AsOfResolutionTests(PlacementCase):
    """Review of PR #145: a past resolution reads the history, not current rows."""

    def test_a_later_move_or_deactivation_does_not_change_a_past_resolution(self):
        jump = self.definition()
        self.publish(self.draft_with([self.placement(jump)]))
        moved = self.add_kit("Jump kit 1", jump)
        removed = self.add_kit("Jump kit 2", jump)
        before = timezone.now()
        drawer = self.make_slot(Compartment, "drawer")
        record_kit_assignment(
            kit_id=moved,
            active=True,
            vehicle_id=str(self.vehicles[0].pk),
            compartment_id=str(drawer.pk),
            **self.context(),
        )
        record_kit_assignment(kit_id=removed, active=False, **self.context())
        cabinet = str(self.own_cabinet().pk)

        def kits(**when):
            resolved = resolve_checklist(
                organization_id=str(self.org.pk),
                vehicle_id=str(self.vehicles[0].pk),
                template_id=str(self.template.pk),
                **when,
            )
            return list(resolved.placements[0].kits)

        self.assertEqual(
            kits(as_of=before),
            [(moved, "Jump kit 1", cabinet), (removed, "Jump kit 2", cabinet)],
        )
        self.assertEqual(kits(as_of=timezone.now()), [(moved, "Jump kit 1", str(drawer.pk))])
        self.assertEqual(
            kits(as_of=timezone.now(), recorded_before=before),
            [(moved, "Jump kit 1", cabinet), (removed, "Jump kit 2", cabinet)],
            "events recorded after the cutoff do not count",
        )

    def test_a_later_kit_list_does_not_change_a_past_resolution(self):
        jump = self.definition()
        self.publish(self.draft_with([self.placement(jump)]))
        self.kit_list([self.line(jump, 1)])
        before = timezone.now()
        self.kit_list([self.line(jump, 3)])

        def counts(**when):
            resolved = resolve_checklist(
                organization_id=str(self.org.pk),
                vehicle_id=str(self.vehicles[0].pk),
                template_id=str(self.template.pk),
                **when,
            )
            return [x.required_count for x in resolved.placements[0].lines]

        self.assertEqual(counts(as_of=before), [1])
        self.assertEqual(counts(as_of=timezone.now()), [3])
        self.assertEqual(counts(as_of=timezone.now(), recorded_before=before), [1])


class ConcurrentDeactivationTests(PlacementCase):
    """Review of PR #145: publishing holds the definitions it checked until it commits."""

    def race(self, module, function_name, publish):
        """Pause `publish` after `module.function_name`; a second actor deactivates meanwhile."""
        import threading
        from unittest import mock

        from django.db import close_old_connections

        checked, release = threading.Event(), threading.Event()
        order, errors = [], []
        original = getattr(module, function_name)

        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            checked.set()
            release.wait(timeout=30)  # deactivation runs meanwhile, if it is not blocked
            return result

        def run_publish():
            try:
                with mock.patch.object(module, function_name, paused):
                    publish()
                order.append("published")
            except Exception as error:  # noqa: BLE001 - asserted below
                errors.append(error)
            finally:
                close_old_connections()

        def deactivate():
            try:
                checked.wait(timeout=30)
                # Another actor, so the two commands share no identity lock.
                set_kit_definition_active(
                    definition_id=self.jump, active=False, **self.context(self.supervisor)
                )
                order.append("deactivated")
            except Exception as error:  # noqa: BLE001 - asserted below
                errors.append(error)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=run_publish), threading.Thread(target=deactivate)]
        for thread in threads:
            thread.start()
        self.assertTrue(checked.wait(timeout=30))
        threads[1].join(timeout=3)  # a deactivation that is not blocked finishes here
        release.set()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(order, ["published", "deactivated"])

    def test_a_deactivation_waits_for_a_checklist_publication_that_checked_the_definition(self):
        self.jump = self.definition()
        version_id = self.draft_with([self.placement(self.jump)])
        self.race(commands, "check_publishable", lambda: self.publish(version_id))

    def test_a_deactivation_waits_for_a_kit_list_publication_that_checked_the_definition(self):
        from cya.kits import kit_lists

        self.jump = self.definition()
        version_id = self.kit_list([self.line(self.jump)], publish=False)
        self.race(
            kit_lists,
            "check_listed_definitions",
            lambda: publish_vehicle_kit_list(version_id=version_id, **self.context()),
        )
