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
            or check.recorded_at > at
            or str(check.root_check_id) in invalid
            or str(check.pk) in superseded
        ):
            raise ValueError("a kit-count check is not current and fresh for this line")
        result.append((line, check))
    return result
