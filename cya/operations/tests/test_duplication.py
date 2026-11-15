"""Duplicating a checklist template or a vehicle (SPEC 4.8 CFG-3, CFG-4; issue #130 part 4)."""

from cya.checklists import commands
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistTemplate,
    ChecklistVersion,
)
from cya.core.audit_commands import CommandDenied
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.kits.keys import KeyRuleError
from cya.kits.models import (
    ChecklistKitPlacement,
    VehicleKitListPublication,
    VehicleKitRequirement,
)
from cya.kits.tests.support import KitCase
from cya.obligations.models import RequirementBinding
from cya.operations.duplication import DEFINITION_FIELDS, DuplicationError, duplicate_template


def definitions_of(version_id):
    return [
        tuple(getattr(d, name) for name in DEFINITION_FIELDS if name != "display_order")
        + (d.display_order,)
        for d in ChecklistItemDef.objects.filter(checklist_version_id=version_id).order_by(
            "display_order", "id"
        )
    ]


def placements(version_id):
    return list(
        ChecklistKitPlacement.objects.filter(checklist_version_id=version_id)
        .order_by("display_order")
        .values_list("definition_id", "display_order")
    )


class TemplateDuplicationTests(KitCase):
    def setUp(self):
        super().setUp()
        definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **self.context()
        ).body["definition_id"]
        contents = save_kit_definition_draft(
            definition_id=definition,
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
        publish_kit_definition_version(version_id=contents, **self.context())
        items = self.base_items()
        items[0]["expiration_tracked"] = True
        self.source = commands.save_template_draft(
            template_id=str(self.template.pk),
            items=items,
            placements=[{"definition_id": definition, "display_order": 5}],
            **self.context(),
        ).body["version_id"]
        self.publish(self.source)

    def duplicate(self, actor=None, key="daily-copy", version=None, label="Synthetic copy"):
        return duplicate_template(
            source_version_id=version or self.source,
            label=label,
            key=key,
            **self.context(actor or self.supervisor),
        )

    def test_a_supervisor_copies_items_placements_and_settings_into_a_new_draft(self):
        before = (
            ChecklistVersion.objects.count(),
            ChecklistPublication.objects.count(),
            RequirementBinding.objects.count(),
        )
        body = self.duplicate().body
        template = ChecklistTemplate.objects.get(pk=body["template_id"])
        self.assertEqual(
            (template.label, template.key, template.active), ("Synthetic copy", "daily-copy", True)
        )
        version = ChecklistVersion.objects.get(template=template)
        self.assertEqual((str(version.pk), version.revision), (body["version_id"], 1))
        self.assertEqual(definitions_of(version.pk), definitions_of(self.source))
        self.assertEqual(placements(version.pk), placements(self.source))
        self.assertEqual((body["item_count"], body["placement_count"]), (3, 1))
        # Only the new template and its draft were written: no publication, binding or other.
        self.assertEqual(
            (
                ChecklistVersion.objects.count(),
                ChecklistPublication.objects.count(),
                RequirementBinding.objects.count(),
            ),
            (before[0] + 1, before[1], before[2]),
        )
        self.assertFalse(ChecklistPublication.objects.filter(checklist_version=version).exists())
        self.assertEqual(ChecklistVersion.objects.filter(template=self.template).count(), 1)

    def test_a_draft_version_can_be_the_source(self):
        draft = self.draft()
        body = self.duplicate(version=draft).body
        self.assertEqual(definitions_of(body["version_id"]), definitions_of(draft))
        self.assertEqual(placements(body["version_id"]), [])

    def test_keys_roles_and_sources_are_checked(self):
        self.duplicate()
        with self.assertRaises(DuplicationError):
            self.duplicate()  # the key is taken
        with self.assertRaises(KeyRuleError):
            self.duplicate(key="Not A Key")
        with self.assertRaises(CommandDenied):
            self.duplicate(actor=self.crew, key="crew-copy")
        with self.assertRaises(DuplicationError):
            self.duplicate(key="missing-copy", version="00000000-0000-4000-8000-000000000000")
        self.duplicate(actor=self.admin, key="admin-copy")
        self.assertEqual(ChecklistTemplate.objects.filter(key__endswith="-copy").count(), 2)


class VehicleDuplicationTests(KitCase):
    VIN = "1FDXE4FS8KDC00008"  # synthetic

    def setUp(self):
        super().setUp()
        from cya.kits.kit_lists import publish_vehicle_kit_list, save_vehicle_kit_list_draft
        from cya.kits.positions import add_position, revise_position
        from cya.kits.sealable import designate_compartment_sealable

        self.publish_schedule("test-0600")
        self.version = self.published_version()
        self.bind()
        definition = create_kit_definition(
            key="jump-kit", label="Synthetic jump kit", **self.context()
        ).body["definition_id"]
        contents = save_kit_definition_draft(
            definition_id=definition,
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
        publish_kit_definition_version(version_id=contents, **self.context())
        self.definition = definition
        kit_list = save_vehicle_kit_list_draft(
            vehicle_id=str(self.vehicle.pk),
            lines=[
                {
                    "definition_id": definition,
                    "compartment_id": str(self.cabinet.pk),
                    "required_count": 1,
                    "display_order": 1,
                }
            ],
            **self.context(),
        ).body["version_id"]
        publish_vehicle_kit_list(version_id=kit_list, **self.context())
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            **self.context(),
        )
        drawer = add_position(
            vehicle_id=str(self.vehicle.pk),
            kind="COMPARTMENT",
            label="Synthetic drawer",
            key="drawer",
            **self.context(self.supervisor),
        ).body["stable_slot_id"]
        revise_position(
            vehicle_id=str(self.vehicle.pk),
            kind="COMPARTMENT",
            stable_slot_id=drawer,
            retired=True,
            **self.context(self.supervisor),
        )
        self.override = commands.save_override_draft(
            vehicle_id=str(self.vehicle.pk),
            template_version_id=self.version,
            entries=[
                {"item_id": str(self.suction.pk), "action": "REMOVE"},
                {
                    "item_id": str(self.masks.pk),
                    "action": "REPLACE",
                    "definition": self.spec(self.masks, 20, kind="QUANTITY", expected_quantity=4),
                },
            ],
            **self.context(),
        ).body["override_version_id"]
        commands.publish_override_version(override_version_id=self.override, **self.context())

    def preview(self):
        from cya.operations.duplication import preview_vehicle_duplicate

        return preview_vehicle_duplicate(str(self.org.pk), str(self.vehicle.pk))

    def duplicate(self, token, actor=None, vin=None):
        from cya.operations.duplication import duplicate_vehicle

        return duplicate_vehicle(
            source_vehicle_id=str(self.vehicle.pk),
            vin=vin or self.VIN,
            display_name="Synthetic Unit copy",
            number="8",
            base_token=token,
            **self.context(actor or self.supervisor),
        )

    def source_counts(self):
        from cya.checklists.models import Compartment, VehicleOverrideVersion
        from cya.kits.models import VehicleKitListVersion

        return (
            Compartment.objects.filter(vehicle=self.vehicle).count(),
            VehicleOverrideVersion.objects.filter(vehicle=self.vehicle).count(),
            VehicleKitListVersion.objects.filter(vehicle=self.vehicle).count(),
            RequirementBinding.objects.filter(vehicle=self.vehicle).count(),
        )

    def test_the_copy_takes_the_previewed_configuration_inactive_and_as_drafts(self):
        from cya.checklists.models import ChecklistPublication as Publication
        from cya.checklists.models import (
            Compartment,
            Vehicle,
            VehicleOverrideItem,
            VehicleOverrideVersion,
            VehicleSystem,
        )
        from cya.kits.models import KitInstance, VehicleKitListPublication, VehicleKitListVersion
        from cya.operations.models import VehicleActivityEvent

        preview = self.preview()
        kinds = sorted((p["kind"], p["label"], p["sealable"]) for p in preview["positions"])
        labels = {label for _kind, label, _sealable in kinds}
        self.assertNotIn("Synthetic drawer", labels, "a retired position is not copied")
        self.assertIn(("COMPARTMENT", self.cabinet.label, True), kinds)
        self.assertEqual(preview["excluded"], [])
        self.assertEqual(
            preview["bindings"], [{"template_id": str(self.template.pk), "slot": "test-0600"}]
        )
        before = self.source_counts()
        body = self.duplicate(preview["base_token"]).body
        copy = Vehicle.objects.get(pk=body["vehicle_id"])
        self.assertEqual((copy.key, copy.active, copy.number), (self.VIN, False, "8"))
        self.assertEqual(
            list(VehicleActivityEvent.objects.filter(vehicle=copy).values_list("active", "basis")),
            [(False, "CHANGE")],
        )
        cabinet = Compartment.objects.get(vehicle=copy, stable_slot_id=self.cabinet.stable_slot_id)
        self.assertEqual((cabinet.revision, cabinet.sealable), (1, True))
        self.assertTrue(
            VehicleSystem.objects.filter(
                vehicle=copy, stable_slot_id=self.system.stable_slot_id
            ).exists()
        )
        self.assertEqual(Compartment.objects.filter(vehicle=copy, retired=True).count(), 0)
        kit_list = VehicleKitListVersion.objects.get(vehicle=copy)
        self.assertFalse(VehicleKitListPublication.objects.filter(version=kit_list).exists())
        self.assertEqual(
            list(
                VehicleKitRequirement.objects.filter(version=kit_list).values_list(
                    "definition_id", "compartment_id", "required_count"
                )
            ),
            [(self.definition, str(cabinet.pk), 1)],
        )
        override = VehicleOverrideVersion.objects.get(vehicle=copy)
        self.assertEqual(str(override.template_version_id), self.version)
        self.assertFalse(Publication.objects.filter(override_version=override).exists())
        entries = {
            e.action: e.definition
            for e in VehicleOverrideItem.objects.filter(override_version=override).select_related(
                "definition"
            )
        }
        self.assertIsNone(entries["REMOVE"])
        self.assertEqual(
            (entries["REPLACE"].expected_quantity, str(entries["REPLACE"].compartment_id)),
            (4, str(cabinet.pk)),
        )
        self.assertEqual(
            list(RequirementBinding.objects.filter(vehicle=copy).values_list("action", "revision")),
            [("BIND", 1)],
        )
        self.assertFalse(KitInstance.objects.filter(vehicle=copy).exists())
        self.assertEqual(self.source_counts(), before, "the source is unchanged")

    def test_confirmation_is_refused_if_the_source_changed_since_the_preview(self):
        from cya.kits.positions import revise_position

        token = self.preview()["base_token"]
        revise_position(
            vehicle_id=str(self.vehicle.pk),
            kind="COMPARTMENT",
            stable_slot_id=str(self.cabinet.stable_slot_id),
            label="Renamed cabinet",
            **self.context(self.supervisor),
        )
        with self.assertRaises(DuplicationError):
            self.duplicate(token)
        self.duplicate(self.preview()["base_token"])

    def test_vins_roles_and_tokens_are_checked(self):
        token = self.preview()["base_token"]
        with self.assertRaises(CommandDenied):
            self.duplicate(token, actor=self.crew)
        with self.assertRaises(KeyRuleError):
            self.duplicate(token, vin="NOT-A-VIN")
        with self.assertRaises(DuplicationError):
            self.duplicate("")
        self.duplicate(token, actor=self.admin)
        with self.assertRaises(DuplicationError):
            self.duplicate(token)  # that VIN now exists

    def test_an_override_over_an_older_template_version_is_left_out_and_shown(self):
        from cya.checklists.models import VehicleOverrideVersion

        self.published_version()  # a new template version: the override no longer governs
        preview = self.preview()
        self.assertEqual(preview["overrides"], [])
        self.assertEqual(len(preview["excluded"]), 1)
        self.assertIn("older template version", preview["excluded"][0])
        body = self.duplicate(preview["base_token"]).body
        self.assertFalse(VehicleOverrideVersion.objects.filter(vehicle_id=body["vehicle_id"]))
        self.assertEqual(body["excluded"], preview["excluded"])

    def test_old_revisions_are_not_read_to_build_the_plan(self):
        # PR #166 review: position and binding histories only grow; the plan reads one row
        # per position and per binding, so its cost must not follow their length.
        from django.db import connection

        from cya.kits.positions import revise_position
        from cya.operations.duplication import plan_vehicle_copy

        def rows_read():
            total = [0]

            def count(execute, sql, params, many, context):
                result = execute(sql, params, many, context)
                if sql.lstrip().upper().startswith("SELECT"):
                    total[0] += max(context["cursor"].rowcount, 0)
                return result

            with connection.execute_wrapper(count):
                plan = plan_vehicle_copy(str(self.org.pk), str(self.vehicle.pk))
            return total[0], plan

        before, _plan = rows_read()
        slot = str(self.cabinet.stable_slot_id)
        for number in range(25):
            revise_position(
                vehicle_id=str(self.vehicle.pk),
                kind="COMPARTMENT",
                stable_slot_id=slot,
                label=f"Synthetic cabinet {number}",
                **self.context(self.supervisor),
            )
        for _ in range(5):
            self.bind(action="UNBIND", reconcile=False)
            self.bind(reconcile=False)
        after, plan = rows_read()
        self.assertEqual(after, before, "rows read do not grow with history")
        labels = {row.label for kind, row in plan.positions if kind == "COMPARTMENT"}
        self.assertIn("Synthetic cabinet 24", labels)
        self.assertEqual(
            plan.bindings, [(str(self.template.pk), "test-0600")], "the latest revision is a BIND"
        )

    def test_an_empty_kit_list_in_force_is_copied_and_no_list_is_not(self):
        # PR #166 review: an empty list in force means "carries no kits", which differs from
        # having no list; CFG-4 copies the list in force either way.
        from cya.kits.kit_lists import publish_vehicle_kit_list, save_vehicle_kit_list_draft
        from cya.kits.models import VehicleKitListVersion
        from cya.operations.duplication import duplicate_vehicle, preview_vehicle_duplicate

        def copy(vin):
            preview = preview_vehicle_duplicate(str(self.org.pk), str(self.vehicle_b.pk))
            body = duplicate_vehicle(
                source_vehicle_id=str(self.vehicle_b.pk),
                vin=vin,
                display_name="Synthetic B copy",
                base_token=preview["base_token"],
                **self.context(self.supervisor),
            ).body
            return (
                preview,
                body,
                VehicleKitListVersion.objects.filter(vehicle_id=body["vehicle_id"]),
            )

        preview, body, lists = copy("1FDXE4FS8KDC00011")
        self.assertFalse(preview["kit_list_in_force"])
        self.assertIsNone(body["kit_list"])
        self.assertFalse(lists.exists(), "no list in force: the copy gets none")

        empty = save_vehicle_kit_list_draft(
            vehicle_id=str(self.vehicle_b.pk), lines=[], **self.context()
        ).body["version_id"]
        publish_vehicle_kit_list(version_id=empty, **self.context())
        preview, body, lists = copy("1FDXE4FS8KDC00012")
        self.assertTrue(preview["kit_list_in_force"])
        self.assertEqual((preview["kit_lines"], body["kit_list"]), ([], 0))
        self.assertEqual(lists.count(), 1, "an empty list in force is copied as an empty draft")
        self.assertFalse(VehicleKitRequirement.objects.filter(version=lists.get()).exists())
        self.assertFalse(
            VehicleKitListPublication.objects.filter(version=lists.get()).exists(), "a draft"
        )
