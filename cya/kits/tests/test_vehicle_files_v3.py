"""Vehicle files with the vehicle's item requirements (SPEC 6.0.0 IMP-7, CHK-11; issue #256 C4).

After the organization's item-requirement cutover a vehicle file (schema 3) carries the
vehicle's item requirements, at most one per item and place, and each checklist item names one.
Import still writes drafts only: requirements arise when a checklist is published.
"""

import json

from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistVersion,
    Vehicle,
    VehicleItemRequirement,
    VehicleItemRequirementVersion,
)
from cya.kits import vehicle_files
from cya.kits.library_files import FileErrors
from cya.kits.tests.test_vehicle_files_v2 import COPY_VIN, VehicleFileV2Case


class VehicleFileV3Case(VehicleFileV2Case):
    def setUp(self):
        super().setUp()
        self.record_item_cutover()
        self.publish_checklist(self.first, self.own_items())
        self.publish_checklist(self.second, self.own_items()[:1])

    def document(self):
        return json.loads(self.export())

    def requirement(self, document, item):
        return next(r for r in document["vehicle"]["requirements"] if r["item"] == item.label)

    def as_v2(self, document):
        """The same configuration in schema 2: each item carries its requirement's values."""
        vehicle = document["vehicle"]
        by_id = {r["id"]: r for r in vehicle.pop("requirements")}
        for checklist in vehicle["checklists"]:
            checklist["items"] = [
                {k: v for k, v in by_id[i["requirement"]].items() if k != "id"}
                for i in checklist["items"]
            ]
        document["schema_version"] = 2
        return document

    def publish_requirement(self, item, **changes):
        from cya.checklists.commands import publish_item_requirement_version

        version = (
            VehicleItemRequirementVersion.objects.filter(vehicle=self.source, item=item)
            .order_by("-effective_at")
            .first()
        )
        values = {
            "kind": version.kind,
            "allowed_outcomes": version.allowed_outcomes,
            "instructions": version.instructions,
            "expected_quantity": version.expected_quantity,
            "unit": version.unit,
            "compartment_id": str(version.compartment_id) if version.compartment_id else None,
            "system_id": str(version.system_id) if version.system_id else None,
        }
        publish_item_requirement_version(
            requirement_id=str(version.requirement_id), **{**values, **changes}, **self.context()
        )


class ExportTests(VehicleFileV3Case):
    def test_the_export_carries_one_requirement_per_item_and_place(self):
        document = self.document()
        self.assertEqual(document["schema_version"], 3)
        requirements = document["vehicle"]["requirements"]
        self.assertEqual(
            sorted(r["item"] for r in requirements),
            sorted(i.label for i in (self.suction, self.masks, self.siren)),
        )
        daily, weekly = document["vehicle"]["checklists"]
        suction = self.requirement(document, self.suction)["id"]
        self.assertIn({"requirement": suction}, daily["items"])
        self.assertEqual(weekly["items"], [{"requirement": suction}])  # shared, not copied

    def test_the_export_follows_a_requirement_edited_since_publication(self):  # CHK-5
        self.publish_requirement(self.masks, expected_quantity=9)
        self.assertEqual(self.requirement(self.document(), self.masks)["quantity"], 9)

    def test_a_requirement_no_checklist_inspects_is_still_exported(self):
        self.publish_checklist(self.first, [self.own_items()[0], self.own_items()[2]])
        document = self.document()
        masks = self.requirement(document, self.masks)
        listed = {i["requirement"] for c in document["vehicle"]["checklists"] for i in c["items"]}
        self.assertNotIn(masks["id"], listed)
        plan = self.preview(json.dumps(document).encode())
        self.assertEqual(plan.errors, [])
        self.assertTrue(
            any("no checklist in the file inspects it" in n for n in plan.requirement_notes)
        )

    def test_the_export_reimports_unchanged(self):
        for fmt in ("json", "csv"):
            with self.subTest(fmt=fmt):
                plan = self.preview(self.export(fmt), fmt)
                self.assertEqual(plan.errors, [])
                summary = plan.summary()
                self.assertEqual(
                    (summary["checklists_created"], summary["checklist_drafts"]), (0, 0)
                )
                self.assertEqual(plan.requirement_notes, [])


class ImportTests(VehicleFileV3Case):
    def test_a_new_vehicle_gets_drafts_only(self):
        body = self.apply(self.as_copy(self.export()))
        copy = Vehicle.objects.get(key=COPY_VIN)
        self.assertEqual(body["checklists_created"], 2)
        self.assertFalse(VehicleItemRequirement.objects.filter(vehicle=copy).exists())
        daily = ChecklistVersion.objects.get(template__vehicle=copy, template__label="Daily")
        masks = ChecklistItemDef.objects.get(checklist_version=daily, item=self.masks)
        self.assertEqual(masks.expected_quantity, 6)

    def test_a_requirement_listed_twice_in_one_place_is_refused(self):
        document = self.document()
        requirements = document["vehicle"]["requirements"]
        requirements.append(dict(self.requirement(document, self.masks), id="another"))
        with self.assertRaises(FileErrors) as refused:
            vehicle_files.parse(json.dumps(document).encode(), "json")
        self.assertIn("already has requirement", " ".join(refused.exception.errors))

    def test_an_unknown_requirement_id_is_refused(self):
        document = self.document()
        document["vehicle"]["checklists"][1]["items"] = [{"requirement": "missing"}]
        with self.assertRaises(FileErrors) as refused:
            vehicle_files.parse(json.dumps(document).encode(), "json")
        self.assertIn("no requirement has that id", " ".join(refused.exception.errors))

    def test_a_schema_2_file_giving_one_place_two_requirements_is_refused(self):  # AC-42 (1)
        document = self.as_v2(self.document())
        weekly = document["vehicle"]["checklists"][1]
        weekly["items"][0]["instructions"] = "Synthetic other."
        plan = self.preview(json.dumps(document).encode())
        self.assertTrue(any("different requirements" in e for e in plan.errors), plan.errors)
        self.assertEqual(self.preview(json.dumps(self.as_v2(self.document())).encode()).errors, [])

    def test_a_change_to_a_requirement_shared_outside_the_file_is_called_out(self):
        document = self.document()
        self.requirement(document, self.suction)["instructions"] = "Synthetic changed."
        document["vehicle"]["checklists"] = document["vehicle"]["checklists"][:1]  # Daily only
        plan = self.preview(json.dumps(document).encode())
        self.assertEqual(plan.errors, [])
        (note,) = [n for n in plan.requirement_notes if self.suction.label in n]
        self.assertIn("which Weekly also inspect", note)

    def test_the_example_file_is_schema_3(self):
        from cya.kits import vehicle_files_v3

        parsed = vehicle_files.parse(
            vehicle_files_v3.render(vehicle_files_v3.TEMPLATE_VEHICLE, "csv"), "csv"
        )
        self.assertEqual(parsed["schema_version"], 3)
        self.assertEqual(len(parsed["requirements"]), 2)
