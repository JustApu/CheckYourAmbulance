"""Shared fixtures for operational-status tests (reuses the checklist organization and users)."""

from cya.checklists.tests.support import ChecklistCase
from cya.operations import commands
from cya.operations.models import VehicleStatusEvent


class StatusCase(ChecklistCase):
    @property
    def vehicle(self):
        return self.vehicles[0]

    def set_status(self, status, actor=None, vehicle=None, explanation="Synthetic reason.", **kw):
        return commands.set_vehicle_status(
            vehicle_id=str((vehicle or self.vehicle).pk),
            status=status,
            explanation=explanation,
            **kw,
            **self.context(actor or self.supervisor),
        )

    def request(self, status, actor=None, vehicle=None, reason="Synthetic request."):
        return commands.request_status_change(
            vehicle_id=str((vehicle or self.vehicle).pk),
            requested_status=status,
            reason=reason,
            **self.context(actor or self.crew),
        )

    def decide(self, request_id, decision, explanation="Synthetic decision.", actor=None, **kw):
        return commands.decide_status_request(
            request_id=request_id,
            decision=decision,
            explanation=explanation,
            **kw,
            **self.context(actor or self.supervisor),
        )

    def events(self, vehicle=None):
        return VehicleStatusEvent.objects.filter(vehicle=vehicle or self.vehicle).order_by(
            "sequence"
        )
