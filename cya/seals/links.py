"""Select obligation links for explicitly inspected or seal-certified items."""

from dataclasses import dataclass
from types import SimpleNamespace

from cya.checks.linking import choose_links
from cya.seals.scope import MAX_SCOPE

MAX_LINKS = 10000


@dataclass(frozen=True)
class LinkTarget:
    item_id: str
    kit_id: str | None = None
    kit_requirement_id: str | None = None


def plan_links(organization_id, vehicle_id, targets, *, recorded_at):
    """Select the same on-time and explicitly shared obligations as a normal check.

    The caller holds each target's item lock and uses this instant on the receipt. Presence
    lines name their pinned line separately so two lines of the same definition stay distinct.
    """
    if not isinstance(targets, (tuple, list)) or len(targets) > MAX_SCOPE:
        raise ValueError("invalid or oversized seal link scope")
    seen = set()
    planned = []
    total = 0
    for target in targets:
        if not isinstance(target, LinkTarget):
            raise ValueError("invalid seal link target")
        key = (target.item_id, target.kit_id, target.kit_requirement_id)
        if key in seen or (target.kit_id is not None and target.kit_requirement_id is not None):
            raise ValueError("duplicate or incompatible seal link target")
        seen.add(key)
        prospective = SimpleNamespace(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            item_id=target.item_id,
            kit_id=target.kit_id,
            kit_requirement_id=target.kit_requirement_id,
        )
        links = choose_links(prospective, recorded_at=recorded_at)
        total += len(links)
        if total > MAX_LINKS:
            raise ValueError("too many seal obligation links")
        planned.append((target, links))
    return planned
