"""Kit definitions: versioned contents, publication, duplication (SPEC 4.6, issue #129)."""

from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction
from django.utils import timezone

from cya.checklists.models import Item
from cya.core.audit_commands import CommandDenied
from cya.core.audit_repository import append_receipt
from cya.kits.commands import KitError, add_kit_instance
from cya.kits.definitions import (
    KitDefinitionError,
    create_kit_definition,
    duplicate_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
    set_kit_definition_active,
    version_effective_at,
)
from cya.kits.models import (
    KitDefinition,
    KitDefinitionItem,
    KitDefinitionPublication,
    KitDefinitionVersion,
    KitInstanceDefinition,
)
from cya.kits.tests.support import KitCase


class DefinitionCase(KitCase):
    def setUp(self):
        super().setUp()
        self.masks = Item.objects.create(organization=self.org, label="Synthetic masks")
        self.gauge = Item.objects.create(organization=self.org, label="Synthetic gauge")

    def contents(self, quantity=6):
        return [
            {
                "item_id": str(self.masks.pk),
                "display_order": 1,
                "kind": "QUANTITY",
                "expected_quantity": quantity,
                "allowed_outcomes": ["PASSED", "FAILED"],
                "expiration_tracked": True,
            },
            {
                "item_id": str(self.gauge.pk),
                "display_order": 2,
                "kind": "MEASUREMENT",
                "unit": "psi",
                "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
            },
        ]

    def create(self, key="synthetic-jump-kit", actor=None, **over):
        return create_kit_definition(
            key=key,
            label="Synthetic jump kit",
            **{**self.context(actor or self.supervisor), **over},
        )

    def draft(self, definition_id, contents=None, actor=None):
        return save_kit_definition_draft(
            definition_id=definition_id,
            contents=self.contents() if contents is None else contents,
            **self.context(actor or self.supervisor),
        )

    def publish(self, version_id, effective_at=None, actor=None, **over):
        return publish_kit_definition_version(
            version_id=version_id,
            effective_at=effective_at,
            **{**self.context(actor or self.supervisor), **over},
        )

    def published_definition(self):
        definition_id = self.create().body["definition_id"]
        version_id = self.draft(definition_id).body["version_id"]
        self.publish(version_id)
        return definition_id, version_id


class CreateAndDraftTests(DefinitionCase):
    def test_a_definition_gets_numbered_draft_revisions_with_their_contents(self):
        created = self.create()
        self.assertEqual(created.body["code"], "KIT_DEFINITION_CREATED")
        definition_id = created.body["definition_id"]
        first = self.draft(definition_id)
        second = self.draft(definition_id, self.contents(quantity=8))
        self.assertEqual((first.body["revision"], second.body["revision"]), (1, 2))
        rows = KitDefinitionItem.objects.filter(version_id=second.body["version_id"])
        masks = rows.get(item=self.masks)
        self.assertEqual(
            (masks.kind, masks.expected_quantity, masks.expiration_tracked), ("QUANTITY", 8, True)
        )
        self.assertEqual(rows.get(item=self.gauge).unit, "psi")
        self.assertEqual(
            KitDefinitionItem.objects.filter(version_id=first.body["version_id"])
            .get(item=self.masks)
            .expected_quantity,
            6,
        )

    def test_keys_are_validated_and_unique(self):
        for key in ("", "Upper", "-lead", "has space", "x" * 101, None):
            with self.subTest(key=key), self.assertRaises(KitDefinitionError):
                self.create(key=key)
        self.create()
        with self.assertRaises(KitDefinitionError):
            self.create()
        self.assertEqual(KitDefinition.objects.filter(organization=self.org).count(), 1)

    def test_malformed_contents_are_refused_with_their_position(self):
        definition_id = self.create().body["definition_id"]
        good = self.contents()[0]
        inactive = Item.objects.create(organization=self.org, label="Retired", active=False)
        cases = {
            "empty": [],
            "not a list": {"item_id": good["item_id"]},
            "vehicle reading in a kit": [
                {**good, "kind": "VEHICLE_READING", "expected_quantity": None, "unit": "mi"}
            ],
            "quantity missing": [{**good, "expected_quantity": None}],
            "unit on a quantity": [{**good, "unit": "each"}],
            "measurement without unit": [{**self.contents()[1], "unit": None}],
            "unknown field": [{**good, "kit_id": str(uuid4())}],
            "missing field": [{k: v for k, v in good.items() if k != "kind"}],
            "outcomes without PASSED": [{**good, "allowed_outcomes": ["FAILED"]}],
            "expiration not bool": [{**good, "expiration_tracked": "yes"}],
            "bad item id": [{**good, "item_id": "not-a-uuid"}],
            "unknown item": [{**good, "item_id": str(uuid4())}],
            "inactive item": [{**good, "item_id": str(inactive.pk)}],
            "duplicate item": [good, {**good, "display_order": 5}],
        }
        for name, contents in cases.items():
            with self.subTest(name), self.assertRaises(KitDefinitionError) as raised:
                self.draft(definition_id, contents)
            if isinstance(contents, list) and contents and name != "duplicate item":
                self.assertIn("contents[0]", str(raised.exception))
        self.assertFalse(KitDefinitionVersion.objects.filter(definition_id=definition_id).exists())

    def test_only_supervisors_and_administrators_manage_definitions(self):
        for actor in (self.crew, self.officer):
            with self.subTest(actor=actor.pk), self.assertRaises(CommandDenied):
                self.create(actor=actor)
        definition_id = self.create(actor=self.admin).body["definition_id"]
        with self.assertRaises(CommandDenied):
            self.draft(definition_id, actor=self.crew)
        self.draft(definition_id, actor=self.admin)

    def test_a_replayed_operation_returns_the_original_result_once(self):
        definition_id = self.create().body["definition_id"]
        context = self.context(self.supervisor)
        first = save_kit_definition_draft(
            definition_id=definition_id, contents=self.contents(), **context
        )
        again = save_kit_definition_draft(
            definition_id=definition_id, contents=self.contents(), **context
        )
        self.assertTrue(again.replayed)
        self.assertEqual(again.body, first.body)
        self.assertEqual(
            KitDefinitionVersion.objects.filter(definition_id=definition_id).count(), 1
        )


class PublishTests(DefinitionCase):
    def test_publication_freezes_a_version_and_answers_as_of(self):
        definition_id = self.create().body["definition_id"]
        v1 = self.draft(definition_id).body["version_id"]
        before = timezone.now() - timedelta(seconds=1)
        self.assertEqual(self.publish(v1).body["code"], "KIT_DEFINITION_PUBLISHED")
        with self.assertRaises(KitDefinitionError):
            self.publish(v1)  # already published
        v2 = self.draft(definition_id, self.contents(quantity=9)).body["version_id"]
        later = timezone.now() + timedelta(days=2)
        self.publish(v2, effective_at=later)
        self.assertIsNone(version_effective_at(str(self.org.pk), definition_id, before))
        self.assertEqual(
            str(version_effective_at(str(self.org.pk), definition_id, timezone.now()).pk), v1
        )
        self.assertEqual(str(version_effective_at(str(self.org.pk), definition_id, later).pk), v2)

    def test_backdated_out_of_order_and_naive_instants_are_refused(self):
        definition_id = self.create().body["definition_id"]
        v1 = self.draft(definition_id).body["version_id"]
        with self.assertRaises(KitDefinitionError):
            self.publish(v1, effective_at=timezone.now() - timedelta(hours=1))
        with self.assertRaises(KitDefinitionError):
            self.publish(v1, effective_at=timezone.now().replace(tzinfo=None))
        self.publish(v1, effective_at=timezone.now() + timedelta(days=3))
        v2 = self.draft(definition_id).body["version_id"]
        with self.assertRaises(KitDefinitionError):  # not later than the latest publication
            self.publish(v2, effective_at=timezone.now() + timedelta(days=1))
        self.assertEqual(KitDefinitionPublication.objects.count(), 1)

    def test_inactive_definitions_cannot_be_drafted_or_published(self):
        definition_id = self.create().body["definition_id"]
        version_id = self.draft(definition_id).body["version_id"]
        set_kit_definition_active(
            definition_id=definition_id, active=False, **self.context(self.supervisor)
        )
        with self.assertRaises(KitDefinitionError):
            self.draft(definition_id)
        with self.assertRaises(KitDefinitionError):
            self.publish(version_id)
        with self.assertRaises(KitDefinitionError):  # already inactive
            set_kit_definition_active(
                definition_id=definition_id, active=False, **self.context(self.supervisor)
            )
        set_kit_definition_active(
            definition_id=definition_id, active=True, **self.context(self.admin)
        )
        self.publish(version_id)


class DuplicateTests(DefinitionCase):
    def test_duplicate_copies_one_versions_contents_without_history(self):
        definition_id, version_id = self.published_definition()
        result = duplicate_kit_definition(
            source_version_id=version_id,
            key="synthetic-jump-kit-b",
            label="Synthetic jump kit B",
            **self.context(self.supervisor),
        )
        copy = KitDefinition.objects.get(pk=result.body["definition_id"])
        self.assertEqual((copy.key, copy.active), ("synthetic-jump-kit-b", True))
        new_version = KitDefinitionVersion.objects.get(definition=copy)
        self.assertEqual(new_version.revision, 1)
        self.assertFalse(KitDefinitionPublication.objects.filter(version=new_version).exists())
        fields = (
            "item_id",
            "kind",
            "expected_quantity",
            "unit",
            "allowed_outcomes",
            "expiration_tracked",
        )
        original = sorted(
            KitDefinitionItem.objects.filter(version_id=version_id).values_list(*fields)
        )
        copied = sorted(KitDefinitionItem.objects.filter(version=new_version).values_list(*fields))
        self.assertEqual(copied, original)
        with self.assertRaises(KitDefinitionError):  # key already used
            duplicate_kit_definition(
                source_version_id=version_id,
                key="synthetic-jump-kit-b",
                label="Again",
                **self.context(self.supervisor),
            )


class KitInstanceDefinitionTests(DefinitionCase):
    def add(self, definition_id, **context):
        return add_kit_instance(
            label="Jump kit 1",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            definition_id=definition_id,
            **(context or self.context(self.supervisor)),
        )

    def test_a_new_kit_names_its_definition_for_life(self):
        definition_id, _ = self.published_definition()
        first, second = self.add(definition_id), self.add(definition_id)
        rows = KitInstanceDefinition.objects.filter(definition_id=definition_id)
        self.assertEqual(
            sorted(str(r.kit_id) for r in rows),
            sorted([first.body["kit_id"], second.body["kit_id"]]),
        )
        self.assertEqual(rows.first().actor_user_id, self.supervisor.pk)
        legacy = self.add_kit("Legacy kit")  # no definition: instances made before KDEF (KDEF-8)
        self.assertFalse(
            KitInstanceDefinition.objects.filter(kit_id=legacy.body["kit_id"]).exists()
        )

    def test_inactive_unknown_and_malformed_definitions_are_refused(self):
        definition_id, _ = self.published_definition()
        set_kit_definition_active(
            definition_id=definition_id, active=False, **self.context(self.supervisor)
        )
        with self.assertRaises(KitError):
            self.add(definition_id)
        with self.assertRaises(KitError):
            self.add(str(uuid4()))
        with self.assertRaises(ValueError):
            self.add("not-a-uuid")
        self.assertFalse(KitInstanceDefinition.objects.exists())


class GuardTests(DefinitionCase):
    """The database refuses what the commands would never write."""

    def refused(self, operation):
        with self.assertRaises(DatabaseError), transaction.atomic():
            operation()

    def receipt(self, command_type):
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=str(uuid4()),
                actor_user=self.supervisor,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=200,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def test_published_versions_and_contents_are_frozen(self):
        definition_id, version_id = self.published_definition()
        published = KitDefinitionItem.objects.filter(version_id=version_id)
        self.refused(lambda: published.update(expected_quantity=1))
        self.refused(lambda: published.delete())
        self.refused(lambda: KitDefinitionVersion.objects.filter(pk=version_id).update(revision=7))
        self.refused(lambda: KitDefinitionVersion.objects.filter(pk=version_id).delete())
        self.refused(
            lambda: KitDefinitionItem.objects.create(
                organization=self.org,
                version_id=version_id,
                item=self.kit_item,
                display_order=9,
                kind="PRESENCE_CONDITION",
                allowed_outcomes=["PASSED"],
            )
        )
        self.refused(
            lambda: KitDefinitionPublication.objects.filter(version_id=version_id).delete()
        )
        self.refused(lambda: KitDefinition.objects.filter(pk=definition_id).update(key="renamed"))
        KitDefinition.objects.filter(pk=definition_id).update(
            label="Relabelled"
        )  # label may change

    def test_publications_and_instance_definitions_must_match_their_receipts(self):
        definition_id = self.create().body["definition_id"]
        version_id = self.draft(definition_id).body["version_id"]
        empty = KitDefinitionVersion.objects.create(
            organization=self.org,
            definition_id=definition_id,
            revision=9,
            created_at=timezone.now(),
        )

        def publication(receipt, version):
            return KitDefinitionPublication.objects.create(
                organization=self.org,
                version_id=version,
                effective_at=receipt.recorded_at,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.supervisor,
                actor_roles_at_time=["SUPERVISOR"],
            )

        self.refused(lambda: publication(self.receipt("KIT_ADD"), version_id))
        self.refused(lambda: publication(self.receipt("KIT_DEFINITION_PUBLISH"), str(empty.pk)))
        publication(self.receipt("KIT_DEFINITION_PUBLISH"), version_id)  # the valid row is accepted
        kit_id = self.add_kit().body["kit_id"]

        def instance_definition(receipt):
            return KitInstanceDefinition.objects.create(
                organization=self.org,
                kit_id=kit_id,
                definition_id=definition_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.supervisor,
                actor_roles_at_time=["SUPERVISOR"],
            )

        self.refused(lambda: instance_definition(self.receipt("KIT_ASSIGNMENT_RECORD")))
        KitDefinition.objects.filter(pk=definition_id).update(active=False)
        self.refused(lambda: instance_definition(self.receipt("KIT_ADD")))
