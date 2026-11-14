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
