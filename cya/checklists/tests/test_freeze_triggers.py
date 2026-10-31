"""MariaDB freeze triggers and constraints on checklist tables (contracts section 2, AC-25)."""

from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from cya.checklists import commands
from cya.checklists.models import (
    ChecklistItemDef,
    ChecklistPublication,
    ChecklistVersion,
    Compartment,
    StockingDeviation,
    VehicleOverrideItem,
    VehicleOverrideVersion,
    VehicleSystem,
)
from cya.checklists.tests.support import ALL_OUTCOMES, ChecklistCase
from cya.core.audit_repository import append_receipt
from cya.core.models import CommandReceipt


class FreezeTriggerTests(ChecklistCase):
    def refused(self, operation, error=DatabaseError):
        with self.assertRaises(error), transaction.atomic():
            operation()

    def setUp(self):
        super().setUp()
        self.draft_id = self.draft()
        self.published_id = self.published_version()
        self.override_id, self.entry = self.published_override()

    def published_override(self):
        draft = commands.save_override_draft(
            vehicle_id=str(self.vehicles[0].pk),
            template_version_id=self.published_id,
            entries=[{"item_id": str(self.suction.pk), "action": "REMOVE"}],
            **self.context(),
        ).body["override_version_id"]
        commands.publish_override_version(override_version_id=draft, **self.context())
        return draft, VehicleOverrideItem.objects.get(override_version_id=draft)

    def test_published_version_cannot_change_or_disappear(self):
        versions = ChecklistVersion.objects.filter(pk=self.published_id)
        self.refused(lambda: versions.update(revision=99))
        self.refused(lambda: versions.delete())
        self.assertEqual(versions.get().revision, 2)

    def test_draft_version_stays_changeable_by_schema_owner(self):
        # Proves the triggers key on publication, not on the table: drafts are not frozen.
        ChecklistVersion.objects.filter(pk=self.draft_id).update(revision=7)
        self.assertEqual(ChecklistVersion.objects.get(pk=self.draft_id).revision, 7)

    def test_published_definitions_cannot_change_disappear_or_grow(self):
        defs = ChecklistItemDef.objects.filter(checklist_version_id=self.published_id)
        self.refused(lambda: defs.update(display_order=999))
        self.refused(lambda: defs.update(instructions="edited"))
        self.refused(lambda: defs.delete())
        extra = ChecklistItemDef.objects.filter(checklist_version_id=self.draft_id).first()
        self.refused(
            lambda: ChecklistItemDef.objects.filter(pk=extra.pk).update(
                checklist_version_id=self.published_id
            )
        )
        self.refused(
            lambda: ChecklistItemDef.objects.create(
                organization=self.org,
                checklist_version_id=self.published_id,
                item=self.suction,
                display_order=1,
                kind="PRESENCE_CONDITION",
                allowed_outcomes=ALL_OUTCOMES,
                compartment=self.cabinet,
            )
        )
        self.assertEqual(defs.count(), 3)

    def test_draft_definitions_stay_changeable_by_schema_owner(self):
        draft_defs = ChecklistItemDef.objects.filter(checklist_version_id=self.draft_id)
        self.assertEqual(draft_defs.update(instructions="edited draft"), 3)

    def test_publication_is_append_only(self):
        publications = ChecklistPublication.objects.all()
        self.refused(lambda: publications.update(effective_at=timezone.now()))
        self.refused(lambda: publications.delete())

    def test_published_override_is_frozen(self):
        self.refused(
            lambda: VehicleOverrideVersion.objects.filter(pk=self.override_id).update(revision=9)
        )
        self.refused(lambda: VehicleOverrideVersion.objects.filter(pk=self.override_id).delete())
        self.refused(
            lambda: VehicleOverrideItem.objects.filter(pk=self.entry.pk).update(action="ADD")
        )
        self.refused(lambda: VehicleOverrideItem.objects.filter(pk=self.entry.pk).delete())
        self.refused(
            lambda: VehicleOverrideItem.objects.create(
                organization=self.org,
                override_version_id=self.override_id,
                item=self.masks,
                action="REMOVE",
            )
        )

    def test_override_definitions_are_frozen_with_their_override(self):
        draft = commands.save_override_draft(
            vehicle_id=str(self.vehicles[1].pk),
            template_version_id=self.published_id,
            entries=[
                {
                    "item_id": str(self.masks.pk),
                    "action": "REPLACE",
                    "definition": self.spec(self.masks, 20, kind="QUANTITY", expected_quantity=4),
                }
            ],
            **self.context(),
        ).body["override_version_id"]
        defs = ChecklistItemDef.objects.filter(override_version_id=draft)
        self.assertEqual(defs.update(expected_quantity=5), 1)  # draft: allowed
        commands.publish_override_version(override_version_id=draft, **self.context())
        self.refused(lambda: defs.update(expected_quantity=6))
        self.refused(lambda: defs.delete())

    def test_slots_and_deviation_revisions_are_immutable(self):
        self.refused(lambda: Compartment.objects.filter(pk=self.cabinet.pk).update(label="renamed"))
        self.refused(lambda: Compartment.objects.filter(pk=self.cabinet.pk).delete())
        self.refused(
            lambda: VehicleSystem.objects.filter(pk=self.system.pk).update(label="renamed")
        )
        commands.record_stocking_deviation(
            vehicle_id=str(self.vehicles[0].pk),
            item_id=str(self.masks.pk),
            template_id=str(self.template.pk),
            alternate_quantity=3,
            valid_from=timezone.now() + timedelta(hours=1),
            valid_until=timezone.now() + timedelta(hours=5),
            explanation="Synthetic shortage.",
            **self.context(),
        )
        deviations = StockingDeviation.objects.all()
        self.refused(lambda: deviations.update(alternate_quantity=1))
        self.refused(lambda: deviations.delete())

    def test_row_identity_cannot_change(self):
        self.refused(
            lambda: (
                type(self.vehicles[0])
                .objects.filter(pk=self.vehicles[0].pk)
                .update(id=str(uuid4()))
            )
        )
        self.refused(
            lambda: (
                type(self.template)
                .objects.filter(pk=self.template.pk)
                .update(organization_id=str(uuid4()))
            )
        )

    def test_structural_constraints(self):
        base = dict(
            organization=self.org,
            checklist_version_id=self.draft_id,
            display_order=1,
            allowed_outcomes=ALL_OUTCOMES,
        )
        newitem = type(self.masks).objects.create(organization=self.org, label="Synthetic extra")
        bad = {
            "bulk eligible": dict(
                kind="PRESENCE_CONDITION", compartment=self.cabinet, bulk_eligible=True
            ),
            "unknown kind": dict(kind="OTHER", compartment=self.cabinet),
            "quantity without count": dict(kind="QUANTITY", compartment=self.cabinet),
            "count on presence": dict(
                kind="PRESENCE_CONDITION", compartment=self.cabinet, expected_quantity=1
            ),
            "measurement without unit": dict(kind="MEASUREMENT", compartment=self.cabinet),
            "no placement": dict(kind="PRESENCE_CONDITION"),
            "both placements": dict(
                kind="PRESENCE_CONDITION", compartment=self.cabinet, system=self.system
            ),
            "no owner": dict(
                kind="PRESENCE_CONDITION", compartment=self.cabinet, checklist_version_id=None
            ),
            "not applicable": dict(
                kind="PRESENCE_CONDITION",
                compartment=self.cabinet,
                allowed_outcomes=["PASSED", "NOT_APPLICABLE"],
            ),
            "no passed": dict(
                kind="PRESENCE_CONDITION", compartment=self.cabinet, allowed_outcomes=["FAILED"]
            ),
        }
        for name, fields in bad.items():
            with self.subTest(name):
                self.refused(
                    lambda f=fields: ChecklistItemDef.objects.create(item=newitem, **{**base, **f}),
                    IntegrityError,
                )

    def test_publication_must_match_its_receipt_and_have_content(self):
        receipt = CommandReceipt.objects.filter(command_type="CHECKLIST_PUBLISH").latest(
            "recorded_at"
        )
        empty = ChecklistVersion.objects.create(
            organization=self.org, template=self.template, revision=50, created_at=timezone.now()
        )
        fields = dict(
            organization=self.org,
            command=receipt,
            actor_user=self.admin,
            actor_roles_at_time=["ADMINISTRATOR"],
            effective_at=receipt.recorded_at + timedelta(days=1),
        )
        self.refused(
            lambda: ChecklistPublication.objects.create(
                checklist_version=empty, recorded_at=receipt.recorded_at, **fields
            ),
            DatabaseError,
        )
        full = ChecklistVersion.objects.get(pk=self.draft_id)
        self.refused(
            lambda: ChecklistPublication.objects.create(
                checklist_version=full,
                recorded_at=receipt.recorded_at + timedelta(seconds=1),
                **fields,
            ),
            DatabaseError,
        )
        self.refused(
            lambda: ChecklistPublication.objects.create(
                checklist_version=full,
                recorded_at=receipt.recorded_at,
                **{**fields, "effective_at": receipt.recorded_at - timedelta(seconds=1)},
            ),
            IntegrityError,
        )

    def _receipt(self, command_type, actor=None):
        actor = actor or self.admin
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                actor_user=actor,
                operation_id=str(uuid4()),
                command_type=command_type,
                request_digest="0" * 64,
                response_status=200,
                response_body={},
                correlation_id=str(uuid4()),
            )

    def test_publication_command_type_guard(self):
        full = ChecklistVersion.objects.get(pk=self.draft_id)

        # Unrelated command type is refused
        unrelated = self._receipt("ROLE_CHANGE")
        self.refused(
            lambda: ChecklistPublication.objects.create(
                organization=self.org,
                checklist_version=full,
                command=unrelated,
                actor_user=self.admin,
                actor_roles_at_time=["ADMINISTRATOR"],
                recorded_at=unrelated.recorded_at,
                effective_at=unrelated.recorded_at + timedelta(days=1),
            ),
            DatabaseError,
        )

        # CHECKLIST_PUBLISH is accepted
        publish_rcpt = self._receipt("CHECKLIST_PUBLISH")
        pub = ChecklistPublication.objects.create(
            organization=self.org,
            checklist_version=full,
            command=publish_rcpt,
            actor_user=self.admin,
            actor_roles_at_time=["ADMINISTRATOR"],
            recorded_at=publish_rcpt.recorded_at,
            effective_at=publish_rcpt.recorded_at + timedelta(days=1),
        )
        self.assertIsNotNone(pub.pk)

        # OVERRIDE_PUBLISH is accepted
        draft_override = commands.save_override_draft(
            vehicle_id=str(self.vehicles[1].pk),
            template_version_id=self.published_id,
            entries=[{"item_id": str(self.suction.pk), "action": "REMOVE"}],
            **self.context(),
        ).body["override_version_id"]
        override_ver = VehicleOverrideVersion.objects.get(pk=draft_override)

        override_rcpt = self._receipt("OVERRIDE_PUBLISH")
        override_pub = ChecklistPublication.objects.create(
            organization=self.org,
            override_version=override_ver,
            command=override_rcpt,
            actor_user=self.admin,
            actor_roles_at_time=["ADMINISTRATOR"],
            recorded_at=override_rcpt.recorded_at,
            effective_at=override_rcpt.recorded_at + timedelta(days=1),
        )
        self.assertIsNotNone(override_pub.pk)

    def test_stocking_deviation_command_type_guard(self):
        now = timezone.now()

        # Unrelated command type is refused
        unrelated = self._receipt("CHECKLIST_PUBLISH")
        self.refused(
            lambda: StockingDeviation.objects.create(
                organization=self.org,
                command=unrelated,
                vehicle=self.vehicles[0],
                item=self.masks,
                approver=self.admin,
                explanation="Shortage",
                normal_quantity=10,
                alternate_quantity=5,
                valid_from=now,
                valid_until=now + timedelta(hours=2),
                recorded_at=unrelated.recorded_at,
                revision=1,
            ),
            DatabaseError,
        )

        # STOCKING_DEVIATION_RECORD is accepted
        dev_rcpt = self._receipt("STOCKING_DEVIATION_RECORD")
        dev = StockingDeviation.objects.create(
            organization=self.org,
            command=dev_rcpt,
            vehicle=self.vehicles[0],
            item=self.masks,
            approver=self.admin,
            explanation="Shortage",
            normal_quantity=10,
            alternate_quantity=5,
            valid_from=now,
            valid_until=now + timedelta(hours=2),
            recorded_at=dev_rcpt.recorded_at,
            revision=1,
        )
        self.assertIsNotNone(dev.pk)
