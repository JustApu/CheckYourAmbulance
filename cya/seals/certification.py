"""Compare a seal's recorded certification with its current physical scope."""

from dataclasses import dataclass

from cya.kits.seal_rules import CertifiedKitCount, kit_count_refusal
from cya.seals.scope import MAX_SCOPE


@dataclass(frozen=True)
class CertifiedItem:
    item_id: str
    kit_id: str | None
    source: str  # CHECKLIST or KIT_DEFINITION
    definition_id: str


@dataclass(frozen=True)
class StoredCertification:
    """Saved inspection facts; nested kit attestations still need current verification."""

    items: tuple[CertifiedItem, ...]
    lines: tuple[CertifiedKitCount, ...]
    nested: tuple[tuple[str, str], ...]  # kit and prior substitution IDs


def _bounded(query):
    rows = tuple(query[: MAX_SCOPE + 1])
    if len(rows) > MAX_SCOPE:
        raise ValueError("stored seal certification is too large")
    return rows


def stored_certification(organization_id, seal_event):
    """Read the full certification of one reseal without silently truncating it."""
    from cya.seals.models import (
        SealInspection,
        SealInspectionItem,
        SealInspectionKitCount,
        SealInspectionKitInstance,
        SealInspectionKitVerification,
    )

    inspection = SealInspection.objects.filter(
        organization_id=organization_id, reseal=seal_event
    ).first()
    if inspection is None:
        raise ValueError("the seal has no full inspection")
    rows = _bounded(
        SealInspectionItem.objects.filter(
            organization_id=organization_id, inspection=inspection
        ).order_by("pk")
    )
    items = tuple(
        CertifiedItem(
            str(row.item_id),
            str(row.kit_id) if row.kit_id else None,
            "KIT_DEFINITION" if row.kit_definition_item_id else "CHECKLIST",
            str(row.kit_definition_item_id or row.item_def_id),
        )
        for row in rows
    )
    counts = _bounded(
        SealInspectionKitCount.objects.filter(
            organization_id=organization_id, inspection=inspection
        ).order_by("pk")
    )
    assignments = _bounded(
        SealInspectionKitInstance.objects.filter(
            organization_id=organization_id, kit_count_id__in=[row.pk for row in counts]
        ).order_by("kit_count_id", "kit_id")
    )
    by_count = {}
    for assignment in assignments:
        by_count.setdefault(str(assignment.kit_count_id), []).append(
            (str(assignment.kit_id), str(assignment.assignment_id))
        )
    lines = tuple(
        CertifiedKitCount(
            str(row.kit_requirement_id), row.count, tuple(by_count.get(str(row.pk), ()))
        )
        for row in counts
    )
    nested = _bounded(
        SealInspectionKitVerification.objects.filter(
            organization_id=organization_id, inspection=inspection
        )
        .order_by("kit_id")
        .values_list("kit_id", "substitution_id")
    )
    return StoredCertification(
        items, lines, tuple((str(kit), str(substitution)) for kit, substitution in nested)
    )


def certification_refusal(scope, certified_items, lines, certified_lines, *, nested_kit_ids=()):
    """Return a conflict code if the saved inspection no longer covers the current scope.

    The caller has independently reverified every nested kit and supplies its physical id;
    nesting does not bypass a change to the cabinet's certified kit-list lines or assignments.
    """
    for rows in (scope, certified_items, lines, certified_lines, nested_kit_ids):
        if not isinstance(rows, (tuple, list)) or len(rows) > MAX_SCOPE:
            raise ValueError("invalid or oversized seal certification")
    nested = set(nested_kit_ids)
    if len(nested) != len(nested_kit_ids) or any(not isinstance(k, str) or not k for k in nested):
        raise ValueError("invalid nested kit list")
    expected = {}
    seen = set()
    for item in scope:
        if item.key in seen:
            raise ValueError("duplicate current physical item")
        seen.add(item.key)
        if item.kit_id in nested:
            continue
        source = "KIT_DEFINITION" if item.source == "KIT_DEFINITION" else "CHECKLIST"
        expected[item.key] = (source, str(item.definition_id))
    recorded = {}
    for item in certified_items:
        if not isinstance(item, CertifiedItem):
            raise ValueError("invalid certified item")
        key = (item.item_id, item.kit_id)
        if key in recorded:
            raise ValueError("duplicate certified physical item")
        recorded[key] = (item.source, item.definition_id)
    if expected != recorded:
        return "ITEM_SCOPE_CHANGED"
    old_lines = {}
    for line in certified_lines:
        if not isinstance(line, CertifiedKitCount) or line.line_id in old_lines:
            raise ValueError("invalid or duplicate certified kit line")
        old_lines[line.line_id] = line
    if len({line.line_id for line in lines}) != len(lines):
        raise ValueError("duplicate current kit line")
    if set(old_lines) != {line.line_id for line in lines}:
        return "KIT_SCOPE_CHANGED"
    for line in lines:
        result = kit_count_refusal(
            old_lines[line.line_id],
            line_id=line.line_id,
            expected_count=line.required_count,
            current_assignments=line.assignments,
        )
        if result is not None:
            return result
    return None
