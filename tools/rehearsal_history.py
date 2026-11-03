"""Synthetic history for the backup and restore rehearsal (issue #7, Part A).

Builds a synthetic agency in an empty application database and records a year (or any number of
days) of daily inspections through the real, audited commands, so the backup holds realistic rows
in realistic proportions: obligations, selections, checks and their links, receipts and audit
events, deficiencies with their events and closures. Only the clock is replaced, as the scenario
drivers do, so the history can start in the past; every guard and trigger still runs.

Synthetic only (DEP-9 rehearsal rules): every name ends in "Synthetic", every address is
@example.test, and nothing is copied from real data. It refuses a database that already holds an
organization. Run it inside Django with the schema-owner environment of a rehearsal database:

    python manage.py shell -c "from tools.rehearsal_history import generate; generate()"
"""

import io
from datetime import UTC, datetime, time, timedelta
from random import Random
from unittest import mock
from uuid import uuid4

from django.core.management import call_command
from django.db import transaction

from cya.accounts.models import Organization, User
from cya.authorization.commands import change_role
from cya.authorization.roles import grant_initial_crew
from cya.checklists.commands import publish_template_version, save_template_draft
from cya.checklists.models import ChecklistTemplate, Compartment, Item, Vehicle
from cya.checks.commands import record_check
from cya.deficiencies.closure import close_administrator
from cya.deficiencies.models import DeficiencyCategory, DeficiencyEvent, DeficiencyProjection
from cya.deficiencies.severity import ensure_default_severities
from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.obligations.commands import bind_requirement
from cya.obligations.generation import run_generation
from cya.schedules.commands import publish_rule
from cya.schedules.engine import Rule

SLOT = "rehearsal-0600"
INSPECTION = time(6, 0)  # due 06:00 UTC; the window opens an hour earlier
FAILED_RATE = 0.012
UNABLE_RATE = 0.003
CLOSE_AFTER = timedelta(days=3)


class Clock:
    """The replaced timezone.now: moves forward by a microsecond per read, never backwards."""

    def __init__(self, instant):
        self.instant = instant

    def __call__(self):
        self.instant += timedelta(microseconds=1)
        return self.instant

    def set(self, instant):
        if instant < self.instant:
            raise ValueError("the rehearsal clock cannot run backwards")
        self.instant = instant


class Agency:
    def __init__(self, clock, rng, vehicles, items, crew):
        self.clock, self.rng = clock, rng
        self.size = (vehicles, items, crew)

    def context(self, actor=None):
        return {
            "organization_id": str(self.org.pk),
            "actor_id": str((actor or self.admin).pk),
            "operation_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "request_attempt_id": str(uuid4()),
        }

    def user(self, name):
        user = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject=f"synthetic-rehearsal-{name}",
            email=f"rehearsal-{name}@example.test",
            display_name=f"Rehearsal {name} Synthetic",
        )
        with transaction.atomic():
            grant_initial_crew(user=user, command_id=str(uuid4()))
        return user

    def build(self, start):
        vehicles, items, crew = self.size
        self.org = Organization.objects.create(
            name="Rehearsal agency Synthetic", station_zone="UTC"
        )
        self.admin = self.user("administrator")
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=self.admin.pk,
            stdout=io.StringIO(),
        )
        supervisor = self.user("supervisor")
        change_role(
            target_id=str(supervisor.pk), role="SUPERVISOR", action="GRANT", **self.context()
        )
        self.crew = [self.user(f"crew-{n:02d}") for n in range(1, crew + 1)]
        slot_id = str(uuid4())
        self.vehicles = []
        for n in range(1, vehicles + 1):
            vehicle = Vehicle.objects.create(
                organization=self.org, display_name=f"Rehearsal unit {n} Synthetic"
            )
            compartment = Compartment.objects.create(
                organization=self.org,
                vehicle=vehicle,
                stable_slot_id=slot_id,
                revision=1,
                label="Rehearsal cabinet Synthetic",
                effective_at=start,
            )
            self.vehicles.append((vehicle, compartment))
        self.items = [
            Item.objects.create(organization=self.org, label=f"Rehearsal item {n:03d} Synthetic")
            for n in range(1, items + 1)
        ]
        template = ChecklistTemplate.objects.create(
            organization=self.org, label="Rehearsal daily inspection Synthetic"
        )
        first_compartment = self.vehicles[0][1]
        definitions = [
            {
                "item_id": str(item.pk),
                "display_order": index * 10,
                "kind": "PRESENCE_CONDITION",
                "instructions": "Synthetic rehearsal check.",
                "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
                "compartment_id": str(first_compartment.pk),
            }
            for index, item in enumerate(self.items, 1)
        ]
        self.version = save_template_draft(
            template_id=str(template.pk), items=definitions, **self.context()
        ).body["version_id"]
        publish_template_version(version_id=self.version, **self.context())
        publish_rule(slot=SLOT, rule=Rule("UTC", INSPECTION, start), **self.context())
        for vehicle, _compartment in self.vehicles:
            bind_requirement(
                vehicle_id=str(vehicle.pk),
                template_id=str(template.pk),
                slot=SLOT,
                **self.context(),
            )
        ensure_default_severities(self.org.pk, start)
        self.category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=str(uuid4()),
            revision=1,
            label="Rehearsal defect Synthetic",
            supply_related=False,
            effective_at=start,
        )

    def inspect(self, counts):
        """One crew member checks every item of each vehicle inside the morning window."""
        for vehicle, _compartment in self.vehicles:
            actor = self.rng.choice(self.crew)
            for item in self.items:
                roll = self.rng.random()
                fields = {"outcome": "PASSED"}
                if roll < FAILED_RATE:
                    fields = {"outcome": "FAILED", "deficiency": self.defect()}
                elif roll < FAILED_RATE + UNABLE_RATE:
                    fields = {
                        "outcome": "UNABLE_TO_INSPECT",
                        "explanation": "Synthetic: unit was on a call.",
                        "deficiency": self.defect(),
                    }
                record_check(
                    vehicle_id=str(vehicle.pk),
                    item_id=str(item.pk),
                    checklist_version_id=self.version,
                    **fields,
                    **self.context(actor),
                )
                counts[fields["outcome"]] = counts.get(fields["outcome"], 0) + 1

    def defect(self):
        return {
            "create": {
                "category_id": str(self.category.stable_category_id),
                "severity": self.rng.choice(["LOW", "MEDIUM", "HIGH"]),
                "summary": "Rehearsal defect Synthetic",
                "description": "Synthetic rehearsal evidence.",
            }
        }

    def close_old(self, counts):
        """An administrator closes deficiencies still open after CLOSE_AFTER (weekly sweep)."""
        cutoff = self.clock.instant - CLOSE_AFTER
        open_rows = DeficiencyProjection.objects.filter(
            organization_id=self.org.pk, status__in=ACTIVE_STATUSES
        ).values_list("deficiency_id", flat=True)
        for deficiency_id in list(open_rows):
            newest = DeficiencyEvent.objects.filter(deficiency_id=deficiency_id).latest("sequence")
            if newest.recorded_at > cutoff:
                continue
            close_administrator(
                deficiency_id=str(deficiency_id),
                expected_event_id=str(newest.pk),
                explanation="Synthetic: repaired at the station.",
                **self.context(),
            )
            counts["CLOSED"] = counts.get("CLOSED", 0) + 1


def generate(*, vehicles=5, items=150, days=365, crew=15, seed=7, end=None, progress=print):
    """Build the agency and record `days` days of inspections ending before `end` (default: now).

    Returns the counts of recorded outcomes and closures. Refuses a database that already holds
    an organization, so it never mixes with other data.
    """
    if Organization.objects.exists():
        raise RuntimeError("the rehearsal database already holds an organization; use an empty one")
    if not (1 <= vehicles <= 100 and 1 <= items <= 500 and 1 <= days <= 1100 and 1 <= crew <= 200):
        raise ValueError("vehicles, items, days and crew are out of the supported range")
    real_now = end or datetime.now(UTC)
    first_day = (real_now - timedelta(days=days + 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    clock = Clock(first_day)
    rng = Random(seed)
    agency = Agency(clock, rng, vehicles, items, crew)
    counts = {}
    with mock.patch("django.utils.timezone.now", side_effect=clock):
        agency.build(first_day)
        for day in range(days):
            morning = first_day + timedelta(days=day)
            clock.set(morning + timedelta(hours=4))
            failures = run_generation(str(agency.org.pk)).failures
            if failures:
                raise RuntimeError(f"obligation generation failed on day {day}: {failures}")
            clock.set(morning + timedelta(hours=5, minutes=30))
            agency.inspect(counts)
            if day % 7 == 6:
                clock.set(morning + timedelta(hours=12))
                agency.close_old(counts)
            progress(f"day {day + 1}/{days} {morning.date()} counts={counts}")
    return counts
