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

from cya.authorization.backend import canonical_id
from cya.authorization.persistence import digest
from cya.checklists.models import ChecklistItemDef, ChecklistTemplate, Vehicle
from cya.core.audit_commands import Plan, run_command
from cya.obligations.generation import reconcile_vehicle
from cya.obligations.models import (
    BINDING_ACTIONS,
    RequirementBinding,
)
from cya.schedules.models import ScheduleRule

logger = logging.getLogger(__name__)


BIND_ACTION = "bind_unbind_template"


class BindingError(ValueError):
    """The requested binding is invalid or does not change anything."""


class SharedRulesRetired(BindingError):
    """SPEC 6.0.0 SCH-5: shared-satisfaction rules are history only."""

    code, status = "SHARED_RULES_RETIRED", 409

    def __init__(self):
        super().__init__("shared-satisfaction rules can no longer be created (SPEC 6.0.0 SCH-5)")


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
        # SPEC 5.0.0 CFG-7: a vehicle checklist is assigned only to its own vehicle, and only
        # after the cutover; a legacy template's bindings are read-only after it.
        from cya.checklists.ownership import require_template_writable

        require_template_writable(organization_id, template, vehicle_id, BindingError)
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


def configure_shared_satisfaction_rule(**_ignored):
    """Refuse: no new shared-satisfaction rule can be created (SPEC 6.0.0 SCH-5).

    One check now links to every eligible obligation of its item (SCH-6), so a rule has nothing
    left to declare. Rules and links recorded before 6.0.0 remain in history; the database
    refuses a new rule row too (cya_checks 0015).
    """
    raise SharedRulesRetired()


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
