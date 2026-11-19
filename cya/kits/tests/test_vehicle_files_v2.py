"""Vehicle files with the vehicle's own checklists (SPEC 5.0.0 IMP-6, IMP-7, AC-40(4); #220).

After the cutover a vehicle file (schema 2) carries the vehicle's positions with file-local
keys, its complete checklists with their schedule assignments, the kit definitions they inspect
and its kit list. A file from before (schema 1) takes the legacy path: one independent checklist
from the named template, or a refusal when the template does not resolve.
"""

import json

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistTemplate,
    ChecklistVersion,
    Compartment,
    Vehicle,
)
from cya.kits import vehicle_files
from cya.kits.keys import set_vehicle_position_key
from cya.kits.library_import import ImportRefused
from cya.kits.tests.test_vehicle_import import VehicleImportCase
from cya.kits.vehicle_import import import_vehicle_file, preview_vehicle_file, vehicle_file
from cya.obligations.models import RequirementBinding

SOURCE_VIN = "1FDXE4FS8KDC00011"  # synthetic
COPY_VIN = "1FDXE4FS8KDC00012"  # synthetic


class VehicleFileV2Case(VehicleImportCase):
    """A legacy setup, then the cutover, then vehicle A given its own checklists and keys."""

    def setUp(self):
        super().setUp()
        self.record_cutover()
        self.source = self.vehicles[0]
        Vehicle.objects.filter(pk=self.source.pk).update(key=SOURCE_VIN)
        for kind, slot, key in (
            ("COMPARTMENT", self.cabinet.stable_slot_id, "cabinet"),
            ("SYSTEM", self.system.stable_slot_id, "lights"),
        ):
            set_vehicle_position_key(
                vehicle_id=str(self.source.pk),
                kind=kind,
                stable_slot_id=str(slot),
                key=key,
                **self.context(),
            )
        self.first = self.vehicle_checklist(label="Daily")
        self.publish_checklist(self.first, self.own_items())
        self.second = self.vehicle_checklist(label="Weekly")
        self.publish_checklist(self.second, self.own_items()[:1])
        from cya.checklists.tests.support import ids
        from cya.obligations.commands import bind_requirement

        bind_requirement(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            vehicle_id=str(self.source.pk),
            template_id=str(self.first.pk),
            slot="test-0600",
            **ids(),
        )

    def export(self, fmt="json"):
        return vehicle_files.render(vehicle_file(str(self.org.pk), str(self.source.pk)), fmt)

    def preview(self, data, fmt="json"):
        return preview_vehicle_file(str(self.org.pk), data, fmt)

    def apply(self, data, fmt="json"):
        plan = self.preview(data, fmt)
        self.assertEqual(plan.errors, [])
        return import_vehicle_file(
            data=data, fmt=fmt, base_token=plan.base_token, **self.context()
        ).body

    def as_copy(self, data):
        document = json.loads(data)
        document["vehicle"]["vin"] = COPY_VIN
        document["vehicle"]["display_name"] = "Synthetic copy"
        return json.dumps(document).encode()


class ExportTests(VehicleFileV2Case):
    def test_the_export_holds_each_checklist_in_order_with_its_assignments(self):
        document = json.loads(self.export())
        self.assertEqual(document["schema_version"], 2)
        checklists = document["vehicle"]["checklists"]
        self.assertEqual([c["label"] for c in checklists], ["Daily", "Weekly"])
        self.assertEqual(checklists[0]["schedules"], ["test-0600"])
        self.assertEqual(checklists[1]["schedules"], [])
        siren = next(i for i in checklists[0]["items"] if i["item"] == self.siren.label)
        self.assertNotIn("compartment", siren)  # Not placed
        self.assertNotIn("system", siren)
        self.assertNotIn("template", document["vehicle"])

    def test_the_export_reimports_unchanged(self):
        for fmt in ("json", "csv"):
            with self.subTest(fmt=fmt):
                plan = self.preview(self.export(fmt), fmt)
                self.assertEqual(plan.errors, [])
                summary = plan.summary()
                self.assertEqual(
                    (
                        summary["checklists_created"],
                        summary["checklist_drafts"],
                        summary["assignments_added"],
                        summary["positions_added"],
                    ),
                    (0, 0, 0, 0),
                )


class ImportAsNewVehicleTests(VehicleFileV2Case):
    def test_a_copy_gets_its_own_positions_checklists_and_assignments(self):
        body = self.apply(self.as_copy(self.export()))
        copy = Vehicle.objects.get(key=COPY_VIN)
        self.assertEqual(body["checklists_created"], 2)
        checklists = list(ChecklistTemplate.objects.filter(vehicle=copy).order_by("precedence"))
        self.assertEqual(
            [(c.label, c.precedence) for c in checklists], [("Daily", 1), ("Weekly", 2)]
        )
        # New vehicle-local positions: no stable slot shared with the source.
        source_slots = set(
            Compartment.objects.filter(vehicle=self.source).values_list("stable_slot_id", flat=True)
        )
        copy_slots = set(
            Compartment.objects.filter(vehicle=copy).values_list("stable_slot_id", flat=True)
        )
        self.assertTrue(copy_slots)
        self.assertFalse(source_slots & copy_slots)
        daily = ChecklistVersion.objects.get(template=checklists[0])
        definitions = ChecklistItemDef.objects.filter(checklist_version=daily)
        siren = definitions.get(item=self.siren)
        self.assertIsNone(siren.compartment_id)  # Not placed survives the round trip
        for definition in definitions.exclude(compartment__isnull=True):
            self.assertEqual(definition.compartment.vehicle_id, copy.pk)
        self.assertTrue(
            RequirementBinding.objects.filter(
                vehicle=copy, template=checklists[0], slot="test-0600"
            ).exists()
        )
        self.assertFalse(
            RequirementBinding.objects.filter(vehicle=copy, template=checklists[1]).exists()
        )


class ImportExistingVehicleTests(VehicleFileV2Case):
    def test_a_changed_item_makes_one_draft_and_nothing_omitted_is_removed(self):
        document = json.loads(self.export())
        daily = document["vehicle"]["checklists"][0]
        daily["items"] = [
            dict(i, instructions="Synthetic changed.") if i["item"] == self.suction.label else i
            for i in daily["items"]
        ]
        document["vehicle"]["checklists"] = [daily]  # the weekly checklist is omitted
        body = self.apply(json.dumps(document).encode())
        self.assertEqual((body["checklist_drafts"], body["checklists_created"]), (1, 0))
        self.assertEqual(ChecklistVersion.objects.filter(template=self.first).count(), 2)
        self.assertEqual(ChecklistVersion.objects.filter(template=self.second).count(), 1)

    def test_a_listed_schedule_is_assigned_and_none_is_ended(self):
        document = json.loads(self.export())
        document["vehicle"]["checklists"][1]["schedules"] = ["test-0600"]
        document["vehicle"]["checklists"][0]["schedules"] = []
        body = self.apply(json.dumps(document).encode())
        self.assertEqual(body["assignments_added"], 1)
        self.assertTrue(RequirementBinding.objects.filter(template=self.second).exists())
        latest = (
            RequirementBinding.objects.filter(template=self.first).order_by("-revision").first()
        )
        self.assertEqual(latest.action, "BIND")

    def test_unknown_references_are_refused_without_writes(self):
        document = json.loads(self.export())
        daily = document["vehicle"]["checklists"][0]
        daily["schedules"] = ["no-such-slot"]
        daily["kits"] = ["no-such-kit"]
        daily["items"][0]["compartment"] = "no-such-position"
        plan = self.preview(json.dumps(document).encode())
        self.assertEqual(len(plan.errors), 3, plan.errors)
        with self.assertRaises(ImportRefused):
            import_vehicle_file(
                data=json.dumps(document).encode(),
                fmt="json",
                base_token="0" * 64,
                **self.context(),
            )


class LegacyFileTests(VehicleFileV2Case):
    def test_a_legacy_file_becomes_one_independent_checklist(self):
        body = self.apply(self.file())
        vehicle = Vehicle.objects.get(pk=body["vehicle_id"])
        checklist = ChecklistTemplate.objects.get(vehicle=vehicle)
        self.assertEqual(checklist.label, self.template.label)
        self.assertTrue(
            RequirementBinding.objects.filter(
                vehicle=vehicle, template=checklist, slot="test-0600"
            ).exists()
        )
        self.assertFalse(
            RequirementBinding.objects.filter(vehicle=vehicle, template=self.template).exists()
        )

    def test_a_legacy_file_naming_an_unknown_template_is_refused(self):
        plan = self.preview(self.file(template="no-such-template"))
        self.assertTrue(any("ambiguous" in e for e in plan.errors), plan.errors)


class BeforeCutoverTests(VehicleImportCase):
    def test_a_schema_2_file_is_refused_before_the_cutover(self):
        document = {"file_type": "vehicle", "schema_version": 2, "vehicle": {"vin": SOURCE_VIN}}
        plan = preview_vehicle_file(str(self.org.pk), json.dumps(document).encode(), "json")
        self.assertTrue(any("after the cutover" in e for e in plan.errors), plan.errors)
