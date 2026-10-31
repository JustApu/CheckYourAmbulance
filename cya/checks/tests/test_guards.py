"""Database guards: append-only, uniqueness, receipt attribution, version/definition ties."""

from datetime import timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction
from django.utils import timezone

from cya.accounts.models import Organization
from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistItemDef, Compartment, KitInstance
from cya.checks.commands import correct_check
from cya.checks.models import CheckEvent, CheckRecord
from cya.checks.tests.support import CheckCase
from cya.core.audit_repository import append_receipt


class GuardTests(CheckCase):
    def setUp(self):
        super().setUp()
        self.row = CheckRecord.objects.get(pk=self.record().body["check_id"])

    def refused(self, operation):
        with self.assertRaises(DatabaseError), transaction.atomic():
            operation()

    def receipt(self, *, operation_id=None, command_type="CHECK_RECORD", actor=None):
        """A committed receipt that no check row uses yet (the command always makes both)."""
        with transaction.atomic():
            return append_receipt(
                organization=self.org,
                operation_id=operation_id or str(uuid4()),
                actor_user=actor or self.crew,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=201,
                response_body={"code": "SYNTHETIC"},
                correlation_id=str(uuid4()),
            )

    def insert(self, receipt=None, **overrides):
        """A row that would be valid, with its own receipt; overrides break one rule."""
        receipt = receipt or self.receipt()
        check_id = overrides.pop("id", str(uuid4()))
        fields = {
            "id": check_id,
            "root_check_id": check_id,
            "organization_id": self.org.pk,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": self.crew,
            "actor_roles_at_time": ["CREW"],
            "operation_id": receipt.operation_id,
            "vehicle": self.vehicle,
            "item": self.suction,
            "item_def_id": self.row.item_def_id,
            "checklist_version_id": self.version_id,
            "outcome": "PASSED",
            "purpose": "ROUTINE",
            **overrides,
        }
        return CheckRecord.objects.create(**fields)

    def definition(self, version_id, item):
        return ChecklistItemDef.objects.get(checklist_version_id=version_id, item=item).pk

    def test_a_valid_follow_up_row_is_accepted_so_the_refusals_below_are_meaningful(self):
        self.insert()
        self.assertEqual(CheckRecord.objects.count(), 2)

    def test_the_table_is_append_only(self):
        rows = CheckRecord.objects.filter(pk=self.row.pk)
        self.refused(lambda: rows.update(outcome="FAILED"))
        self.refused(lambda: rows.update(recorded_at=self.row.recorded_at))
        self.refused(rows.delete)
        self.assertEqual(CheckRecord.objects.get().outcome, "PASSED")

    def test_an_operation_id_is_unique_within_the_organization(self):
        self.refused(lambda: self.insert(receipt=self.row.command))

    def test_a_first_record_is_its_own_root(self):
        self.refused(lambda: self.insert(root_check_id=self.row.pk))

    def test_correction_event_is_append_only_and_a_predecessor_cannot_fork(self):
        result = correct_check(
            check_id=str(self.row.pk),
            expected_tip_check_id=str(self.row.pk),
            outcome="PASSED",
            **self.context(self.crew),
        )
        event = CheckEvent.objects.get(pk=result.body["event_id"])
        self.refused(lambda: CheckEvent.objects.filter(pk=event.pk).update(explanation="changed"))
        self.refused(lambda: CheckEvent.objects.filter(pk=event.pk).delete())
        duplicate_receipt = self.receipt(command_type="CHECK_CORRECTION", actor=self.crew)
        duplicate = CheckRecord(
            id=str(uuid4()),
            root_check_id=self.row.pk,
            predecessor_check_id=self.row.pk,
            organization=self.org,
            command=duplicate_receipt,
            recorded_at=duplicate_receipt.recorded_at,
            actor_user=self.crew,
            actor_roles_at_time=["CREW"],
            operation_id=duplicate_receipt.operation_id,
            vehicle=self.row.vehicle,
            item=self.row.item,
            item_def_id=self.row.item_def_id,
            checklist_version_id=self.row.checklist_version_id,
            kit_id=self.row.kit_id,
            session=self.row.session,
            outcome="PASSED",
            purpose=self.row.purpose,
        )
        self.refused(duplicate.save)

    def test_value_checks(self):
        for label, overrides in {
            "outcome": {"outcome": "MAYBE"},
            "purpose": {"purpose": "BONUS"},
            "blank explanation": {"explanation": "  "},
            "unable without a reason": {"outcome": "UNABLE_TO_INSPECT"},
            "unit without a value": {"measurement_unit": "psi"},
            "value without a unit": {"measurement_value": 1},
            "blank unit": {"measurement_value": 1, "measurement_unit": " "},
            "quantity and measurement": {
                "quantity": 1,
                "measurement_value": 1,
                "measurement_unit": "psi",
            },
            "operation id not a uuid": {"operation_id": "x"},
            "unknown role": {"actor_roles_at_time": ["ROOT"]},
            "roles not a list": {"actor_roles_at_time": "CREW"},
        }.items():
            with self.subTest(label):
                self.refused(lambda overrides=overrides: self.insert(**overrides))
        self.assertEqual(CheckRecord.objects.count(), 1)

    def test_a_row_must_match_its_command_receipt(self):
        self.refused(lambda: self.insert(recorded_at=self.row.recorded_at))  # another instant
        self.refused(lambda: self.insert(actor_user=self.supervisor))
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        self.refused(lambda: self.insert(organization_id=other.pk))

    def test_a_row_must_carry_its_receipts_operation_id_and_command_type(self):
        # A receipt from another command must not be borrowed for a check under a new id, or
        # replaying the receipt's operation id would no longer find the check.
        self.refused(lambda: self.insert(operation_id=str(uuid4())))
        self.refused(lambda: self.insert(receipt=self.receipt(command_type="STATUS_REQUEST")))
        self.insert(receipt=self.receipt(command_type="CHECK_RECORD"))  # the control

    def test_the_item_must_be_the_definitions_own(self):
        self.refused(lambda: self.insert(item=self.masks))

    def test_the_version_must_have_been_published_and_effective(self):
        draft = self.draft(self.items)
        self.refused(
            lambda: self.insert(
                checklist_version_id=draft, item_def_id=self.definition(draft, self.suction)
            )
        )
        future = self.published_version(self.items, effective_at=timezone.now() + timedelta(days=1))
        self.refused(
            lambda: self.insert(
                checklist_version_id=future, item_def_id=self.definition(future, self.suction)
            )
        )

    def test_the_definition_must_belong_to_the_named_version(self):
        newer = self.published_version(self.items)
        self.refused(lambda: self.insert(item_def_id=self.definition(newer, self.suction)))

    def test_an_override_definition_counts_only_for_its_own_vehicle(self):
        draft = checklist_commands.save_override_draft(
            vehicle_id=str(self.vehicle.pk),
            template_version_id=self.version_id,
            entries=[
                {
                    "item_id": str(self.masks.pk),
                    "action": "REPLACE",
                    "definition": self.spec(self.masks, 20, kind="QUANTITY", expected_quantity=4),
                }
            ],
            **self.context(),
        ).body["override_version_id"]
        override_def = ChecklistItemDef.objects.get(override_version_id=draft).pk
        # Not published yet: the database refuses even for the right vehicle.
        self.refused(lambda: self.insert(item=self.masks, item_def_id=override_def, quantity=4))
        checklist_commands.publish_override_version(override_version_id=draft, **self.context())
        self.insert(item=self.masks, item_def_id=override_def, quantity=4)
        self.refused(
            lambda: self.insert(
                vehicle=self.vehicles[1], item=self.masks, item_def_id=override_def, quantity=4
            )
        )

    def test_a_kit_must_be_the_vehicles_own_kit_in_the_definitions_slot(self):
        from cya.checklists.models import Item

        kit_item = Item.objects.create(organization=self.org, label="Synthetic kit contents")
        own = KitInstance.objects.create(
            organization=self.org, label="Own", vehicle=self.vehicle, compartment=self.cabinet
        )
        self.version_id = self.published_version(
            [*self.items, self.spec(kit_item, 80, kit_id=str(own.pk))]
        )
        fresh = {
            "item": kit_item,
            "item_def_id": self.definition(self.version_id, kit_item),
            "checklist_version_id": self.version_id,
        }
        drawer = self.make_slot(Compartment, "drawer")
        wrong_slot = KitInstance.objects.create(
            organization=self.org, label="Drawer", vehicle=self.vehicle, compartment=drawer
        )
        foreign = KitInstance.objects.create(
            organization=self.org,
            label="Foreign",
            vehicle=self.vehicles[1],
            compartment=Compartment.objects.get(
                vehicle=self.vehicles[1], stable_slot_id=self.cabinet.stable_slot_id
            ),
        )
        unplaced = KitInstance.objects.create(organization=self.org, label="Unplaced")
        for label, kit in {
            "other vehicle": foreign,
            "other slot": wrong_slot,
            "no compartment": unplaced,
        }.items():
            with self.subTest(label):
                self.refused(lambda kit=kit: self.insert(kit=kit, **fresh))
        # An item that is not in a kit takes no kit at all.
        plain = {
            **fresh,
            "item": self.suction,
            "item_def_id": self.definition(self.version_id, self.suction),
        }
        self.refused(lambda: self.insert(kit=own, **plain))
        self.refused(lambda: self.insert(**fresh))  # a kit item must name its kit
        self.insert(kit=own, **fresh)
