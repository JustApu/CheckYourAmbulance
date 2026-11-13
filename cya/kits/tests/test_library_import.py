"""Kit-library preview, import and export against the database (SPEC 4.7, issue #129)."""

import json

from cya.checklists.models import Item
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.kits import library_files
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
    set_kit_definition_active,
)
from cya.kits.library_import import (
    CHANGE,
    CREATE,
    UNCHANGED,
    ImportRefused,
    export_kit_library,
    import_kit_library,
    preview_kit_library,
)
from cya.kits.models import (
    KitDefinition,
    KitDefinitionItem,
    KitDefinitionPublication,
    KitDefinitionVersion,
)
from cya.kits.tests.support import KitCase


def kit(key="jump-kit", label="Jump kit", quantity=6, items=("Masks", "Gauge")):
    contents = [
        {
            "item": items[0],
            "kind": "QUANTITY",
            "quantity": quantity,
            "outcomes": ["PASSED", "FAILED"],
            "expiration_tracked": True,
        },
        {"item": items[1], "kind": "MEASUREMENT", "unit": "psi"},
    ]
    return {"key": key, "label": label, "contents": contents}


def library(*kits):
    return json.dumps(
        {"file_type": "kit-library", "schema_version": 1, "kits": list(kits)}
    ).encode()


class LibraryCase(KitCase):
    def preview(self, data, fmt="json"):
        return preview_kit_library(str(self.org.pk), data, fmt)

    def import_(self, data, fmt="json", token=None, actor=None, **over):
        token = token if token is not None else self.preview(data, fmt).base_token
        return import_kit_library(
            data=data, fmt=fmt, base_token=token, **{**self.context(actor or self.admin), **over}
        )

    def versions(self, key):
        return KitDefinitionVersion.objects.filter(definition__key=key).order_by("revision")


class PreviewTests(LibraryCase):
    def test_preview_plans_without_writing(self):
        counts = (Item.objects.count(), KitDefinition.objects.count(), AuditEvent.objects.count())
        plan = self.preview(library(kit()))
        self.assertEqual(plan.errors, [])
        self.assertEqual([(k.key, k.action) for k in plan.kits], [("jump-kit", CREATE)])
        self.assertEqual(plan.new_items, ["Masks", "Gauge"])
        self.assertEqual(
            plan.summary(), {"created": 1, "changed": 0, "unchanged": 0, "new_items": 2}
        )
        self.assertEqual(len(plan.base_token), 64)
        self.assertEqual(
            (Item.objects.count(), KitDefinition.objects.count(), AuditEvent.objects.count()),
            counts,
        )

    def test_preview_reports_every_problem(self):
        Item.objects.create(organization=self.org, label="Twin")
        Item.objects.create(organization=self.org, label="Twin")
        Item.objects.create(organization=self.org, label="Retired", active=False)
        bad = [
            kit(key="Bad Key"),
            kit(key="twins", items=("Twin", "Gauge")),
            kit(key="retired", items=("Retired", "Gauge")),
            {
                **kit(key="reading"),
                "contents": [{"item": "Odometer", "kind": "VEHICLE_READING", "unit": "mi"}],
            },
            {**kit(key="dupe"), "contents": [kit()["contents"][0], kit()["contents"][0]]},
            kit(key="jump-kit"),
            kit(key="jump-kit"),
        ]
        errors = self.preview(library(*bad)).errors
        for expected in (
            "Bad Key",
            "'Twin' matches 2 active items",
            "'Retired' exists only as an inactive",
            "kind must be one of",
            "'Masks' appears more than once",
            "'jump-kit' appears more than once",
        ):
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in e for e in errors), errors)
        self.assertEqual(self.preview(b"{", "json").errors[0][:5], "file:")


class ImportTests(LibraryCase):
    def test_import_creates_drafts_and_an_identical_reimport_changes_nothing(self):
        result = self.import_(library(kit(), kit(key="other-kit", label="Other kit")))
        self.assertEqual(result.body["code"], "KIT_LIBRARY_IMPORTED")
        self.assertEqual((result.body["created"], result.body["new_items"]), (2, 2))
        version = self.versions("jump-kit").get()
        rows = KitDefinitionItem.objects.filter(version=version).order_by("display_order")
        self.assertEqual(
            [(Item.objects.get(pk=r.item_id).label, r.display_order) for r in rows],
            [("Masks", 1), ("Gauge", 2)],
        )
        self.assertFalse(KitDefinitionPublication.objects.exists(), "an import never publishes")
        again = self.import_(library(kit(), kit(key="other-kit", label="Other kit")))
        self.assertEqual(
            (again.body["unchanged"], again.body["created"], again.body["changed"]), (2, 0, 0)
        )
        self.assertEqual(self.versions("jump-kit").count(), 1)
        audits = AuditEvent.objects.filter(command__command_type="KIT_LIBRARY_IMPORT")
        self.assertEqual(audits.count(), 2)
        for audit in audits:
            self.assertNotIn(
                "Masks", json.dumps(audit.new_snapshot), "the audit keeps counts, not contents"
            )

    def test_a_change_adds_a_draft_revision_and_omitted_kits_are_untouched(self):
        self.import_(library(kit(), kit(key="other-kit", label="Other kit")))
        result = self.import_(library(kit(quantity=8, label="Jump kit renamed")))
        self.assertEqual((result.body["changed"], result.body["created"]), (1, 0))
        self.assertEqual([v.revision for v in self.versions("jump-kit")], [1, 2])
        self.assertEqual(KitDefinition.objects.get(key="jump-kit").label, "Jump kit renamed")
        self.assertEqual(self.versions("other-kit").count(), 1)
        self.assertTrue(
            KitDefinition.objects.get(key="other-kit").active, "omitted kits are not deactivated"
        )

    def test_existing_items_and_definitions_are_reused(self):
        masks = Item.objects.create(organization=self.org, label="Masks")
        definition_id = create_kit_definition(
            key="jump-kit", label="Jump kit", **self.context(self.admin)
        ).body["definition_id"]
        draft = save_kit_definition_draft(
            definition_id=definition_id,
            contents=[
                {
                    "item_id": str(masks.pk),
                    "display_order": 1,
                    "kind": "PRESENCE_CONDITION",
                    "allowed_outcomes": ["PASSED"],
                }
            ],
            **self.context(self.admin),
        )
        publish_kit_definition_version(
            version_id=draft.body["version_id"], **self.context(self.admin)
        )
        plan = self.preview(library(kit()))
        self.assertEqual(
            [(k.action, k.definition_id) for k in plan.kits], [(CHANGE, definition_id)]
        )
        self.assertEqual(plan.new_items, ["Gauge"])
        self.import_(library(kit()))
        latest = self.versions("jump-kit").last()
        self.assertEqual(
            KitDefinitionItem.objects.get(version=latest, kind="QUANTITY").item_id, masks.pk
        )

    def test_a_stale_preview_and_an_invalid_file_are_refused_whole(self):
        data = library(kit())
        token = self.preview(data).base_token
        Item.objects.create(
            organization=self.org, label="Masks"
        )  # resolution changed since preview
        with self.assertRaises(ImportRefused) as raised:
            self.import_(data, token=token)
        self.assertIn("preview it again", str(raised.exception))
        with self.assertRaises(ImportRefused):
            self.import_(library(kit(), kit(key="BAD")))
        with self.assertRaises(ImportRefused):
            self.import_(data, token="not-a-token")
        self.assertFalse(
            KitDefinition.objects.exists(), "nothing is written when any part is refused"
        )
        self.assertEqual(Item.objects.filter(label="Gauge").count(), 0)

    def test_inactive_definitions_are_not_changed_by_import(self):
        self.import_(library(kit()))
        definition = KitDefinition.objects.get(key="jump-kit")
        set_kit_definition_active(
            definition_id=str(definition.pk), active=False, **self.context(self.admin)
        )
        self.assertTrue(any("inactive" in e for e in self.preview(library(kit(quantity=9))).errors))

    def test_only_administrators_import(self):
        data = library(kit())
        for actor in (self.crew, self.supervisor, self.officer):
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.import_(data, actor=actor)
        self.assertFalse(KitDefinition.objects.exists())

    def test_a_replayed_operation_returns_the_original_result_once(self):
        data = library(kit())
        context = self.context(self.admin)
        token = self.preview(data).base_token
        first = import_kit_library(data=data, fmt="json", base_token=token, **context)
        again = import_kit_library(data=data, fmt="json", base_token=token, **context)
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(KitDefinition.objects.count(), 1)


class ExportTests(LibraryCase):
    def test_export_round_trips_in_both_formats(self):
        self.import_(library(kit(), kit(key="other-kit", label="=Other kit")))
        for fmt in library_files.FORMATS:
            with self.subTest(fmt=fmt):
                exported = export_kit_library(str(self.org.pk), fmt)
                plan = self.preview(exported, fmt)
                self.assertEqual(plan.errors, [])
                self.assertEqual({k.action for k in plan.kits}, {UNCHANGED})
                self.assertEqual(len(plan.kits), 2)

    def test_export_can_select_kits_and_skips_inactive_ones(self):
        self.import_(library(kit(), kit(key="other-kit", label="Other kit")))
        selected = json.loads(export_kit_library(str(self.org.pk), "json", keys=["other-kit"]))
        self.assertEqual([k["key"] for k in selected["kits"]], ["other-kit"])
        definition = KitDefinition.objects.get(key="jump-kit")
        set_kit_definition_active(
            definition_id=str(definition.pk), active=False, **self.context(self.admin)
        )
        everything = json.loads(export_kit_library(str(self.org.pk), "json"))
        self.assertEqual([k["key"] for k in everything["kits"]], ["other-kit"])
