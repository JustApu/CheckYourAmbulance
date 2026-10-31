"""Kit identity per vehicle (SPEC 4.2) and publish ordering under concurrency (CHK-3)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, BrokenBarrierError
from unittest import mock

from django.db import connection
from django.utils import timezone

from cya.checklists import commands
from cya.checklists.models import ChecklistPublication, Compartment, KitInstance
from cya.checklists.tests.support import ChecklistCase
from cya.checklists.validation import ChecklistValidationError


class KitBindingTests(ChecklistCase):
    def setUp(self):
        super().setUp()
        first, second = self.vehicles
        self.second_cabinet = Compartment.objects.get(
            vehicle=second, stable_slot_id=self.cabinet.stable_slot_id
        )
        self.kit_a = self.make_kit("Synthetic kit A", first, self.cabinet)

    def make_kit(self, label, vehicle, compartment, **fields):
        return KitInstance.objects.create(
            organization=self.org, label=label, vehicle=vehicle, compartment=compartment, **fields
        )

    def kit_items(self):
        return [self.spec(self.suction, kit_id=str(self.kit_a.pk))]

    def test_each_vehicle_resolves_to_its_own_kit_never_the_templates(self):
        kit_b = self.make_kit("Synthetic kit B", self.vehicles[1], self.second_cabinet)
        self.published_version(self.kit_items())
        for vehicle, kit in zip(self.vehicles, (self.kit_a, kit_b), strict=True):
            self.assertEqual(self.item_for(vehicle).kits, ((str(kit.pk), kit.label),))

    def test_vehicles_may_carry_none_one_or_several_of_a_kit(self):
        # Vehicle B holds no kit; A holds four (any count is valid), ordered by label.
        extra = [
            self.make_kit(f"Synthetic kit A{n}", self.vehicles[0], self.cabinet) for n in (3, 1, 2)
        ]
        version_id = self.published_version(self.kit_items())
        self.assertEqual(self.item_for(self.vehicles[1]).kits, ())
        expected = tuple(
            (str(kit.pk), kit.label) for kit in sorted([self.kit_a, *extra], key=lambda k: k.label)
        )
        self.assertEqual(len(expected), 4)
        self.assertEqual(self.item_for(self.vehicles[0]).kits, expected)
        # Deactivating every kit later does not break resolution; it just lists none.
        KitInstance.objects.filter(organization=self.org).update(active=False)
        resolved = self.resolve(self.vehicles[0])
        self.assertEqual(resolved.template_version_id, version_id)
        self.assertEqual(self.item_for(self.vehicles[0]).kits, ())

    def test_kit_must_be_active_and_in_the_definitions_compartment_slot(self):
        other = self.make_slot(Compartment, "drawer")
        wrong_slot = self.make_kit("Synthetic drawer kit", self.vehicles[0], other)
        unplaced = KitInstance.objects.create(organization=self.org, label="Synthetic unplaced")
        inactive = self.make_kit("Synthetic old kit", self.vehicles[0], self.cabinet, active=False)
        for kit in (wrong_slot, unplaced, inactive):
            with self.subTest(kit=kit.label), self.assertRaises(ChecklistValidationError):
                self.draft([self.spec(self.suction, kit_id=str(kit.pk))])

    def item_for(self, vehicle):
        return next(iter(self.resolve(vehicle).items))


class PublishRaceTests(ChecklistCase):
    """Two different authorized publishers meet right after the ordering check; one may win.

    Distinct actors matter: one actor's user-row lock would serialize the commands by itself.
    """

    def race(self, publish_a, publish_b):
        meeting = Barrier(2)
        original = commands._require_after_latest

        def meet_after_check(*args):
            original(*args)
            try:
                meeting.wait(timeout=3)
            except BrokenBarrierError:
                pass  # the other publisher is held back by the parent lock

        def run(publish):
            try:
                return publish()
            except ChecklistValidationError as error:
                return error
            finally:
                connection.close()

        with (
            mock.patch.object(commands, "_require_after_latest", meet_after_check),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(run, publish_a), pool.submit(run, publish_b)]
            return [future.result(timeout=30) for future in futures]

    def test_concurrent_template_publications_keep_strict_ordering(self):
        first, second = self.draft(), self.draft()
        at = timezone.now() + timedelta(days=1)
        results = self.race(
            lambda: self.publish(first, effective_at=at, actor=self.admin),
            lambda: self.publish(second, effective_at=at, actor=self.supervisor),
        )
        self.assertEqual(sum(isinstance(r, ChecklistValidationError) for r in results), 1)
        self.assertEqual(ChecklistPublication.objects.count(), 1)

    def test_concurrent_override_publications_keep_strict_ordering(self):
        template_version = self.published_version()
        drafts = [
            commands.save_override_draft(
                vehicle_id=str(self.vehicles[0].pk),
                template_version_id=template_version,
                entries=[{"item_id": str(self.suction.pk), "action": "REMOVE"}],
                **self.context(),
            ).body["override_version_id"]
            for _ in range(2)
        ]
        at = timezone.now() + timedelta(days=1)
        results = self.race(
            *[
                lambda d=d, actor=actor: commands.publish_override_version(
                    override_version_id=d, effective_at=at, **self.context(actor)
                )
                for d, actor in zip(drafts, (self.admin, self.supervisor), strict=True)
            ]
        )
        self.assertEqual(sum(isinstance(r, ChecklistValidationError) for r in results), 1)
        self.assertEqual(ChecklistPublication.objects.count(), 2)  # template + one override
