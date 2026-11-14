"""Vehicle file preview, import and export (SPEC 2.1.0 IMP-2 to IMP-7; issue #129)."""

import copy
from datetime import UTC

from cya.checklists.models import (
    Compartment,
    Item,
    KitInstance,
    Vehicle,
    VehicleOverrideItem,
    VehicleOverrideVersion,
    VehicleSystem,
)
from cya.core.audit_commands import CommandDenied
from cya.kits import vehicle_files
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.kits.keys import set_position_key, set_template_key
from cya.kits.kit_lists import publish_vehicle_kit_list
from cya.kits.library_import import ImportRefused
from cya.kits.models import (
    KitInstanceDefinition,
    PositionKey,
    VehicleKitListVersion,
    VehicleKitRequirement,
)
from cya.kits.tests.support import KitCase
from cya.kits.vehicle_import import import_vehicle_file, preview_vehicle_file, vehicle_file
from cya.obligations.models import RequirementBinding

VIN = "1FDXE4FS8KDC00009"  # synthetic


class VehicleImportCase(KitCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600")
        if not self.has_published_version():
            self.published_version()
        set_template_key(template_id=str(self.template.pk), key="daily", **self.context())
        for kind, slot, key in (
            ("COMPARTMENT", self.cabinet.stable_slot_id, "cabinet"),
            ("SYSTEM", self.system.stable_slot_id, "lights"),
        ):
            set_position_key(kind=kind, stable_slot_id=str(slot), key=key, **self.context())
        self.definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **self.context()
        ).body["definition_id"]
        version = save_kit_definition_draft(
            definition_id=self.definition,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "QUANTITY",
                    "expected_quantity": 2,
                    "allowed_outcomes": ["PASSED", "FAILED"],
                }
            ],
            **self.context(),
        ).body["version_id"]
        publish_kit_definition_version(version_id=version, **self.context())

    def has_published_version(self):
        from cya.checklists.models import ChecklistPublication

        return ChecklistPublication.objects.filter(
            checklist_version__template=self.template
        ).exists()

    def file(self, **changes):
        vehicle = {
            "vin": VIN,
            "display_name": "Synthetic Unit 9",
            "number": "9",
            "template": "daily",
            "schedule": "test-0600",
            "compartments": [
                {"key": "cabinet", "label": "Synthetic cabinet"},
                {"key": "drawer", "label": "Synthetic drawer"},
            ],
            "systems": [{"key": "lights", "label": "Synthetic lights"}],
            "items": [
                {
                    "item": "Synthetic oxygen wrench",
                    "compartment": "drawer",
                    "system": None,
                    "kind": "PRESENCE_CONDITION",
                    "expected_quantity": None,
                    "unit": None,
                    "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
                    "instructions": "",
                }
            ],
            "kits": [{"kit": "jump-kit", "compartment": "drawer", "count": 2}],
        }
        vehicle.update(changes)
        return vehicle_files.render(vehicle, "json")

    def import_(self, data, plan=None, actor=None):
        plan = plan or preview_vehicle_file(str(self.org.pk), data, "json")
        self.assertEqual(plan.errors, [])
        return import_vehicle_file(
            data=data, fmt="json", base_token=plan.base_token, **self.context(actor)
        )


class PreviewTests(VehicleImportCase):
    def test_a_preview_writes_nothing_and_summarizes_the_plan(self):
        counts = (Vehicle.objects.count(), Compartment.objects.count(), Item.objects.count())
        plan = preview_vehicle_file(str(self.org.pk), self.file(), "json")
        self.assertEqual(plan.errors, [])
        self.assertEqual(len(plan.base_token), 64)
        self.assertEqual(
            plan.summary(),
            {
                "vehicle": "CREATE",
                "positions_added": 3,
                "positions_relabelled": 0,
                "items_created": 1,
                "override_entries": 1,
                "kit_list_changed": True,
                "kits_added": 2,
                "binding_slot": "test-0600",
            },
        )
        self.assertEqual(
            (Vehicle.objects.count(), Compartment.objects.count(), Item.objects.count()), counts
        )

    def test_problems_are_listed(self):
        bad = self.file(
            vin="NOT-A-VIN",
            template="nope",
            kits=[{"kit": "missing", "compartment": "nowhere", "count": 0}],
        )
        errors = preview_vehicle_file(str(self.org.pk), bad, "json").errors
        for fragment in ("vin", "template", "kits[0].kit"):
            self.assertTrue(any(e.startswith(fragment) for e in errors), (fragment, errors))


class ImportTests(VehicleImportCase):
    def test_an_import_creates_the_vehicle_its_positions_drafts_kits_and_binding(self):
        result = self.import_(self.file())
        vehicle = Vehicle.objects.get(key=VIN)
        self.assertEqual(result.body["vehicle_id"], str(vehicle.pk))
        self.assertEqual((vehicle.display_name, vehicle.number), ("Synthetic Unit 9", "9"))
        cabinet = Compartment.objects.get(
            vehicle=vehicle, stable_slot_id=self.cabinet.stable_slot_id
        )
        self.assertEqual(cabinet.label, "Synthetic cabinet")
        drawer_key = PositionKey.objects.get(kind="COMPARTMENT", key="drawer")
        drawer = Compartment.objects.get(vehicle=vehicle, stable_slot_id=drawer_key.stable_slot_id)
        self.assertTrue(VehicleSystem.objects.filter(vehicle=vehicle).exists())
        override = VehicleOverrideVersion.objects.get(vehicle=vehicle)
        (entry,) = VehicleOverrideItem.objects.filter(override_version=override)
        self.assertEqual((entry.action, entry.item.label), ("ADD", "Synthetic oxygen wrench"))
        version = VehicleKitListVersion.objects.get(vehicle=vehicle)
        (line,) = VehicleKitRequirement.objects.filter(version=version)
        self.assertEqual((line.compartment_id, line.required_count), (drawer.pk, 2))
        kits = KitInstance.objects.filter(vehicle=vehicle, compartment=drawer, active=True)
        self.assertEqual(kits.count(), 2)
        self.assertEqual(
            set(
                KitInstanceDefinition.objects.filter(kit__in=kits).values_list(
                    "definition_id", flat=True
                )
            ),
            {self.definition},
        )
        binding = RequirementBinding.objects.get(vehicle=vehicle)
        self.assertEqual((binding.slot, binding.action), ("test-0600", "BIND"))

    def test_a_stale_preview_is_refused_and_a_replay_returns_the_result(self):
        data = self.file()
        plan = preview_vehicle_file(str(self.org.pk), data, "json")
        context = self.context()
        first = import_vehicle_file(data=data, fmt="json", base_token=plan.base_token, **context)
        again = import_vehicle_file(data=data, fmt="json", base_token=plan.base_token, **context)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        with self.assertRaises(ImportRefused):
            self.import_(data, plan=plan)  # the configuration it read has changed

    def test_an_import_never_changes_an_existing_binding(self):
        self.import_(self.file())
        self.publish_schedule("test-1800")
        errors = preview_vehicle_file(
            str(self.org.pk), self.file(schedule="test-1800"), "json"
        ).errors
        self.assertTrue(any(e.startswith("schedule") for e in errors))

    def test_a_reconciliation_failure_after_commit_is_logged(self):
        from datetime import datetime
        from types import SimpleNamespace
        from unittest import mock

        failure = SimpleNamespace(
            vehicle_id="v", due_at=datetime(2026, 1, 1, tzinfo=UTC), error="boom"
        )
        with (
            mock.patch(
                "cya.kits.commands.reconcile_vehicle",
                return_value=SimpleNamespace(failures=[failure]),
            ) as reconcile,
            self.assertLogs("cya.kits.commands", level="ERROR") as logs,
        ):
            result = self.import_(self.file())
        reconcile.assert_called_once_with(str(self.org.pk), result.body["vehicle_id"])
        self.assertIn("boom", logs.output[0])
        self.assertTrue(Vehicle.objects.filter(key=VIN).exists(), "the import stays committed")

    def test_only_administrators_import(self):
        with self.assertRaises(CommandDenied):
            self.import_(self.file(), actor=self.supervisor)
        self.assertFalse(Vehicle.objects.filter(key=VIN).exists())


class RoundTripTests(VehicleImportCase):
    def test_an_export_reimports_unchanged_once_its_drafts_are_published(self):
        self.import_(self.file())
        vehicle = Vehicle.objects.get(key=VIN)
        publish_vehicle_kit_list(
            version_id=str(VehicleKitListVersion.objects.get(vehicle=vehicle).pk), **self.context()
        )
        from cya.checklists import commands

        commands.publish_override_version(
            override_version_id=str(VehicleOverrideVersion.objects.get(vehicle=vehicle).pk),
            **self.context(),
        )
        exported = vehicle_file(str(self.org.pk), str(vehicle.pk))
        for fmt in vehicle_files.FORMATS:
            with self.subTest(fmt=fmt):
                data = vehicle_files.render(exported, fmt)
                plan = preview_vehicle_file(str(self.org.pk), data, fmt)
                self.assertEqual(plan.errors, [])
                summary = plan.summary()
                self.assertEqual(
                    {k: v for k, v in summary.items() if k != "vehicle"},
                    {
                        "positions_added": 0,
                        "positions_relabelled": 0,
                        "items_created": 0,
                        "override_entries": 0,
                        "kit_list_changed": False,
                        "kits_added": 0,
                        "binding_slot": None,
                    },
                )
        self.assertEqual(copy.deepcopy(exported)["vin"], VIN)
