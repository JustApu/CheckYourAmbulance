"""Validate freshly recorded, individually attributed kit-presence checks."""

from cya.authorization.backend import canonical_id
from cya.checks.models import CheckEvent, CheckRecord
from cya.seals.scope import MAX_SCOPE


def validate_kit_counts(organization_id, vehicle_id, actor_id, lines, selections, *, start, at):
    """Pair every current line with one current check made during this inspection.

    The caller holds the vehicle and all applicable item locks. A corrected check is accepted
    only when its root was also a fresh check by this actor; an invalidated or superseded check
    cannot certify a cabinet count.
    """
    if not isinstance(selections, list) or len(selections) > MAX_SCOPE:
        raise ValueError("invalid kit-count checks")
    selected = {}
    used = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {"line_id", "check_id"}:
            raise ValueError("invalid kit-count check fields")
        line_id, check_id = selection["line_id"], selection["check_id"]
        canonical_id(line_id)
        canonical_id(check_id)
        if line_id in selected or check_id in used:
            raise ValueError("duplicate kit-count check")
        selected[line_id] = check_id
        used.add(check_id)
    if set(selected) != {line.line_id for line in lines}:
        raise ValueError("each current kit-list line needs a fresh count check")
    if not selected:
        return []
    if start is None:
        raise ValueError("kit counts require a started inspection")
    checks = {
        str(row.pk): row
        for row in CheckRecord.objects.filter(
            organization_id=organization_id, pk__in=used
        ).select_related("root_check")
    }
    if set(checks) != used:
        raise ValueError("a kit-count check is unavailable")
    roots = {str(row.root_check_id) for row in checks.values()}
    invalid = set(
        str(root)
        for root in CheckEvent.objects.filter(
            organization_id=organization_id,
            root_check_id__in=roots,
            event_type="INVALIDATION",
            recorded_at__lte=at,
        ).values_list("root_check_id", flat=True)
    )
    superseded = set(
        str(check_id)
        for check_id in CheckRecord.objects.filter(
            organization_id=organization_id,
            predecessor_check_id__in=used,
            recorded_at__lte=at,
        ).values_list("predecessor_check_id", flat=True)
    )
    result = []
    for line in lines:
        check = checks[selected[line.line_id]]
        if (
            str(check.vehicle_id) != vehicle_id
            or str(check.actor_user_id) != actor_id
            or str(check.root_check.actor_user_id) != actor_id
            or str(check.kit_requirement_id) != line.line_id
            or check.kit_id is not None
            or check.outcome not in ("PASSED", "FAILED")
            or check.quantity is None
            or check.root_check.recorded_at <= start.recorded_at
            # A paper check is not fresh evidence for a live inspection (SPEC 8.0.0 LATE-7).
            or check.paper_entry_batch_id is not None
            or check.recorded_at > at
            or str(check.root_check_id) in invalid
            or str(check.pk) in superseded
        ):
            raise ValueError("a kit-count check is not current and fresh for this line")
        result.append((line, check))
    return result


def validate_kit_presence(organization_id, vehicle_id, actor_id, kits, selections, *, start, at):
    """Require a fresh per-unit presence check for every defined kit covered by a reseal."""
    if not isinstance(selections, list) or len(selections) > MAX_SCOPE:
        raise ValueError("invalid physical kit presence checks")
    selected, used = {}, set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {"kit_id", "check_id"}:
            raise ValueError("invalid physical kit presence check fields")
        kit_id, check_id = selection["kit_id"], selection["check_id"]
        canonical_id(kit_id)
        canonical_id(check_id)
        if kit_id in selected or check_id in used:
            raise ValueError("duplicate physical kit presence check")
        selected[kit_id] = check_id
        used.add(check_id)
    expected = {row.kit_id for row in kits}
    if set(selected) != expected:
        raise ValueError("each assigned physical kit needs a fresh presence check")
    if not expected:
        return []
    if start is None:
        raise ValueError("physical kit presence checks require a started inspection")
    checks = {
        str(row.pk): row
        for row in CheckRecord.objects.filter(
            organization_id=organization_id, pk__in=used
        ).select_related("root_check")
    }
    if set(checks) != used:
        raise ValueError("a physical kit presence check is unavailable")
    roots = {str(row.root_check_id) for row in checks.values()}
    invalid = {
        str(value)
        for value in CheckEvent.objects.filter(
            organization_id=organization_id,
            root_check_id__in=roots,
            event_type="INVALIDATION",
            recorded_at__lte=at,
        ).values_list("root_check_id", flat=True)
    }
    superseded = {
        str(value)
        for value in CheckRecord.objects.filter(
            organization_id=organization_id,
            predecessor_check_id__in=used,
            recorded_at__lte=at,
        ).values_list("predecessor_check_id", flat=True)
    }
    result = []
    for kit in kits:
        check = checks[selected[kit.kit_id]]
        if (
            str(check.vehicle_id) != vehicle_id
            or str(check.actor_user_id) != actor_id
            or str(check.root_check.actor_user_id) != actor_id
            or str(check.kit_id) != kit.kit_id
            or str(check.kit_assignment_event_id) != kit.assignment_event_id
            or str(check.item_id) != kit.item_id
            or not check.kit_presence
            or check.kit_requirement_id is not None
            or check.quantity not in (0, 1)
            or check.outcome not in ("PASSED", "FAILED")
            or (check.quantity == 1) != (check.outcome == "PASSED")
            or check.root_check.recorded_at <= start.recorded_at
            # A paper check is not fresh evidence for a live inspection (SPEC 8.0.0 LATE-7).
            or check.paper_entry_batch_id is not None
            or check.recorded_at > at
            or str(check.root_check_id) in invalid
            or str(check.pk) in superseded
        ):
            raise ValueError("a physical kit presence check is not current and fresh")
        result.append((kit, check))
    return result
