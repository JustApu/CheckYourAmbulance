"""Determine whether a seal's covered physical items can substitute for checks."""

from types import SimpleNamespace

from cya.deficiencies import service
from cya.deficiencies.effects import NO_KIT_KEY
from cya.deficiencies.models import Deficiency, DeficiencyAffectedItem, DeficiencyEvent
from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.kits.presence import line_unit_keys
from cya.seals.locking import MAX_LOCK_ROWS, SealLockError


def seal_blockers(
    organization_id,
    projections,
    covered_pairs,
    *,
    at,
    presence_lines=(),
    covered_kit_ids=(),
    suspensions_at=None,
):
    """Return (open deficiency, suspended check) for the already locked vehicle scope.

    `covered_pairs` contains server-resolved (item id, physical kit id or None) pairs;
    `presence_lines` contains (presence item id, kit-list line id) pairs. The caller holds
    every vehicle deficiency lock through the transaction, so reopening and suspension
    changes cannot race this read.
    """
    pairs = {(str(item), str(kit) if kit is not None else None) for item, kit in covered_pairs}
    kits = {str(kit) for kit in covered_kit_ids}
    if len(pairs) > MAX_LOCK_ROWS or len(kits) > MAX_LOCK_ROWS:
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

    def units_for(lines):
        ids = {str(line) for line in lines}
        units = line_unit_keys(ids)
        if set(units) != ids:
            raise SealLockError("a kit-presence line is unavailable")
        return units

    current_units = units_for(line for _, line in line_pairs)
    presence_units = {(item, current_units[line]) for item, line in line_pairs}
    if line_pairs:
        active_ids = [key for key, value in projections.items() if value.status in ACTIVE_STATUSES]
        affected = DeficiencyAffectedItem.objects.filter(
            organization_id=organization_id,
            deficiency_id__in=active_ids,
            item_id__in={item for item, _ in line_pairs},
            kit_requirement_id__isnull=False,
        ).values_list("item_id", "kit_requirement_id")[: MAX_LOCK_ROWS + 1]
        affected = list(affected)
        if len(affected) > MAX_LOCK_ROWS:
            raise SealLockError("too many kit-presence deficiency rows")
        affected_units = units_for(line for _, line in affected)
        has_open = any(
            (str(item), affected_units[str(line)]) in presence_units for item, line in affected
        )
    for deficiency_id, projection in projections.items():
        if projection.status in ACTIVE_STATUSES:
            deficiency = deficiencies[deficiency_id]
            # A physical kit may have no bound checklist. Its direct deficiency remains
            # a blocker even when the correction-time checklist resolver returns no rows.
            kit_id = getattr(deficiency, "kit_id", None)
            item_id = getattr(deficiency, "item_id", None)
            if kit_id is not None:
                has_open |= (
                    str(kit_id) in kits if item_id is None else (str(item_id), str(kit_id)) in pairs
                )
            rows = service.correction_scope(organization_id, deficiency, at)
            if len(rows) > MAX_LOCK_ROWS:
                raise SealLockError("deficiency scope is too large")
            # A seal pair is (item, kit or None): a place outside a kit is the item's None.
            has_open |= any((str(row[0]), str(row[2]) if row[2] else None) in pairs for row in rows)
        suspensions = (
            service.suspensions_in_force(organization_id, deficiency_id)
            if suspensions_at is None
            else service.suspensions_in_force_at(organization_id, deficiency_id, suspensions_at)
        )
        for row in suspensions:
            if row.kit_requirement_id is not None:
                unit = units_for([row.kit_requirement_id])[str(row.kit_requirement_id)]
                has_suspension |= (str(row.item_id), unit) in presence_units
            else:
                has_suspension |= (
                    str(row.item_id),
                    None
                    if row.instance_key == NO_KIT_KEY
                    or getattr(row, "item_requirement_version_id", None)
                    else str(row.instance_key),
                ) in pairs
        if has_open and has_suspension:
            break
    return has_open, has_suspension


def _statuses_at(organization_id, deficiency_ids, at):
    """{deficiency id: status} as recorded by `at`, for deficiencies reported by then."""
    reported = set(
        str(pk)
        for pk in Deficiency.objects.filter(
            organization_id=organization_id, pk__in=deficiency_ids, reported_at__lte=at
        ).values_list("pk", flat=True)
    )
    statuses = {}
    rows = (
        DeficiencyEvent.objects.filter(
            organization_id=organization_id,
            deficiency_id__in=reported,
            recorded_at__lte=at,
            to_status__isnull=False,
        )
        .order_by("deficiency_id", "sequence")
        .values_list("deficiency_id", "to_status")[: MAX_LOCK_ROWS * 50 + 1]
    )
    rows = list(rows)
    if len(rows) > MAX_LOCK_ROWS * 50:
        raise SealLockError("too many deficiency events to check")
    for deficiency_id, status in rows:
        statuses[str(deficiency_id)] = SimpleNamespace(status=status)
    return statuses


def paper_seal_blockers(organization_id, projections, covered_pairs, *, at, **scope):
    """seal_blockers for a paper seal verification at performance instant `at` (LATE-7).

    Fails closed: blocking if a covered deficiency or suspension blocked at `at`, as recorded
    by then, or blocks now. A deficiency open then but closed since still blocks, because the
    seal could not substitute then; one still open now blocks too, because the app cannot tell
    whether it was first found after the paper check.
    """
    now_open, now_suspended = seal_blockers(
        organization_id, projections, covered_pairs, at=at, **scope
    )
    then = _statuses_at(organization_id, list(projections), at)
    then_open, then_suspended = seal_blockers(
        organization_id, then, covered_pairs, at=at, suspensions_at=at, **scope
    )
    return now_open or then_open, now_suspended or then_suspended
