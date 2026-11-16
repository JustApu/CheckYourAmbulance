"""Administrator command that binds a vehicle's checklist template to a schedule slot.

The binding says which checklist template follows which schedule slot for a vehicle, from an
effective instant. It is append-only: unbinding appends an UNBIND revision, and history stays.
Only an active administrator may bind (as for schedule revisions). The command locks the
vehicle and then the template, the same order the generator uses, so a bind cannot interleave
with a reconciliation of the same vehicle. After it commits, the vehicle's upcoming
obligations are reconciled; a failure there is logged and left to the next generator run.
"""

import logging
import re
from datetime import datetime

from cya.accounts.fields import new_id
from cya.accounts.models import Organization
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.checklists.models import ChecklistItemDef, ChecklistTemplate, Vehicle
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.obligations.generation import reconcile_vehicle
from cya.obligations.models import (
    BINDING_ACTIONS,
    RequirementBinding,
    RequirementVersion,
    SharedSatisfactionRule,
    SharedSatisfactionRuleMember,
)
from cya.schedules.models import ScheduleRule

logger = logging.getLogger(__name__)
MAX_SHARED_RULE_MEMBERS = 1000


BIND_ACTION = "bind_unbind_template"


class BindingError(ValueError):
    """The requested binding is invalid or does not change anything."""


class PreviewChanged(BindingError):
    """The CFG-8 preview recomputed at confirmation differs from the one the user confirmed."""


def _slot(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", value) is None:
        raise BindingError("slot must be 1–100 lowercase letters, digits or hyphens")


def _aware(value):
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise BindingError("effective_at must be timezone-aware")
    return value


def bind_requirement(
    *,
    organization_id,
    actor_id,
    vehicle_id,
    template_id,
    slot,
    action="BIND",
    effective_at=None,
    operation_id,
    correlation_id,
    request_attempt_id,
    preview_token=None,
):
    """Append a BIND or UNBIND revision for (vehicle, template, slot); history is never edited.

    preview_token is the digest of the CFG-8 preview the user confirmed. With it, an UNBIND takes
    effect at the instant it is recorded, and is refused (PreviewChanged) unless the preview
    recomputed then is identical.
    """
    for value in (organization_id, actor_id, vehicle_id, template_id):
        canonical_id(value)
    _slot(slot)
    if action not in BINDING_ACTIONS:
        raise BindingError("action must be BIND or UNBIND")
    requested = _aware(effective_at)
    if preview_token is not None:
        if action != "UNBIND" or requested is not None:
            raise BindingError("a preview applies only to an unbinding that takes effect now")
        if not isinstance(preview_token, str) or not re.fullmatch(r"[0-9a-f]{64}", preview_token):
            raise BindingError("preview token is malformed")

    # SPEC 3.3 and AC-37(1): Supervisors and Administrators, through the policy action.
    from cya.kits.definitions import authorizer

    authorize = authorizer(organization_id, actor_id, BIND_ACTION)

    def prepare(actor):
        vehicle = (
            Vehicle.objects.select_for_update()
            .filter(organization_id=organization_id, pk=vehicle_id)
            .first()
        )
        template = (
            ChecklistTemplate.objects.select_for_update()
            .filter(organization_id=organization_id, pk=template_id)
            .first()
        )
        # SPEC CFG-7: a binding may be recorded while the vehicle is inactive. Reconciliation
        # covers active vehicles only, so it owes nothing until the vehicle is activated.
        if vehicle is None:
            raise BindingError("vehicle not found")
        if template is None or not template.active:
            raise BindingError("template not found or not active")
        if not ScheduleRule.objects.filter(organization_id=organization_id, slot=slot).exists():
            raise BindingError("no schedule has been published for that slot")
        prior = (
            RequirementBinding.objects.select_for_update()
            .filter(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                template_id=template_id,
                slot=slot,
            )
            .order_by("-revision")
            .first()
        )
        if (action == "BIND") == (prior is not None and prior.action == "BIND"):
            raise BindingError(
                f"the binding is already {'bound' if action == 'BIND' else 'unbound'}"
            )
        revision = prior.revision + 1 if prior else 1
        if action == "BIND":
            # SPEC 4.8 CFG-2: a binding cannot make a retired position required again. The
            # vehicle row is locked above, as a retirement locks it.
            from django.utils import timezone

            from cya.kits.positions import refuse_retired, template_slot_rows

            rows = template_slot_rows(organization_id, template_id, timezone.now())
            for kind, row_ids in rows.items():
                refuse_retired(kind, vehicle_id, row_ids, BindingError, "the template")

        def apply(receipt, current):
            effective = requested or receipt.recorded_at
            # Time rules run against the receipt's instant, assigned after prepare (no backfill).
            if effective < receipt.recorded_at:
                raise BindingError("a binding cannot take effect before it is recorded")
            if prior is not None and effective <= prior.effective_at:
                raise BindingError("a revision must take effect after the previous revision")
            if preview_token is not None:
                from cya.obligations.unbinding import PreviewTooLarge, unbind_preview

                try:
                    fresh = unbind_preview(
                        organization_id, vehicle_id, template_id, slot, receipt.recorded_at
                    )
                except PreviewTooLarge as exc:
                    raise BindingError(str(exc)) from None
                if fresh.token != preview_token:
                    raise PreviewChanged("the obligations or binding changed since the preview")
            RequirementBinding.objects.create(
                organization_id=organization_id,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
                vehicle_id=vehicle_id,
                template_id=template_id,
                slot=slot,
                revision=revision,
                action=action,
                effective_at=effective,
            )
            if action == "BIND":
                from cya.checks.locks import ensure_item_locks

                item_ids = (
                    ChecklistItemDef.objects.filter(
                        checklist_version__template_id=template_id,
                    )
                    .values_list("item_id", flat=True)
                    .distinct()
                )
                ensure_item_locks(organization_id, [vehicle_id], item_ids)

        values = {
            "vehicle_id": vehicle_id,
            "template_id": template_id,
            "slot": slot,
            "revision": revision,
            "action": action,
            "effective_at": requested.isoformat() if requested else None,
        }
        return Plan(
            response_body={"code": "BOUND" if action == "BIND" else "UNBOUND", **values},
            apply=apply,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={"schema_version": 1, "values": values},
        )

    result = run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type="REQUIREMENT_BIND",
        request_digest=digest(
            {
                "vehicle_id": vehicle_id,
                "template_id": template_id,
                "slot": slot,
                "action": action,
                "effective_at": requested.isoformat() if requested else None,
                "preview_token": preview_token,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=prepare,
    )
    if not result.replayed:
        _reconcile_after(organization_id, vehicle_id)
    return result


def configure_shared_satisfaction_rule(
    *,
    organization_id,
    actor_id,
    members,
    effective_at=None,
    operation_id,
    correlation_id,
    request_attempt_id,
):
    """Append an audited rule snapshot; there is deliberately no administration UI yet."""
    for value in (organization_id, actor_id):
        canonical_id(value)
    if not isinstance(members, (list, tuple)) or not members:
        raise BindingError("members must be a non-empty list")
    if len(members) > MAX_SHARED_RULE_MEMBERS:
        raise BindingError("shared rules may name at most 1000 requirement pairs")
    normalized = []
    for pair in members:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise BindingError("each member must name source and additional requirement versions")
        source, additional = canonical_id(pair[0]), canonical_id(pair[1])
        if source == additional:
            raise BindingError("a shared rule cannot name the same requirement twice")
        normalized.append((source, additional))
    if len(set(normalized)) != len(normalized):
        raise BindingError("shared rule members must be unique")
    if effective_at is not None and (
        not isinstance(effective_at, datetime)
        or effective_at.tzinfo is None
        or effective_at.utcoffset() is None
    ):
        raise BindingError("effective_at must be timezone-aware")

    def authorize(org):
        with locked_identities(organization_id, [actor_id]) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = load_state(DatabaseRoleBackend(), user).grants
            actor = Actor(
                user=user,
                roles=tuple(sorted((Role.CREW if r == Role.EMT else r).value for r in roles)),
            )
            if not current_org.active or not user.active or Role.ADMINISTRATOR not in roles:
                raise CommandDenied(actor)
            return actor

    def prepare(actor):
        Organization.objects.select_for_update().get(pk=organization_id)
        versions = {
            str(row.pk): row
            for row in RequirementVersion.objects.filter(
                organization_id=organization_id,
                pk__in={value for pair in normalized for value in pair},
            )
        }
        if len(versions) != len({value for pair in normalized for value in pair}):
            raise BindingError("a shared rule requirement version was not found")
        for source_id, additional_id in normalized:
            source, additional = versions[source_id], versions[additional_id]
            if (source.vehicle_id, source.item_id, source.kit_key) != (
                additional.vehicle_id,
                additional.item_id,
                additional.kit_key,
            ):
                raise BindingError("shared rule members must have the same vehicle/item/kit scope")
        revision = (
            SharedSatisfactionRule.objects.filter(organization_id=organization_id)
            .order_by("-revision")
            .values_list("revision", flat=True)
            .first()
            or 0
        ) + 1
        prior_effective = (
            SharedSatisfactionRule.objects.filter(organization_id=organization_id)
            .order_by("-effective_at")
            .values_list("effective_at", flat=True)
            .first()
        )
        rule_id = new_id()

        def apply(receipt, current):
            effective = effective_at or receipt.recorded_at
            if effective < receipt.recorded_at:
                raise BindingError("effective_at cannot precede the rule's recorded instant")
            if prior_effective is not None and effective <= prior_effective:
                raise BindingError("a shared-rule revision must follow the prior effective instant")
            rule = SharedSatisfactionRule.objects.create(
                id=rule_id,
                organization_id=organization_id,
                revision=revision,
                effective_at=effective,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )
            SharedSatisfactionRuleMember.objects.bulk_create(
                SharedSatisfactionRuleMember(
                    id=new_id(),
                    organization_id=organization_id,
                    rule=rule,
                    command=receipt,
                    recorded_at=receipt.recorded_at,
                    actor_user=current.user,
                    actor_roles_at_time=list(current.roles),
                    source_requirement_version_id=source,
                    additional_requirement_version_id=additional,
                )
                for source, additional in normalized
            )

        values = {
            "rule_id": rule_id,
            "revision": revision,
            "members": normalized,
            "effective_at": effective_at.isoformat() if effective_at else None,
        }
        return Plan(
            response_body={"code": "SHARED_RULE_CONFIGURED", **values},
            apply=apply,
            target_type="ORGANIZATION",
            target_id=organization_id,
            new_snapshot={"schema_version": 1, "values": values},
        )

    return run_command(
        organization_id=organization_id,
        operation_id=operation_id,
        command_type="SHARED_SATISFACTION_RULE_SET",
        request_digest=digest(
            {
                "members": normalized,
                "effective_at": effective_at.isoformat() if effective_at else None,
            }
        ),
        correlation_id=correlation_id,
        request_attempt_id=request_attempt_id,
        authorize=authorize,
        prepare=prepare,
    )


def _reconcile_after(organization_id, vehicle_id):
    summary = reconcile_vehicle(organization_id, vehicle_id)
    for failure in summary.failures:
        logger.error(
            "obligation reconciliation failed after binding change: vehicle %s due %s: %s",
            failure.vehicle_id,
            failure.due_at.isoformat(),
            failure.error,
        )


__all__ = ["BindingError", "bind_requirement"]
