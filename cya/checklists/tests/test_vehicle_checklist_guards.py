"""Database guards for vehicle-owned checklists (SPEC 5.0.0 CHK-1, CHK-8, CHK-9, 18.2).

Each refusal is made directly against the tables, bypassing the commands, to show the database
itself refuses it (migrations cya_checklists 0009 and cya_obligations 0017).
"""

from django.db import DatabaseError, IntegrityError, transaction

from cya.accounts.fields import new_id
from cya.checklists.models import (
    ChecklistCutover,
    ChecklistItemDef,
    ChecklistTemplate,
    ChecklistVersion,
    VehicleOverrideVersion,
)
from cya.checklists.tests.support import ALL_OUTCOMES, ChecklistCase
from cya.obligations.models import RequirementBinding


class VehicleChecklistGuardTests(ChecklistCase):
    def refused(self, operation, message=None, error=DatabaseError):
        if message is None:
            with self.assertRaises(error), transaction.atomic():
                operation()
        else:
            with self.assertRaisesRegex(error, message), transaction.atomic():
                operation()

    def own_checklist(self):
        return ChecklistTemplate.objects.create(
            organization=self.org, label="Own", vehicle=self.vehicles[0], precedence=1
        )

    def item_def(self, version, **placement):
        return ChecklistItemDef.objects.create(
            organization=self.org,
            checklist_version=version,
            item=self.suction,
            display_order=1,
            kind="PRESENCE_CONDITION",
            allowed_outcomes=ALL_OUTCOMES,
            **placement,
        )

    def version(self, template):
        return ChecklistVersion.objects.create(
            organization=self.org, template=template, revision=1, created_at=self.now
        )

    def test_a_vehicle_checklist_exists_only_after_the_cutover(self):
        self.refused(self.own_checklist, "start at the cutover")
        self.record_cutover()
        self.assertEqual(self.own_checklist().precedence, 1)

    def test_no_legacy_template_or_version_is_written_after_the_cutover(self):
        self.record_cutover()
        self.refused(
            lambda: ChecklistTemplate.objects.create(organization=self.org, label="Late"),
            "read-only",
        )
        self.refused(lambda: self.version(self.template), "read-only")
        self.refused(
            lambda: ChecklistTemplate.objects.filter(pk=self.template.pk).update(active=False),
            "read-only",
        )

    def test_owner_and_precedence_never_change(self):
        self.record_cutover()
        own = self.own_checklist()
        rows = ChecklistTemplate.objects.filter(pk=own.pk)
        self.refused(lambda: rows.update(precedence=2), "never change")
        self.refused(lambda: rows.update(vehicle=self.vehicles[1]), "never change")
        rows.update(label="Renamed")  # the label is ordinary mutable data
        self.assertEqual(rows.get().label, "Renamed")

    def test_owner_needs_precedence_and_no_key(self):
        self.record_cutover()
        self.refused(
            lambda: ChecklistTemplate.objects.create(
                organization=self.org, label="No order", vehicle=self.vehicles[0]
            ),
            error=IntegrityError,
        )
        self.refused(
            lambda: ChecklistTemplate.objects.create(
                organization=self.org,
                label="Keyed",
                vehicle=self.vehicles[0],
                precedence=1,
                key="keyed",
            ),
            error=IntegrityError,
        )
        self.own_checklist()
        self.refused(self.own_checklist, error=IntegrityError)  # same precedence, same vehicle

    def test_item_rows_stay_on_the_owning_vehicle_or_are_not_placed(self):
        self.record_cutover()
        version = self.version(self.own_checklist())
        other_cabinet = self.own_row(self.cabinet, self.vehicles[1])
        other_system = self.own_row(self.system, self.vehicles[1])
        self.refused(lambda: self.item_def(version, compartment=other_cabinet), "its own vehicle")
        self.refused(lambda: self.item_def(version, system=other_system), "its own vehicle")
        self.assertIsNone(self.item_def(version).compartment_id)  # Not placed (CHK-9)

    def test_a_legacy_item_row_keeps_its_placement(self):
        version = self.version(self.template)
        self.refused(lambda: self.item_def(version), "may be not placed")
        self.assertEqual(self.item_def(version, compartment=self.cabinet).compartment, self.cabinet)

    def test_no_override_overlays_a_vehicle_checklist(self):
        self.record_cutover()
        version = self.version(self.own_checklist())
        self.refused(
            lambda: VehicleOverrideVersion.objects.create(
                organization=self.org,
                vehicle=self.vehicles[0],
                template_version=version,
                revision=1,
                created_at=self.now,
            ),
            "read-only",
        )

    def test_the_cutover_is_append_only_and_one_per_organization(self):
        self.record_cutover()
        rows = ChecklistCutover.objects.filter(organization=self.org)
        self.refused(lambda: rows.update(recorded_at=self.now), "append-only")
        self.refused(lambda: rows.delete(), "append-only")
        with self.assertRaises(DatabaseError):
            self.record_cutover()  # a command owns its transaction, so no atomic() here

    def bind_row(self, template, vehicle):
        from uuid import uuid4

        from cya.core.audit_repository import append_receipt
        from cya.schedules.models import ScheduleRule

        self.assertTrue(ScheduleRule.objects.filter(organization=self.org).exists())
        with transaction.atomic():
            receipt = append_receipt(
                organization=self.org,
                actor_user=self.admin,
                operation_id=str(uuid4()),
                command_type="REQUIREMENT_BIND",
                request_digest="0" * 64,
                response_status=200,
                response_body={},
                correlation_id=str(uuid4()),
            )
            return RequirementBinding.objects.create(
                id=new_id(),
                organization=self.org,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=self.admin,
                vehicle=vehicle,
                template=template,
                slot=self.slot,
                revision=1,
                action="BIND",
                effective_at=receipt.recorded_at,
            )

    def publish_schedule(self):
        from datetime import time

        from cya.checklists.tests.support import ids
        from cya.schedules.commands import publish_rule
        from cya.schedules.engine import Rule

        self.slot = "test-0600"
        publish_rule(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            slot=self.slot,
            rule=Rule(zone="UTC", wall_time=time(6), effective_at=self.now, recurrence="DAILY"),
            **ids(),
        )

    def test_assignments_follow_ownership_and_the_cutover(self):
        self.publish_schedule()
        self.bind_row(self.template, self.vehicles[0])  # legacy, before the cutover
        self.record_cutover()
        own = self.own_checklist()
        self.refused(lambda: self.bind_row(own, self.vehicles[1]), "another vehicle checklist")
        self.refused(lambda: self.bind_row(self.template, self.vehicles[1]), "read-only")
        self.assertEqual(self.bind_row(own, self.vehicles[0]).vehicle, self.vehicles[0])
