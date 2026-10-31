"""Shared fixtures for odometer tests: scheduled inspections with their odometer obligations."""

from datetime import timedelta
from uuid import uuid4

from cya.core.models import CommandReceipt
from cya.obligations.tests.support import ObligationCase
from cya.odometer.commands import record_odometer, set_odometer_threshold
from cya.odometer.models import (
    EngineHoursEntry,
    OdometerEntry,
    OdometerObligationLink,
    OdometerThreshold,
)


class OdometerCase(ObligationCase):
    """Two daily 06:00 inspections generated (due 06-01 and 06-02; early/grace one hour each).

    The clock starts at midnight on 2026-06-01, so the first odometer obligation opens at 05:00.
    """

    def setUp(self):
        super().setUp()
        self.version_id = self.published_version()
        self.publish_schedule("test-0600")
        self.bind(reconcile=False)
        self.generate()
        self.obligation_rows = {
            row.due_at: row for row in self.odometers() if row.vehicle_id == self.vehicle.pk
        }
        self.first, self.second = (
            self.obligation_rows[self.at(hours=6)],
            self.obligation_rows[self.at(days=1, hours=6)],
        )

    def record(self, value, unit="mi", actor=None, operation_id=None, vehicle=None, **fields):
        return record_odometer(
            vehicle_id=str((vehicle or self.vehicle).pk),
            value=value,
            unit=unit,
            organization_id=str(self.org.pk),
            actor_id=str((actor or self.crew).pk),
            operation_id=operation_id or str(uuid4()),
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
            **fields,
        )

    def set_threshold(self, unit, limit, actor=None):
        return set_odometer_threshold(
            unit=unit,
            max_increase=limit,
            organization_id=str(self.org.pk),
            actor_id=str((actor or self.admin).pk),
            operation_id=str(uuid4()),
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
        )

    def counts(self):
        return (
            OdometerEntry.objects.count(),
            EngineHoursEntry.objects.count(),
            OdometerObligationLink.objects.count(),
            OdometerThreshold.objects.count(),
            CommandReceipt.objects.count(),
        )

    def at_window(self, minutes):
        """Move the server clock to the first inspection's due instant plus the offset."""
        self.clock.set(self.at(hours=6) + timedelta(minutes=minutes))
