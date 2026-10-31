"""Fixtures for kit tests: a clock-controlled organization with two vehicles and a kit slot."""

from cya.checklists.models import Compartment, Item
from cya.kits.commands import add_kit_instance, record_kit_assignment
from cya.obligations.tests.support import ObligationCase


class KitCase(ObligationCase):
    """Vehicle A and B each have a "cabinet" compartment in the same slot; items are synthetic."""

    def setUp(self):
        super().setUp()
        self.vehicle_b = self.vehicles[1]
        self.cabinet_b = Compartment.objects.get(
            vehicle=self.vehicle_b, stable_slot_id=self.cabinet.stable_slot_id
        )
        self.kit_item = Item.objects.create(organization=self.org, label="Synthetic kit contents")
        self.officer = self.make_user("officer")
        self.grant(self.officer, "SUPPLY_OFFICER")

    @property
    def slot(self):
        return str(self.cabinet.stable_slot_id)

    def compartment_of(self, vehicle):
        return self.cabinet if vehicle.pk == self.vehicle.pk else self.cabinet_b

    def add_kit(self, label="Kit", vehicle=None, actor=None, **context):
        vehicle = vehicle or self.vehicle
        return add_kit_instance(
            label=label,
            vehicle_id=str(vehicle.pk),
            compartment_id=str(self.compartment_of(vehicle).pk),
            **(context or self.context(actor or self.supervisor)),
        )

    def move(self, kit_id, vehicle, actor=None, **context):
        return record_kit_assignment(
            kit_id=kit_id,
            active=True,
            vehicle_id=str(vehicle.pk),
            compartment_id=str(self.compartment_of(vehicle).pk),
            **(context or self.context(actor or self.supervisor)),
        )

    def deactivate(self, kit_id, actor=None, **context):
        return record_kit_assignment(
            kit_id=kit_id, active=False, **(context or self.context(actor or self.supervisor))
        )
