"""Synthetic sample agency seed: idempotent, non-authoritative, resolves for all four ambulances."""

import io
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from django.core.management import CommandError, call_command
from django.utils import timezone

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    Compartment,
    Item,
    Vehicle,
)
from cya.checklists.overlay import resolve_checklist
from cya.checklists.seed import FIXTURE, plan_seed
from cya.checklists.tests.support import ChecklistCase
from cya.testing.fixtures import FixtureError, load_fixture


class SeedTests(ChecklistCase):
    def setUp(self):
        super().setUp()
        # The generic test vehicles have none of the seed's slots, and publishing validates
        # the resolved set for every active vehicle.
        Vehicle.objects.update(active=False)

    def run_seed(self, *args):
        out = io.StringIO()
        call_command("seed_synthetic_fleet", f"--actor-user-id={self.admin.pk}", *args, stdout=out)
        return out.getvalue()

    def test_fixture_is_marked_synthetic_and_non_authoritative(self):
        data = load_fixture(FIXTURE)
        self.assertIs(data["non_authoritative"], True)
        self.assertEqual(
            [v["display_name"] for v in data["vehicles"]],
            ["Ambulance 1", "Ambulance 2", "Ambulance 3", "Ambulance 4"],
        )
        self.assertIn("NON-AUTHORITATIVE", data["template"]["label"])
        for item in data["items"]:
            self.assertTrue(item["label"].startswith("Synthetic"))

    def test_dry_run_writes_nothing(self):
        before = Vehicle.objects.count()
        self.assertIn(
            "Dry run: would ensure 4 vehicles, 16 slot rows, 6 items", self.run_seed("--dry-run")
        )
        self.assertEqual(Vehicle.objects.count(), before)
        self.assertFalse(Item.objects.filter(label__startswith="Synthetic: ").exists())
        self.assertFalse(ChecklistPublication.objects.exists())

    def test_seed_publishes_a_checklist_that_resolves_for_every_ambulance(self):
        self.run_seed()
        seeded = Vehicle.objects.filter(active=True)
        self.assertEqual(seeded.count(), 4)
        template_id = plan_seed(str(self.org.pk)).template_id
        for vehicle in seeded:
            resolved = resolve_checklist(
                organization_id=str(self.org.pk),
                vehicle_id=vehicle.pk,
                template_id=template_id,
                as_of=timezone.now(),
            )
            self.assertEqual(len(resolved.items), 6)
            self.assertEqual(
                {i.requirement.kind for i in resolved.items},
                {
                    "PRESENCE_CONDITION",
                    "QUANTITY",
                    "MEASUREMENT",
                    "SPECIAL_OBSERVATION",
                    "VEHICLE_READING",
                },
            )
            cabinet = [
                i
                for i in resolved.items
                if i.compartment_label and "cabinet" in i.compartment_label
            ]
            self.assertTrue(cabinet)
        # Each ambulance owns its own slot rows.
        self.assertEqual(
            Compartment.objects.filter(label="Synthetic patient compartment cabinet").count(), 4
        )

    def test_seed_is_idempotent(self):
        self.run_seed()
        counts = (
            Vehicle.objects.count(),
            Item.objects.count(),
            ChecklistItemDef.objects.count(),
            ChecklistPublication.objects.count(),
        )
        self.assertIn("already published", self.run_seed())
        self.assertEqual(
            counts,
            (
                Vehicle.objects.count(),
                Item.objects.count(),
                ChecklistItemDef.objects.count(),
                ChecklistPublication.objects.count(),
            ),
        )

    def test_actor_must_be_an_authorized_active_user(self):
        for actor, message in (
            ("nope", "canonical UUID"),
            (str(uuid4()), "not an active user"),
        ):
            with self.subTest(actor), self.assertRaisesRegex(CommandError, message):
                call_command(
                    "seed_synthetic_fleet", f"--actor-user-id={actor}", stdout=io.StringIO()
                )
        with self.assertRaisesRegex(CommandError, "not permitted"):
            call_command(
                "seed_synthetic_fleet", f"--actor-user-id={self.crew.pk}", stdout=io.StringIO()
            )
        self.assertFalse(ChecklistPublication.objects.exists())

    def test_loader_refuses_unmarked_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "bad.json").write_text(json.dumps({"vehicles": []}))
            with self.assertRaises(FixtureError):
                load_fixture("bad.json", fixture_dir=directory)
