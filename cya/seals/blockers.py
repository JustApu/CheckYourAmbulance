"""Determine whether a seal's covered physical items can substitute for checks."""

from cya.deficiencies import service
from cya.deficiencies.effects import NO_KIT_KEY
from cya.deficiencies.models import Deficiency
from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.seals.locking import MAX_LOCK_ROWS, SealLockError


def seal_blockers(organization_id, projections, covered_pairs, *, at):
    """Return (open deficiency, suspended check) for the already locked vehicle scope.

    `covered_pairs` contains server-resolved (item id, physical kit id or None) pairs,
    including applicable kit-presence items. The caller holds every vehicle deficiency lock
    through the transaction, so reopening and suspension changes cannot race this read.
    """
    pairs = {(str(item), str(kit) if kit is not None else None) for item, kit in covered_pairs}
    if len(pairs) > MAX_LOCK_ROWS:
        raise SealLockError("seal blocker scope is too large")
    if len(projections) > MAX_LOCK_ROWS:
        raise SealLockError("too many deficiencies to check")
    deficiencies = {
        str(row.pk): row
        for row in Deficiency.objects.filter(
            organization_id=organization_id, pk__in=projections
        ).order_by("pk")
    }
    if set(deficiencies) != set(projections):
        raise SealLockError("a locked deficiency is unavailable")
    has_open = False
    has_suspension = False
    for deficiency_id, projection in projections.items():
        if projection.status in ACTIVE_STATUSES:
            rows = service.correction_scope(organization_id, deficiencies[deficiency_id], at)
            if len(rows) > MAX_LOCK_ROWS:
                raise SealLockError("deficiency scope is too large")
            has_open |= any(
                (str(item), str(kit) if kit else None) in pairs for item, _, kit in rows
            )
        suspensions = service.suspensions_in_force(organization_id, deficiency_id)
        has_suspension |= any(
            (str(row.item_id), None if row.instance_key == NO_KIT_KEY else str(row.instance_key))
            in pairs
            for row in suspensions
        )
        if has_open and has_suspension:
            break
    return has_open, has_suspension
