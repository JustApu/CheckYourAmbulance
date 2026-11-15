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
from cya.kits.models import ChecklistKitPlacement
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
