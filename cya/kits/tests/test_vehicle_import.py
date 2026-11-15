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

    def test_an_imported_vehicle_starts_its_activity_history(self):
        from cya.operations.models import VehicleActivityEvent

        self.import_(self.file())
        vehicle = Vehicle.objects.get(key=VIN)
        self.assertEqual(
            list(
                VehicleActivityEvent.objects.filter(vehicle=vehicle).values_list(
                    "sequence", "active", "basis"
                )
            ),
            [(1, True, "CHANGE")],
        )

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


class ExpirationTrackedTests(VehicleImportCase):
    """Issue #152: vehicle files carry expiration_tracked for items outside kits."""

    WRENCH = "Synthetic oxygen wrench"

    def item(self, label=WRENCH, tracked=True, **changes):
        return {
            "item": label,
            "compartment": "drawer",
            "system": None,
            "kind": "PRESENCE_CONDITION",
            "expected_quantity": None,
            "unit": None,
            "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
            "expiration_tracked": tracked,
            "instructions": "",
            **changes,
        }

    def entries(self, override):
        return {
            e.item.label: e.definition.expiration_tracked
            for e in VehicleOverrideItem.objects.filter(override_version=override).select_related(
                "item", "definition"
            )
        }

    def publish_override(self, vehicle):
        from cya.checklists import commands

        override = VehicleOverrideVersion.objects.filter(vehicle=vehicle).latest("revision")
        commands.publish_override_version(override_version_id=str(override.pk), **self.context())
        return override

    def test_imports_write_it_carry_it_and_export_it(self):
        self.import_(self.file(items=[self.item()]))
        vehicle = Vehicle.objects.get(key=VIN)
        first = self.publish_override(vehicle)
        self.assertEqual(self.entries(first), {self.WRENCH: True})
        # A later file that changes another item carries the wrench entry, setting and all.
        self.import_(self.file(items=[self.item(), self.item("Synthetic spare fuse", False)]))
        second = VehicleOverrideVersion.objects.filter(vehicle=vehicle).latest("revision")
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(self.entries(second), {self.WRENCH: True, "Synthetic spare fuse": False})
        self.publish_override(vehicle)
        exported = vehicle_file(str(self.org.pk), str(vehicle.pk))
        tracked = {e["item"]: e["expiration_tracked"] for e in exported["items"]}
        self.assertEqual(tracked[self.WRENCH], True)
        self.assertEqual(tracked["Synthetic spare fuse"], False)
        # Turning the setting off is a change, not a no-op.
        plan = preview_vehicle_file(
            str(self.org.pk),
            self.file(items=[self.item(tracked=False), self.item("Synthetic spare fuse", False)]),
            "json",
        )
        self.assertEqual(plan.errors, [])
        # Only the wrench changes; the fuse already matches the published override.
        self.assertEqual(plan.summary()["override_entries"], 1)

    def test_a_value_other_than_true_or_false_is_an_input_error(self):
        plan = preview_vehicle_file(
            str(self.org.pk), self.file(items=[self.item(tracked="yes")]), "json"
        )
        self.assertTrue(any("expiration_tracked: must be true or false" in e for e in plan.errors))


class ReviewFindingTests(VehicleImportCase):
    """Codex review 5907353408 of PR #150: findings 1 to 5."""

    def drawer(self, vehicle):
        slot = PositionKey.objects.get(kind="COMPARTMENT", key="drawer").stable_slot_id
        return Compartment.objects.filter(vehicle=vehicle, stable_slot_id=slot)

    def compartments(self, drawer_label="Synthetic drawer"):
        return [
            {"key": "cabinet", "label": "Synthetic cabinet"},
            {"key": "drawer", "label": drawer_label},
        ]

    def errors(self, data, fmt="json"):
        return preview_vehicle_file(str(self.org.pk), data, fmt).errors

    def test_1_a_relabel_keeps_the_sealable_designation(self):
        from cya.kits.sealable import designate_compartment_sealable

        self.import_(self.file())
        vehicle = Vehicle.objects.get(key=VIN)
        first = self.drawer(vehicle).get()
        designate_compartment_sealable(
            vehicle_id=str(vehicle.pk),
            compartment_id=str(first.pk),
            sealable=True,
            **self.context(),
        )
        self.import_(self.file(compartments=self.compartments("Renamed drawer")))
        history = list(
            self.drawer(vehicle).order_by("revision").values_list("revision", "label", "sealable")
        )
        self.assertEqual(
            history,
            [
                (1, "Synthetic drawer", False),
                (2, "Synthetic drawer", True),
                (3, "Renamed drawer", True),
            ],
        )

    def schedule_designation(self, vehicle):
        from datetime import timedelta

        from django.utils import timezone

        from cya.kits.sealable import designate_compartment_sealable

        designate_compartment_sealable(
            vehicle_id=str(vehicle.pk),
            compartment_id=str(self.drawer(vehicle).get().pk),
            sealable=True,
            effective_at=timezone.now() + timedelta(days=1),
            **self.context(),
        )

    def test_5_a_scheduled_revision_refuses_a_relabel_but_not_an_unchanged_file(self):
        self.import_(self.file())
        vehicle = Vehicle.objects.get(key=VIN)
        self.schedule_designation(vehicle)
        self.assertEqual(self.errors(self.file()), [])
        errors = self.errors(self.file(compartments=self.compartments("Renamed drawer")))
        self.assertTrue(any("already scheduled" in e for e in errors), errors)
        self.assertEqual(self.drawer(vehicle).count(), 2)

    def test_5_a_revision_scheduled_after_the_preview_makes_it_stale(self):
        self.import_(self.file())
        vehicle = Vehicle.objects.get(key=VIN)
        data = self.file(compartments=self.compartments("Renamed drawer"))
        plan = preview_vehicle_file(str(self.org.pk), data, "json")
        self.assertEqual(plan.errors, [])
        self.schedule_designation(vehicle)
        with self.assertRaises(ImportRefused):
            import_vehicle_file(data=data, fmt="json", base_token=plan.base_token, **self.context())
        self.assertEqual(self.drawer(vehicle).count(), 2)

    def test_3_kit_counts_and_entries_are_bounded_before_anything_is_allocated(self):
        from cya.kits import vehicle_import
        from cya.kits.models import STORED_COUNT_MAX

        for count, fragment in (
            (STORED_COUNT_MAX + 1, "kits[0].count"),
            (10**30, "kits[0].count"),
            (vehicle_import.MAX_NEW_KITS + 1, "kits: the file would add"),
        ):
            with self.subTest(count=count):
                kits = [{"kit": "jump-kit", "compartment": "drawer", "count": count}]
                errors = self.errors(self.file(kits=kits))
                self.assertTrue(any(e.startswith(fragment) for e in errors), errors)
        at_limit = [
            {"kit": "jump-kit", "compartment": "drawer", "count": vehicle_import.MAX_NEW_KITS}
        ]
        self.assertEqual(self.errors(self.file(kits=at_limit)), [])
        many = [{"key": f"bay-{i}", "label": f"Bay {i}"} for i in range(1001)]
        errors = self.errors(self.file(compartments=many))
        self.assertTrue(any(e.startswith("compartments: more than") for e in errors), errors)

    def test_4_malformed_values_are_input_errors_in_preview_and_confirmation(self):
        import json

        def raw(**changes):
            document = json.loads(self.file())
            document["vehicle"].update(changes)
            return json.dumps(document).encode()

        cases = {
            "kit key is a list": raw(
                kits=[{"kit": ["jump-kit"], "compartment": "drawer", "count": 1}]
            ),
            "compartment key is an object": raw(
                kits=[{"kit": "jump-kit", "compartment": {"k": 1}, "count": 1}]
            ),
            "kit without a compartment": raw(
                kits=[{"kit": "jump-kit", "compartment": None, "count": 1}]
            ),
            "item position is a list": raw(
                items=[
                    {
                        "item": "Synthetic oxygen wrench",
                        "compartment": ["drawer"],
                        "system": None,
                        "kind": "PRESENCE_CONDITION",
                        "quantity": None,
                        "unit": None,
                    }
                ]
            ),
            "template is a list": raw(template=["daily"]),
            "schedule is an object": raw(schedule={"s": 1}),
            "deep nesting": b'{"file_type": "vehicle", "vehicle": '
            + b"[" * 100_000
            + b"]" * 100_000
            + b"}",
            "a number of 5000 digits": b'{"file_type": "vehicle", "schema_version": '
            + b"9" * 5000
            + b"}",
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(self.errors(data), [])
                with self.assertRaises(ImportRefused):
                    import_vehicle_file(
                        data=data, fmt="json", base_token="0" * 64, **self.context()
                    )
        csv_text = vehicle_files.render(self._normal(), "csv").decode()
        for bad in ("²", "9" * 20):
            with self.subTest(csv_count=bad):
                data = csv_text.replace(",jump-kit,2", f",jump-kit,{bad}").encode()
                self.assertNotEqual(data.decode(), csv_text)
                errors = self.errors(data, "csv")
                self.assertTrue(any("whole number" in e for e in errors), errors)
        self.assertFalse(Vehicle.objects.filter(key=VIN).exists())

    def _normal(self):
        return vehicle_files.parse(self.file(), "json")

    def test_2_an_import_holds_the_vehicle_so_a_concurrent_kit_add_waits(self):
        self.race_kit_add()

    def test_2_the_vehicle_lock_alone_makes_a_concurrent_kit_add_wait(self):
        # The organization lock already blocks every other command (each inserts a receipt that
        # references the organization row), so turn it off to show the vehicle lock suffices.
        from unittest import mock

        from cya.kits import vehicle_import

        original = vehicle_import._authorize

        def without_organization_lock(*args, **kwargs):
            return original(*args, **{**kwargs, "lock_organization": False})

        with mock.patch.object(vehicle_import, "_authorize", without_organization_lock):
            self.race_kit_add()

    def race_kit_add(self):
        import threading
        from unittest import mock

        from django.db import close_old_connections

        from cya.kits import vehicle_import
        from cya.kits.commands import add_kit_instance

        self.import_(self.file())
        vehicle = Vehicle.objects.get(key=VIN)
        drawer = self.drawer(vehicle).get()
        data = self.file(kits=[{"kit": "jump-kit", "compartment": "drawer", "count": 3}])
        plan = preview_vehicle_file(str(self.org.pk), data, "json")
        self.assertEqual(plan.errors, [])
        planned, release = threading.Event(), threading.Event()
        order, errors = [], []
        original = vehicle_import.build_plan

        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            planned.set()
            release.wait(timeout=30)  # the kit add runs meanwhile, unless it is blocked
            return result

        def run_import():
            try:
                with mock.patch.object(vehicle_import, "build_plan", paused):
                    import_vehicle_file(
                        data=data, fmt="json", base_token=plan.base_token, **self.context()
                    )
                order.append("imported")
            except Exception as error:  # noqa: BLE001 - asserted below
                errors.append(error)
            finally:
                close_old_connections()

        def add_kit():
            try:
                planned.wait(timeout=30)
                add_kit_instance(  # another actor, so no shared identity lock
                    label="Concurrent kit",
                    vehicle_id=str(vehicle.pk),
                    compartment_id=str(drawer.pk),
                    definition_id=self.definition,
                    **self.context(self.supervisor),
                )
                order.append("added")
            except Exception as error:  # noqa: BLE001 - asserted below
                errors.append(error)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=run_import), threading.Thread(target=add_kit)]
        for thread in threads:
            thread.start()
        self.assertTrue(planned.wait(timeout=30))
        threads[1].join(timeout=3)  # an add that is not blocked finishes here
        release.set()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(order, ["imported", "added"])
        self.assertEqual(
            KitInstance.objects.filter(
                vehicle=vehicle, compartment__stable_slot_id=drawer.stable_slot_id, active=True
            ).count(),
            4,
        )
