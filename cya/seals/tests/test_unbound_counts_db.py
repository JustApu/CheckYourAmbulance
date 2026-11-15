"""Unbound kit-list lines are counted with ordinary, attributed standalone checks."""

from cya.checks.commands import CheckError, correct_check, record_check
from cya.checks.models import CheckObligationLink, CheckRecord
from cya.deficiencies.models import DeficiencyCategory
from cya.deficiencies.severity import ensure_default_severities
from cya.kits.commands import add_kit_instance
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.kits.kit_lists import publish_vehicle_kit_list, save_vehicle_kit_list_draft
from cya.kits.models import KitDefinition, VehicleKitRequirement
from cya.kits.sealable import designate_compartment_sealable
from cya.kits.tests.support import KitCase
from cya.seals.commands import SealError, start_inspection, verify_seal
from cya.seals.models import SealInspectionKitCount
from cya.seals.reseal import reseal


class UnboundKitCountTests(KitCase):
    def setUp(self):
        super().setUp()
        self.definition = create_kit_definition(
            key="sample-kit", label="Synthetic kit", **self.context(self.supervisor)
        ).body["definition_id"]
        draft = save_kit_definition_draft(
            definition_id=self.definition,
            contents=[
                {
                    "item_id": str(self.kit_item.pk),
                    "display_order": 1,
                    "kind": "QUANTITY",
                    "expected_quantity": 2,
                    "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
                }
            ],
            **self.context(self.supervisor),
        ).body["version_id"]
        publish_kit_definition_version(version_id=draft, **self.context(self.supervisor))
        self.kit = add_kit_instance(
            label="Synthetic kit",
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            definition_id=self.definition,
            **self.context(self.supervisor),
        ).body["kit_id"]
        version = save_vehicle_kit_list_draft(
            vehicle_id=str(self.vehicle.pk),
            lines=[
                {
                    "definition_id": self.definition,
                    "compartment_id": str(self.cabinet.pk),
                    "required_count": 1,
                    "display_order": 1,
                }
            ],
            **self.context(self.supervisor),
        ).body["version_id"]
        publish_vehicle_kit_list(version_id=version, **self.context(self.supervisor))
        self.line = VehicleKitRequirement.objects.get(version_id=version)
        self.presence_item = str(KitDefinition.objects.get(pk=self.definition).presence_item_id)
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=True,
            **self.context(self.supervisor),
        )
        self.start = self.context(self.crew)
        start_inspection(vehicle_id=str(self.vehicle.pk), slot_id=self.slot, **self.start)

    def check(self, **overrides):
        args = {
            "vehicle_id": str(self.vehicle.pk),
            "item_id": self.presence_item,
            "checklist_version_id": None,
            "kit_requirement_id": str(self.line.pk),
            "inspection_start_operation_id": self.start["operation_id"],
            "quantity": 1,
        }
        return record_check(**(args | overrides), **self.context(self.crew))

    def test_fresh_count_is_attributed_and_does_not_invent_an_obligation(self):
        result = self.check()
        check = CheckRecord.objects.get(pk=result.body["check_id"])
        self.assertIsNone(check.checklist_version_id)
        self.assertEqual(
            (check.outcome, check.quantity, check.actor_user_id), ("PASSED", 1, self.crew.pk)
        )
        self.assertFalse(CheckObligationLink.objects.filter(check_record=check).exists())
        corrected = correct_check(
            check_id=str(check.pk),
            expected_tip_check_id=str(check.pk),
            outcome="PASSED",
            quantity=1,
            **self.context(self.crew),
        )
        self.assertEqual(
            CheckRecord.objects.get(pk=corrected.body["check_id"]).checklist_version_id, None
        )

    def test_cabinet_reseal_references_the_effective_line_check(self):
        from importlib import import_module

        from django.apps import apps

        check = self.check()
        result = reseal(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            marking="cabinet-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=[
                {
                    "item_id": str(self.kit_item.pk),
                    "kit_id": self.kit,
                    "outcome": "PASSED",
                    "count": 2,
                    "expiration": None,
                    "measurement": None,
                }
            ],
            start_operation_id=self.start["operation_id"],
            kit_count_checks=[{"line_id": str(self.line.pk), "check_id": check.body["check_id"]}],
            **self.context(self.crew),
        )
        saved = SealInspectionKitCount.objects.get(inspection_id=result.body["inspection_id"])
        self.assertEqual(str(saved.check_record_id), check.body["check_id"])
        self.assertEqual(saved.count, 1)
        guard = import_module("cya.seals.migrations.0005_inspection_count_checks")
        with self.assertRaises(RuntimeError):
            guard.forbid_reverse_with_count_evidence(apps, None)

    def test_an_open_deficiency_on_unbound_kit_content_blocks_verification(self):
        from datetime import timedelta

        from cya.accounts.fields import new_id

        effective_at = self.clock_start - timedelta(days=1)
        ensure_default_severities(self.org.pk, effective_at)
        category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=new_id(),
            revision=1,
            label="Synthetic supply",
            supply_related=True,
            effective_at=effective_at,
        )
        check = self.check()
        result = reseal(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            marking="cabinet-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=[
                {
                    "item_id": str(self.kit_item.pk),
                    "kit_id": self.kit,
                    "outcome": None,
                    "count": 1,
                    "expiration": None,
                    "measurement": None,
                    "deficiency": {
                        "create": {
                            "category_id": str(category.stable_category_id),
                            "severity": "MEDIUM",
                            "summary": "Synthetic shortfall",
                            "description": "One item is missing.",
                        }
                    },
                }
            ],
            start_operation_id=self.start["operation_id"],
            kit_count_checks=[{"line_id": str(self.line.pk), "check_id": check.body["check_id"]}],
            **self.context(self.crew),
        )
        with self.assertRaises(SealError):
            verify_seal(
                vehicle_id=str(self.vehicle.pk),
                slot_id=self.slot,
                expected_tip_id=result.body["event_id"],
                marking="cabinet-id",
                attested=True,
                **self.context(self.crew),
            )

    def test_presence_deficiency_remains_a_blocker_after_kit_list_republication(self):
        from datetime import timedelta

        from cya.accounts.fields import new_id

        effective_at = self.clock_start - timedelta(days=1)
        ensure_default_severities(self.org.pk, effective_at)
        category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=new_id(),
            revision=1,
            label="Synthetic supply",
            supply_related=True,
            effective_at=effective_at,
        )
        self.check(
            quantity=0,
            deficiency={
                "create": {
                    "category_id": str(category.stable_category_id),
                    "severity": "MEDIUM",
                    "summary": "Synthetic missing kit",
                    "description": "The kit was missing.",
                }
            },
        )
        version = save_vehicle_kit_list_draft(
            vehicle_id=str(self.vehicle.pk),
            lines=[
                {
                    "definition_id": self.definition,
                    "compartment_id": str(self.cabinet.pk),
                    "required_count": 1,
                    "display_order": 1,
                }
            ],
            **self.context(self.supervisor),
        ).body["version_id"]
        publish_vehicle_kit_list(version_id=version, **self.context(self.supervisor))
        self.line = VehicleKitRequirement.objects.get(version_id=version)
        check = self.check()
        result = reseal(
            vehicle_id=str(self.vehicle.pk),
            slot_id=self.slot,
            marking="cabinet-id",
            inspected_attestation=True,
            written_attestation=True,
            observations=[
                {
                    "item_id": str(self.kit_item.pk),
                    "kit_id": self.kit,
                    "outcome": "PASSED",
                    "count": 2,
                    "expiration": None,
                    "measurement": None,
                }
            ],
            start_operation_id=self.start["operation_id"],
            kit_count_checks=[{"line_id": str(self.line.pk), "check_id": check.body["check_id"]}],
            **self.context(self.crew),
        )
        with self.assertRaises(SealError):
            verify_seal(
                vehicle_id=str(self.vehicle.pk),
                slot_id=self.slot,
                expected_tip_id=result.body["event_id"],
                marking="cabinet-id",
                attested=True,
                **self.context(self.crew),
            )

    def test_missing_or_other_actors_start_is_refused(self):
        with self.assertRaises(CheckError):
            self.check(inspection_start_operation_id=None)
        other = self.context(self.supervisor)
        start_inspection(vehicle_id=str(self.vehicle.pk), slot_id=self.slot, **other)
        with self.assertRaises(CheckError):
            self.check(inspection_start_operation_id=other["operation_id"])

    def test_count_is_refused_when_cabinet_is_no_longer_sealable(self):
        designate_compartment_sealable(
            vehicle_id=str(self.vehicle.pk),
            compartment_id=str(self.cabinet.pk),
            sealable=False,
            **self.context(self.supervisor),
        )
        with self.assertRaises(CheckError):
            self.check()
        self.assertFalse(CheckRecord.objects.filter(item_id=self.presence_item).exists())

    def test_superseded_count_cannot_certify_a_reseal(self):
        check = self.check()
        correct_check(
            check_id=check.body["check_id"],
            expected_tip_check_id=check.body["check_id"],
            outcome="PASSED",
            quantity=1,
            **self.context(self.crew),
        )
        with self.assertRaises(ValueError):
            reseal(
                vehicle_id=str(self.vehicle.pk),
                slot_id=self.slot,
                marking="cabinet-id",
                inspected_attestation=True,
                written_attestation=True,
                observations=[
                    {
                        "item_id": str(self.kit_item.pk),
                        "kit_id": self.kit,
                        "outcome": "PASSED",
                        "count": 2,
                        "expiration": None,
                        "measurement": None,
                    }
                ],
                start_operation_id=self.start["operation_id"],
                kit_count_checks=[
                    {"line_id": str(self.line.pk), "check_id": check.body["check_id"]}
                ],
                **self.context(self.crew),
            )
        self.assertFalse(SealInspectionKitCount.objects.exists())
