"""Shared synthetic fixtures for checklist tests (organization, users, slots, commands)."""

import io
from datetime import timedelta
from uuid import uuid4

from django.core.management import call_command
from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone

from cya.accounts.models import Organization, User
from cya.authorization.commands import change_role
from cya.authorization.roles import grant_initial_crew
from cya.checklists import commands
from cya.checklists.models import (
    ChecklistTemplate,
    Compartment,
    Item,
    Vehicle,
    VehicleSystem,
)

ALL_OUTCOMES = ["PASSED", "FAILED", "UNABLE_TO_INSPECT"]


def ids():
    """Fresh operation, correlation and attempt ids for one command call."""
    return {
        "operation_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "request_attempt_id": str(uuid4()),
    }


class ChecklistCase(TransactionTestCase):
    """Command tests commit for real (run_command owns the transaction), so flush afterwards."""

    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def make_org(self):
        """Subclasses that sign in over HTTP supply the one organization sign-in will find."""
        return Organization.objects.create(name="Sample Agency", station_zone="UTC")

    def setUp(self):
        self.org = self.make_org()
        self.admin = self.make_user("admin")
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=self.admin.pk,
            stdout=io.StringIO(),
        )
        self.crew = self.make_user("crew")
        self.supervisor = self.make_user("supervisor")
        self.grant(self.supervisor, "SUPERVISOR")
        self.vehicles = [
            Vehicle.objects.create(organization=self.org, display_name=f"Synthetic {n}")
            for n in ("A", "B")
        ]
        self.now = timezone.now()
        self.cabinet = self.make_slot(Compartment, "cabinet")
        self.system = self.make_slot(VehicleSystem, "lights")
        self.masks = Item.objects.create(organization=self.org, label="Synthetic masks")
        self.suction = Item.objects.create(organization=self.org, label="Synthetic suction")
        self.siren = Item.objects.create(organization=self.org, label="Synthetic siren")
        self.template = ChecklistTemplate.objects.create(
            organization=self.org, label="Synthetic checklist"
        )

    def make_user(self, subject):
        user = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject=subject,
            email=f"{subject}@example.test",
            display_name=f"User {subject}",
        )
        with transaction.atomic():
            grant_initial_crew(user=user, command_id=str(uuid4()))
        return user

    def grant(self, user, role):
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.admin.pk),
            target_id=str(user.pk),
            role=role,
            action="GRANT",
            **ids(),
        )

    def make_slot(self, model, key):
        """One slot on every vehicle sharing a stable_slot_id; returns the first vehicle's row."""
        slot_id = str(uuid4())
        rows = [
            model.objects.create(
                organization=self.org,
                vehicle=vehicle,
                stable_slot_id=slot_id,
                revision=1,
                label=f"Synthetic {key} {vehicle.display_name}",
                effective_at=self.now - timedelta(days=1),
            )
            for vehicle in self.vehicles
        ]
        return rows[0]

    def spec(self, item, order=10, **overrides):
        base = {
            "item_id": str(item.pk),
            "display_order": order,
            "kind": "PRESENCE_CONDITION",
            "instructions": "Synthetic.",
            "allowed_outcomes": ALL_OUTCOMES,
            "compartment_id": str(self.cabinet.pk),
        }
        base.update(overrides)
        return base

    def base_items(self):
        return [
            self.spec(self.suction, 10),
            self.spec(self.masks, 20, kind="QUANTITY", expected_quantity=6),
            self.spec(self.siren, 30, compartment_id=None, system_id=str(self.system.pk)),
        ]

    def context(self, actor=None):
        return {
            "organization_id": str(self.org.pk),
            "actor_id": str((actor or self.admin).pk),
            **ids(),
        }

    def draft(self, items=None, actor=None):
        return commands.save_template_draft(
            template_id=str(self.template.pk),
            items=self.base_items() if items is None else items,
            **self.context(actor),
        ).body["version_id"]

    def publish(self, version_id, effective_at=None, actor=None):
        return commands.publish_template_version(
            version_id=version_id, effective_at=effective_at, **self.context(actor)
        )

    def published_version(self, items=None, effective_at=None):
        version_id = self.draft(items)
        self.publish(version_id, effective_at)
        return version_id

    def resolve(self, vehicle=None, as_of=None):
        from cya.checklists.overlay import resolve_checklist

        return resolve_checklist(
            organization_id=str(self.org.pk),
            vehicle_id=str((vehicle or self.vehicles[0]).pk),
            template_id=str(self.template.pk),
            as_of=as_of or timezone.now(),
        )

    # --- SPEC 5.0.0 vehicle checklists -------------------------------------------------------

    def record_cutover(self, actor=None):
        """Record the organization's cutover the way the migration command does (SPEC 18.2).

        The migration of issue #220 part 2 writes this row inside its own command; tests of the
        model alone record it directly under a CHECKLIST_CUTOVER receipt.
        """
        from cya.authorization.persistence import digest
        from cya.checklists.models import ChecklistCutover
        from cya.core.audit_commands import Plan, run_command
        from cya.kits.definitions import authorizer

        org = str(self.org.pk)

        def prepare(_actor):
            def apply(receipt, current):
                ChecklistCutover.objects.create(
                    organization=self.org,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    actor_user=current.user,
                    actor_roles_at_time=list(current.roles),
                )

            return Plan(
                response_body={"code": "CHECKLIST_CUTOVER"},
                apply=apply,
                target_type="ORGANIZATION",
                target_id=org,
                new_snapshot={"schema_version": 1, "values": {}},
            )

        run_command(
            organization_id=org,
            command_type="CHECKLIST_CUTOVER",
            request_digest=digest({"organization_id": org}),
            authorize=authorizer(org, str((actor or self.admin).pk), "draft_edit_template"),
            prepare=prepare,
            **ids(),
        )
        return ChecklistCutover.objects.get(organization=self.org).recorded_at

    def own_row(self, row, vehicle):
        """The given vehicle's own row of the same stable slot as `row`."""
        return (
            type(row)
            .objects.filter(vehicle=vehicle, stable_slot_id=row.stable_slot_id)
            .order_by("-revision")
            .first()
        )

    def vehicle_checklist(self, vehicle=None, label="Synthetic own checklist", actor=None):
        from cya.operations.templates import create_vehicle_checklist

        body = create_vehicle_checklist(
            vehicle_id=str((vehicle or self.vehicles[0]).pk), label=label, **self.context(actor)
        ).body
        return ChecklistTemplate.objects.get(pk=body["checklist_id"])

    def own_items(self, vehicle=None):
        """The base items placed on `vehicle`'s own rows, plus the siren left Not placed."""
        vehicle = vehicle or self.vehicles[0]
        return [
            self.spec(self.suction, 10, compartment_id=str(self.own_row(self.cabinet, vehicle).pk)),
            self.spec(
                self.masks,
                20,
                kind="QUANTITY",
                expected_quantity=6,
                compartment_id=str(self.own_row(self.cabinet, vehicle).pk),
            ),
            self.spec(self.siren, 30, compartment_id=None),
        ]

    def publish_checklist(self, checklist, items, effective_at=None):
        version_id = commands.save_template_draft(
            template_id=str(checklist.pk), items=items, **self.context()
        ).body["version_id"]
        self.publish(version_id, effective_at)
        return version_id
