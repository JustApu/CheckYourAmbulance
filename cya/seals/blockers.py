"""Determine whether a seal's covered physical items can substitute for checks."""

from cya.deficiencies import service
from cya.deficiencies.effects import NO_KIT_KEY
from cya.deficiencies.models import Deficiency, DeficiencyAffectedItem
from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.seals.locking import MAX_LOCK_ROWS, SealLockError


def seal_blockers(organization_id, projections, covered_pairs, *, at, presence_lines=()):
    """Return (open deficiency, suspended check) for the already locked vehicle scope.

    `covered_pairs` contains server-resolved (item id, physical kit id or None) pairs;
    `presence_lines` contains (presence item id, kit-list line id) pairs. The caller holds
    every vehicle deficiency lock through the transaction, so reopening and suspension
    changes cannot race this read.
    """
    pairs = {(str(item), str(kit) if kit is not None else None) for item, kit in covered_pairs}
    if len(pairs) > MAX_LOCK_ROWS:
        raise SealLockError("seal blocker scope is too large")
    if len(projections) > MAX_LOCK_ROWS:
        raise SealLockError("too many deficiencies to check")
    line_pairs = {(str(item), str(line)) for item, line in presence_lines}
    if len(line_pairs) > MAX_LOCK_ROWS:
        raise SealLockError("seal presence scope is too large")
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
    if line_pairs:
        active_ids = [key for key, value in projections.items() if value.status in ACTIVE_STATUSES]
        affected = DeficiencyAffectedItem.objects.filter(
            organization_id=organization_id,
            deficiency_id__in=active_ids,
            kit_requirement_id__in={line for _, line in line_pairs},
        ).values_list("item_id", "kit_requirement_id")[: MAX_LOCK_ROWS + 1]
        affected = list(affected)
        if len(affected) > MAX_LOCK_ROWS:
            raise SealLockError("too many kit-presence deficiency rows")
        has_open = any((str(item), str(line)) in line_pairs for item, line in affected)
    for deficiency_id, projection in projections.items():
        if projection.status in ACTIVE_STATUSES:
            rows = service.correction_scope(organization_id, deficiencies[deficiency_id], at)
            if len(rows) > MAX_LOCK_ROWS:
                raise SealLockError("deficiency scope is too large")
            has_open |= any(
                (str(item), str(kit) if kit else None) in pairs for item, _, kit in rows
            )
        suspensions = service.suspensions_in_force(organization_id, deficiency_id)
        for row in suspensions:
            if row.kit_requirement_id is not None:
                has_suspension |= (str(row.item_id), str(row.kit_requirement_id)) in line_pairs
            else:
                has_suspension |= (
                    str(row.item_id),
                    None if row.instance_key == NO_KIT_KEY else str(row.instance_key),
                ) in pairs
        if has_open and has_suspension:
            break
    return has_open, has_suspension
