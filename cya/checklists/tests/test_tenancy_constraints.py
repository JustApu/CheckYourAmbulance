"""Organization-pair keys and kit slot binding (contracts section 1, SPEC 4.2)."""

from django.db import DatabaseError, transaction

from cya.accounts.models import Organization
from cya.checklists import commands
from cya.checklists.models import ChecklistTemplate, Compartment, Item, KitInstance, Vehicle
from cya.checklists.tests.support import ChecklistCase
from cya.checklists.validation import ChecklistValidationError


class TenancyConstraintTests(ChecklistCase):
    def refused(self, operation):
        with self.assertRaises(DatabaseError), transaction.atomic():
            operation()

    def test_kit_must_sit_in_a_compartment_of_its_own_vehicle(self):
        first, second = self.vehicles
        KitInstance.objects.create(
            organization=self.org, label="Synthetic kit", vehicle=first, compartment=self.cabinet
        )
        KitInstance.objects.create(organization=self.org, label="Synthetic spare kit")
        self.refused(
            lambda: KitInstance.objects.create(
                organization=self.org,
                label="Synthetic wrong",
                vehicle=second,
                compartment=self.cabinet,
            )
        )
        self.refused(
            lambda: KitInstance.objects.create(
                organization=self.org, label="Synthetic unplaced", compartment=self.cabinet
            )
        )

    def test_rows_cannot_reference_another_organizations_parents(self):
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        foreign_vehicle = Vehicle.objects.create(organization=other, display_name="Foreign")
        foreign_item = Item.objects.create(organization=other, label="Foreign item")
        foreign_template = ChecklistTemplate.objects.create(organization=other, label="Foreign")
        self.refused(
            lambda: Compartment.objects.create(
                organization=self.org,
                vehicle=foreign_vehicle,
                stable_slot_id=self.cabinet.stable_slot_id,
                revision=1,
                label="Synthetic cross-org",
                effective_at=self.now,
            )
        )
        # A command naming another organization's template or item finds nothing to act on.
        with self.assertRaises(ChecklistValidationError):
            self.draft([self.spec(foreign_item)])
        with self.assertRaises(ChecklistValidationError):
            commands.save_template_draft(
                template_id=str(foreign_template.pk), items=self.base_items(), **self.context()
            )
