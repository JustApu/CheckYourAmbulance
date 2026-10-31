"""Shared fixtures for check-record tests (reuses the checklist organization and users)."""

from cya.checklists.models import Item
from cya.checklists.tests.support import ChecklistCase
from cya.checks.commands import record_check


class CheckCase(ChecklistCase):
    """A published checklist with one item of every kind the command distinguishes."""

    def setUp(self):
        super().setUp()
        self.gauge = Item.objects.create(organization=self.org, label="Synthetic gauge")
        self.special = Item.objects.create(organization=self.org, label="Synthetic seal")
        self.odometer = Item.objects.create(organization=self.org, label="Synthetic odometer")
        self.limited = Item.objects.create(organization=self.org, label="Synthetic limited")
        self.stray = Item.objects.create(organization=self.org, label="Synthetic stray")
        self.items = [
            *self.base_items(),
            self.spec(self.gauge, 40, kind="MEASUREMENT", unit="psi"),
            self.spec(self.special, 50, kind="SPECIAL_OBSERVATION"),
            self.spec(
                self.odometer,
                60,
                kind="VEHICLE_READING",
                unit="mi",
                compartment_id=None,
                system_id=str(self.system.pk),
            ),
            self.spec(self.limited, 70, allowed_outcomes=["PASSED", "FAILED"]),
        ]
        self.version_id = self.published_version(self.items)

    @property
    def vehicle(self):
        return self.vehicles[0]

    def record(self, item=None, actor=None, vehicle=None, operation_id=None, **fields):
        args = {
            "vehicle_id": str((vehicle or self.vehicle).pk),
            "item_id": str((item or self.suction).pk),
            "checklist_version_id": self.version_id,
            "outcome": "PASSED",
            "purpose": "ROUTINE",
            **fields,
        }
        context = self.context(actor or self.crew)
        if operation_id is not None:
            context["operation_id"] = operation_id
        return record_check(**args, **context)
