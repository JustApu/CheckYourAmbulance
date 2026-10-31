"""Shared fixtures for obligation tests: a controllable server clock, schedules and bindings.

The code under test reads django.utils.timezone.now() for every server instant (TIME-4), so the
tests replace it with a clock they set. Each reading advances one microsecond, which keeps
recorded_at strictly increasing across commands without tests having to care.
"""

import threading
from datetime import UTC, datetime, time, timedelta
from unittest import mock
from uuid import uuid4

from cya.checklists.models import Vehicle
from cya.checklists.tests.support import ChecklistCase, ids
from cya.obligations import commands
from cya.obligations.generation import run_generation
from cya.obligations.models import Obligation, ObligationSelection
from cya.obligations.queries import effective_obligations, odometer_obligations
from cya.schedules.commands import publish_rule
from cya.schedules.engine import Rule

START = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


class Clock:
    """A settable server clock that never repeats an instant, safe across threads."""

    def __init__(self, start):
        self._now = start
        self._lock = threading.Lock()

    def set(self, instant):
        with self._lock:
            self._now = instant

    def __call__(self):
        with self._lock:
            self._now += timedelta(microseconds=1)
            return self._now

    @property
    def now(self):
        return self._now


class ObligationCase(ChecklistCase):
    """Organization, users, vehicles, a template and the means to schedule and bind it."""

    clock_start = START

    def setUp(self):
        self.clock = Clock(self.clock_start)
        patcher = mock.patch("django.utils.timezone.now", side_effect=self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    @property
    def vehicle(self):
        return self.vehicles[0]

    def at(self, hours=0, days=0, minutes=0):
        """An instant relative to START (a Monday, 2026-06-01 00:00 UTC)."""
        return START + timedelta(days=days, hours=hours, minutes=minutes)

    def publish_schedule(self, slot, wall_time=time(6), recurrence="DAILY", **window):
        rule = Rule(
            zone=self.org.station_zone,
            wall_time=wall_time,
            effective_at=window.pop("effective_at", self.clock.now),
            recurrence=recurrence,
            **window,
        )
        return publish_rule(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            slot=slot,
            rule=rule,
            **ids(),
        )

    def bind(self, vehicle=None, slot="test-0600", action="BIND", template=None, **kw):
        """Bind (or unbind). reconcile=False leaves the generator to do the first run itself."""
        if not kw.pop("reconcile", True):
            with mock.patch("cya.obligations.commands.reconcile_vehicle"):
                return self.bind(vehicle, slot, action, template, **kw)
        return commands.bind_requirement(
            organization_id=str(self.org.pk),
            actor_id=str((kw.pop("actor", None) or self.admin).pk),
            vehicle_id=str((vehicle or self.vehicle).pk),
            template_id=str((template or self.template).pk),
            slot=slot,
            action=action,
            **kw,
            **ids(),
        )

    def generate(self, **kw):
        return run_generation(str(self.org.pk), **kw)

    def obligations(self, start=None, end=None, vehicle=None, as_of=None):
        return effective_obligations(
            str(self.org.pk),
            start or self.clock_start - timedelta(days=30),
            end or self.clock_start + timedelta(days=400),
            vehicle_id=str(vehicle.pk) if vehicle else None,
            as_of=as_of,
        )

    def odometers(self, start=None, end=None, as_of=None):
        return odometer_obligations(
            str(self.org.pk),
            start or self.clock_start - timedelta(days=30),
            end or self.clock_start + timedelta(days=400),
            as_of=as_of,
        )

    def due_instants(self):
        return sorted({o.due_at for o in self.obligations()})

    def selection_events(self, vehicle=None):
        events = ObligationSelection.objects.filter(organization=self.org)
        if vehicle is not None:
            events = events.filter(vehicle=vehicle)
        return events.order_by("due_at", "item_id", "sequence")

    def all_obligation_rows(self):
        return Obligation.objects.filter(organization=self.org)

    def new_vehicle(self, name="Z"):
        return Vehicle.objects.create(organization=self.org, display_name=f"Synthetic {name}")

    def synthetic_ids(self):
        return {"operation_id": str(uuid4())}
