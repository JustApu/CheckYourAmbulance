"""The obligations-stop preview shown before a template is unbound from a vehicle (SPEC CFG-8).

A pure read. At the unbinding instant `at` it lists the binding's selected routine obligations
due at or after `at`, split the way reconciliation will treat them once the UNBIND is recorded:

- **superseded**: the early window has not opened (early_at > at). The planner's freeze instant
  is max(early_at, basis_at), and basis_at never exceeds the instant an obligation was recorded,
  so for these it is early_at: the UNBIND is recorded before it and they are retired as
  SUPERSEDED (SPEC 5.7). No check can have satisfied them, because a check counts only from
  early_at.
- **continuing**: the window has opened. They keep their ordinary state and stay owed.

The digest covers the binding revision and both lists, including each continuing obligation's
state, so a confirmation recomputed later refuses when time passed across a window opening, an
obligation was generated or completed, or the binding changed (AC-37(6)).
"""

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass

from cya.obligations.current import selected_ids
from cya.obligations.models import Obligation, RequirementBinding
from cya.obligations.projections import get_vehicle_obligations_as_of

# NFR-6. Generation creates obligations only up to its horizon, so a binding's future
# obligations are few (items x occurrences in the horizon). Past this many the preview refuses
# rather than show a partial list that the confirmation could not match.
MAX_PREVIEW_OBLIGATIONS = 2000


def legacy_templates_of(vehicle_id, template_id):
    # Imported late: cya.obligations.legacy imports the requirement resolver.
    from cya.obligations.legacy import legacy_templates_of as found

    return found(vehicle_id, template_id)


class PreviewTooLarge(Exception):
    """More obligations than one preview may list."""


@dataclass(frozen=True)
class Occurrence:
    """One due instant of the binding: its window opening and the obligations per state."""

    due_at: object
    early_at: object
    states: tuple  # ((state label, count), ...) in display order
    count: int


@dataclass(frozen=True)
class UnbindPreview:
    binding: RequirementBinding | None  # the latest revision of the chain, if any
    at: object
    superseded: tuple  # Occurrence
    continuing: tuple  # Occurrence
    superseded_count: int
    continuing_count: int
    token: str

    @property
    def bound(self):
        return self.binding is not None and self.binding.action == "BIND"


def latest_revision(organization_id, vehicle_id, template_id, slot):
    return (
        RequirementBinding.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            template_id=template_id,
            slot=slot,
        )
        .order_by("-revision")
        .first()
    )


def _group(rows):
    """[(due_at, early_at, state)] -> Occurrences in due order."""
    grouped = OrderedDict()
    for due_at, early_at, state in sorted(rows, key=lambda r: (r[0], r[1], r[2])):
        counts = grouped.setdefault((due_at, early_at), OrderedDict())
        counts[state] = counts.get(state, 0) + 1
    return tuple(
        Occurrence(due_at, early_at, tuple(counts.items()), sum(counts.values()))
        for (due_at, early_at), counts in grouped.items()
    )


def unbind_preview(organization_id, vehicle_id, template_id, slot, at):
    """What unbinding (vehicle, template, slot) at `at` would supersede and leave owed."""
    binding = latest_revision(organization_id, vehicle_id, template_id, slot)
    candidates = list(
        Obligation.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            routine=True,
            due_at__gte=at,
            recorded_at__lte=at,
            # A migrated assignment also stops the obligations still pinned to the legacy
            # binding it was copied from (SPEC 5.0.0 18.2 step 3, cya.obligations.legacy).
            requirement_version__binding__template_id__in=[
                str(template_id),
                *legacy_templates_of(vehicle_id, template_id),
            ],
            requirement_version__binding__slot=slot,
        )
        .order_by("due_at", "id")
        .values_list("id", "due_at", "early_at")[: MAX_PREVIEW_OBLIGATIONS + 1]
    )
    if len(candidates) > MAX_PREVIEW_OBLIGATIONS:
        raise PreviewTooLarge(
            f"more than {MAX_PREVIEW_OBLIGATIONS} obligations would be affected; "
            "unbind from the command line"
        )
    chosen = {str(i) for i in selected_ids(organization_id, [c[0] for c in candidates], at)}
    rows = [(str(i), due, early) for i, due, early in candidates if str(i) in chosen]
    superseded = [(i, due, early) for i, due, early in rows if early > at]
    continuing = [(i, due, early) for i, due, early in rows if early <= at]
    states = {}
    if continuing:
        wanted = {i for i, _, _ in continuing}
        for state in get_vehicle_obligations_as_of(
            vehicle_id,
            at,
            organization_id=organization_id,
            start=at,
            end=max(due for _, due, _ in continuing),
        ):
            if str(state.obligation.id) in wanted:
                states[str(state.obligation.id)] = state.state.value
    continuing_states = [(i, due, early, states.get(i, "OWED")) for i, due, early in continuing]
    token = hashlib.sha256(
        json.dumps(
            {
                "binding": str(binding.pk) if binding else None,
                "superseded": sorted(i for i, _, _ in superseded),
                "continuing": sorted([i, s] for i, _, _, s in continuing_states),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return UnbindPreview(
        binding=binding,
        at=at,
        superseded=_group([(due, early, "SUPERSEDED") for _, due, early in superseded]),
        continuing=_group([(due, early, s) for _, due, early, s in continuing_states]),
        superseded_count=len(superseded),
        continuing_count=len(continuing_states),
        token=token,
    )
