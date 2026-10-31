"""Audited administrator revisions and explicit operator-only initial seeding."""

import json
import re
from dataclasses import asdict
from datetime import time
from uuid import uuid4

from django.conf import settings

from cya.accounts.models import Organization
from cya.authorization.backend import load_state
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.core.models import CommandReceipt
from cya.schedules.engine import Rule
from cya.schedules.models import ScheduleRule
from tools.command_identity import worker_operation_id

# SPEC §5.2 demonstration schedule values; editable configuration, never recurrence-engine behavior.
DEFAULT_SCHEDULES = (
    # These slot IDs are stable persisted keys; display names are configurable.
    ("first-due-0600", "DAILY", time(6)),
    ("first-due-1800", "DAILY", time(18)),
    ("second-due-weekday", "WEEKDAY", time(9)),
    ("second-due-weekend", "WEEKEND", time(12)),
    ("third-due-1800", "DAILY", time(18)),
    ("fourth-due-1800", "DAILY", time(18)),
)


def _values(rule):
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in asdict(rule).items()
    }


def _slot(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", value) is None:
        raise ValueError("slot must be 1–100 lowercase letters, digits or hyphens")


def _append(org_id, slot, revision, rule, receipt, actor):
    return ScheduleRule.objects.create(
        organization_id=org_id,
        slot=slot,
        revision=revision,
        **asdict(rule),
        command=receipt,
        actor_user=actor.user,
        actor_roles_at_time=list(actor.roles),
        recorded_at=receipt.recorded_at,
    )


def publish_rule(
    *, organization_id, actor_id, slot, rule, operation_id, correlation_id, request_attempt_id
):
    """Append a revision under current admin authorization, retaining all history."""
    _slot(slot)
    if not isinstance(rule, Rule):
        raise ValueError("rule must be a validated Rule")

    def authorize(org):
        with locked_identities(organization_id, [actor_id]) as (current_org, users):
            user = users[actor_id]
            roles = load_state(DatabaseRoleBackend(), user).grants
            actor = Actor(
                user=user,
                roles=tuple(sorted((Role.CREW if r == Role.EMT else r).value for r in roles)),
            )
            if not current_org.active or not user.active or Role.ADMINISTRATOR not in roles:
                raise CommandDenied(actor)
            if rule.zone != current_org.station_zone:
                raise ValueError("Schedule zone must match station zone")
            return actor

    def prepare(actor):
        prior = (
            ScheduleRule.objects.filter(organization_id=organization_id, slot=slot)
            .order_by("-revision")
            .first()
        )
        if prior and rule.effective_at <= prior.effective_at:
            raise ValueError("A revision must take effect after the previous revision")
        revision = prior.revision + 1 if prior else 1
        return Plan(
            response_body={"slot": slot, "revision": revision},
            apply=lambda receipt, current: _append(
                organization_id, slot, revision, rule, receipt, current
            ),
            prior_snapshot={
                "schema_version": 1,
                "values": {"slot": slot, "revision": prior.revision, **_values(prior.as_rule())},
            }
            if prior
            else None,
            new_snapshot={
                "schema_version": 1,
                "values": {"slot": slot, "revision": revision, **_values(rule)},
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type="SCHEDULE_PUBLISH",
        request_digest=digest({"slot": slot, **_values(rule)}),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=prepare,
    )


def seed_default_schedules(organization_id, effective_at):
    """Explicit setup command, not a migration that guesses the organization."""
    if settings.CYA_DATABASE_ROLE != "schema_owner":
        raise PermissionError("Initial seeding requires schema_owner mode")
    service, action = "operator.schedules", "SCHEDULE_SEED"
    prior_receipts = list(
        CommandReceipt.objects.filter(
            organization_id=organization_id,
            service_identity=service,
            command_type=action,
        ).order_by("recorded_at")[:2]
    )
    if len(prior_receipts) > 1:
        raise ValueError("Multiple initial schedule receipts exist; inspect before retrying")
    if prior_receipts:
        prior = prior_receipts[0]
        if not prior.job_key:
            raise ValueError("Initial schedule receipt has no worker identity")
        key, operation_id = prior.job_key, str(prior.operation_id)
    else:
        key = json.dumps(["default-schedules-initial-v1"], separators=(",", ":"))
        operation_id = str(worker_operation_id(str(organization_id), service, action, key))
    attempt = str(uuid4())

    def authorize(org):
        org = Organization.objects.select_for_update().get(pk=org.pk)
        if not org.active:
            raise CommandDenied()
        return Actor(service_identity=service, job_key=key)

    def prepare(actor):
        org = Organization.objects.get(pk=organization_id)
        if ScheduleRule.objects.filter(
            organization=org, slot__in=[s[0] for s in DEFAULT_SCHEDULES]
        ).exists():
            raise ValueError("Initial slots already exist; use administrator revisions")
        rules = [
            (slot, Rule(org.station_zone, wall, effective_at, recurrence))
            for slot, recurrence, wall in DEFAULT_SCHEDULES
        ]

        def apply(receipt, current):
            for slot, rule in rules:
                _append(organization_id, slot, 1, rule, receipt, current)

        return Plan(
            response_body={"code": "SEEDED", "count": len(rules)},
            apply=apply,
            new_snapshot={
                "schema_version": 1,
                "values": {
                    "slots": [s for s, _ in rules],
                    "effective_at": effective_at.isoformat(),
                    "zone": org.station_zone,
                },
            },
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type=action,
        request_digest=digest({"effective_at": effective_at.isoformat()}),
        correlation_id=attempt,
        request_attempt_id=attempt,
        authorize=authorize,
        prepare=prepare,
    )
