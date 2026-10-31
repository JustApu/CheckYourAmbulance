"""Append UTI-3 determination effects inside the check-chain command's transaction."""

from dataclasses import dataclass

from cya.accounts.fields import new_id
from cya.checks.models import DeterminationCheck
from cya.conditions.models import ObligationDetermination

MAX_COMPLETIONS = 100


class CompletionEffectLimit(ValueError):
    """Refuse an oversized effect set before the check event is written."""


@dataclass(frozen=True)
class CompletionEffect:
    id: str
    prior: ObligationDetermination


def plan_check_effects(organization_id, root_id):
    """Lock current completions supported by any row of this chain, under its item lock."""
    supported = DeterminationCheck.objects.filter(
        organization_id=organization_id, check_record__root_check_id=root_id
    ).values("determination_id")
    replaced = ObligationDetermination.objects.filter(
        organization_id=organization_id, prior_determination__isnull=False
    ).values("prior_determination_id")
    rows = list(
        ObligationDetermination.objects.select_for_update()
        .filter(
            organization_id=organization_id,
            pk__in=supported,
            determination_type="SUPERVISOR_COMPLETED",
        )
        .exclude(pk__in=replaced)
        .order_by("obligation_id", "id")[: MAX_COMPLETIONS + 1]
    )
    if len(rows) > MAX_COMPLETIONS:
        raise CompletionEffectLimit("check chain supports too many supervisor completions")
    return tuple(CompletionEffect(new_id(), row) for row in rows)


def append_check_effects(effects, receipt, actor, *, root_id, event_id, determination_type):
    """The check event is already appended; each child preserves its completion's scope."""
    explanation = {
        "SUPERSEDED": "Supporting Unable check was corrected to Passed or Failed.",
        "INVALIDATED": "Supporting Unable check chain was invalidated.",
    }[determination_type]
    for effect in effects:
        prior = effect.prior
        ObligationDetermination.objects.create(
            id=effect.id,
            organization_id=prior.organization_id,
            command=receipt,
            recorded_at=receipt.recorded_at,
            actor_user=actor.user,
            actor_roles_at_time=list(actor.roles),
            obligation_id=prior.obligation_id,
            vehicle_id=prior.vehicle_id,
            due_at=prior.due_at,
            determination_type=determination_type,
            prior_determination=prior,
            explanation=explanation,
            evaluated_inputs={"root_check_id": str(root_id), "check_event_id": str(event_id)},
        )
