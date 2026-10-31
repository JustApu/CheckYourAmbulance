"""Compare what should exist for one vehicle and due instant with what is selected (SPEC 5.7, 5.8).

build_plan is a pure read: it returns the exact writes that bring one (vehicle, due instant)
unit up to date, and nothing when it already is. Generation and publication reconciliation
both use it ("the same reconciliation", contracts section 2), so they cannot disagree.

Judging a change against the early window. An obligation may adopt a change only if the change
was **recorded** before the obligation's window opened (SPEC 5.7: "pre-window"). Comparing the
clock with the window is not enough: a repair that runs late (a publication callback that
failed, a stale read lost in a race) must still apply a change that was made in time, and must
never apply one that was not. So for an anchor that already has a selected obligation O, the
requirement is resolved as the records stood just before the *freeze instant*

    freeze = max(O.early_at, O.basis_at)

where basis_at is the instant before which every record counted when O was resolved: O's own
creation for an obligation created from current data (so one created late is not downgraded),
or the cutoff a repair was judged against (so a late repair does not move the freeze forward
and let a later record in). The result is compared with O's requirement. Before the freeze
instant everything recorded so far counts. Anything recorded later never reaches O, so in-window
and past obligations keep their selection, and the SPEC 5.7 "apply to open obligations" path
stays absent.

Rules for an anchor (vehicle, item, D):

- No selection yet: SELECT the latest requirement, unless the vehicle is out of service
  (SPEC 5.9). This also repairs a missed generator run, for past, current and future D alike.
- A selected obligation: REPLACE or RETIRE it if its requirement as recorded before the freeze
  instant differs or no longer applies; otherwise nothing.
- A RETIRE tail and the requirement applies again (judged against the new window the same
  way): REPLACE with no determination, unless the vehicle is out of service.

Out-of-service status gates only the creation of new obligations; an existing obligation is
never retired for it (its exemption is derived from the status stream, SPEC 19.2).

An odometer obligation (ODO-2) follows its inspection: it is needed for every distinct schedule
row pinned by the item obligations that are effective once the plan is applied.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from cya.obligations.models import Obligation, ObligationSelection, OdometerObligation
from cya.obligations.requirements import (
    DesiredItem,
    desired_at,
    digest_of,
    inspection_key,
    is_exempt,
    occurrences,
)
from cya.obligations.state import NO_KIT


class RequirementRevived(RuntimeError):
    """A requirement would select an obligation that was archived under the very same pins.

    Pins include the publication, binding, schedule and rule rows, and every change appends a
    new one, so this cannot happen through the supported commands. If it ever does, the unit
    fails loudly instead of reviving history or looping on a unique-key rollback.
    """


@dataclass(frozen=True)
class Action:
    """One selection event to append for an anchor."""

    kind: str  # SELECT, REPLACE or RETIRE
    item_id: str
    kit_key: str  # the kit instance id, or NO_KIT for an item outside a kit
    tail_id: str | None
    tail_sequence: int
    old_obligation_id: str | None  # the selected obligation this event supersedes, if any
    old_requirement_id: str | None
    desired: DesiredItem | None  # None for RETIRE
    reason: str
    # The records-before instant the requirement was resolved at (None: all records, so the
    # obligation's basis is its own creation). Not part of the fingerprint: it follows the clock.
    cutoff: datetime | None = None

    @property
    def key(self):
        return (self.item_id, self.kit_key)

    def fingerprint(self):
        parts = [
            self.kind,
            self.item_id,
            self.tail_id,
            self.old_obligation_id,
            self.desired.requirement_id if self.desired else None,
        ]
        if self.kit_key != NO_KIT:  # a non-kit unit's digest, and so its job key, is unchanged
            parts.append(self.kit_key)
        return parts


@dataclass(frozen=True)
class OdometerAction:
    """An odometer obligation to create for one inspection as one schedule row defines it."""

    key: str
    slot: str
    schedule_rule_id: str
    due_at: datetime
    early_at: datetime
    grace_at: datetime


@dataclass(frozen=True)
class Plan:
    organization_id: str
    vehicle_id: str
    due_at: datetime
    actions: tuple[Action, ...]
    odometers: tuple[OdometerAction, ...]
    digest: str

    @property
    def is_empty(self):
        return not self.actions and not self.odometers

    def counts(self):
        return {
            "selected": sum(a.kind == "SELECT" for a in self.actions),
            "replaced": sum(a.kind == "REPLACE" for a in self.actions),
            "retired": sum(a.kind == "RETIRE" for a in self.actions),
            "odometer": len(self.odometers),
        }


def _tails(organization_id, vehicle_id, due_at):
    """Latest selection event per (item, kit instance) for this vehicle and due instant."""
    events = (
        ObligationSelection.objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, due_at=due_at
        )
        .select_related("selected_obligation__requirement_version__schedule_rule")
        .order_by("item_id", "kit_key", "-sequence")
    )
    tails = {}
    for event in events:
        tails.setdefault((str(event.item_id), event.kit_key), event)
    return tails


class _AsRecorded:
    """The desired requirements as the records stood before a cutoff (None: all records)."""

    def __init__(self, organization_id, vehicle_id, due_at):
        self.args = (organization_id, vehicle_id, due_at)
        self._cache = {}

    def at(self, cutoff):
        if cutoff not in self._cache:
            organization_id, vehicle_id, due_at = self.args
            found = occurrences(organization_id, vehicle_id, due_at, due_at, cutoff)
            self._cache[cutoff] = desired_at(organization_id, vehicle_id, due_at, found, cutoff)
        return self._cache[cutoff]


def _last_early_at(resolver, key):
    """Early instant of the last obligation selected for this anchor."""
    organization_id, vehicle_id, due_at = resolver.args
    item_id, kit_key = key
    event = (
        ObligationSelection.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            due_at=due_at,
            item_id=item_id,
            kit_key=kit_key,
            selected_obligation__isnull=False,
        )
        .select_related("selected_obligation")
        .order_by("-sequence")
        .first()
    )
    return event.selected_obligation.early_at


def _action_for(key, tail, resolver, now, exempt):
    """The single event this anchor needs, or None."""
    item_id, kit_key = key
    latest = resolver.at(None).items.get(key)
    if tail is None:
        if latest is not None and not exempt:
            return Action("SELECT", item_id, kit_key, None, 0, None, None, latest, "INITIAL")
        return None
    selected = tail.selected_obligation
    if selected is None:  # RETIRE tail: does the requirement apply again?
        if exempt:
            return None
        # A later removal must not hide a timely reapplication, so the window comes from the
        # latest records when they still require the item, else from the last obligation
        # that was selected for the anchor.
        early_at = latest.early_at if latest is not None else _last_early_at(resolver, key)
        cutoff = min(now + timedelta(microseconds=1), early_at)
        want = resolver.at(cutoff).items.get(key)
        if want is None:
            return None
        if want.early_at != early_at:  # the requirement as recorded then has its own window
            cutoff = min(now + timedelta(microseconds=1), want.early_at)
            want = resolver.at(cutoff).items.get(key)
            if want is None:
                return None
        return Action(
            "REPLACE",
            item_id,
            kit_key,
            str(tail.pk),
            tail.sequence,
            None,
            None,
            want,
            "REQUIREMENT_APPLIES_AGAIN",
            cutoff,
        )
    freeze = max(selected.early_at, selected.basis_at)
    cutoff = min(now + timedelta(microseconds=1), freeze)
    want = resolver.at(cutoff).items.get(key)
    current = str(selected.requirement_version_id)
    common = (item_id, kit_key, str(tail.pk), tail.sequence, str(selected.pk), current)
    if want is None:
        return Action("RETIRE", *common, None, "REQUIREMENT_REMOVED")
    if want.requirement_id != current:
        return Action("REPLACE", *common, want, "REQUIREMENT_CHANGED", cutoff)
    return None


def _refuse_revival(organization_id, vehicle_id, due_at, actions):
    wanted = {(a.item_id, a.kit_key, a.desired.requirement_id) for a in actions if a.desired}
    if not wanted:
        return
    existing = Obligation.objects.filter(
        organization_id=organization_id,
        vehicle_id=vehicle_id,
        due_at=due_at,
        requirement_version_id__in=[requirement for _, _, requirement in wanted],
    ).values_list("item_id", "kit_key", "requirement_version_id")
    clash = wanted & {(str(i), k, str(r)) for i, k, r in existing}
    if clash:
        raise RequirementRevived(
            f"{len(clash)} requirement(s) for {due_at.isoformat()} match archived obligations "
            "exactly; refusing to revive them"
        )


def _odometers_needed(organization_id, vehicle_id, due_at, tails, actions):
    """Odometer obligations missing for the inspections the item obligations will pin."""
    final = {}
    for key, tail in tails.items():
        if tail.selected_obligation is not None:
            obligation = tail.selected_obligation
            rule = obligation.requirement_version.schedule_rule
            final[key] = (rule.slot, str(rule.pk), obligation.early_at, obligation.grace_at)
    for action in actions:
        desired = action.desired
        if desired is None:
            final.pop(action.key, None)
        else:
            final[action.key] = (
                desired.slot,
                desired.schedule_rule_id,
                desired.early_at,
                desired.grace_at,
            )
    needed = {}
    for slot, rule_id, early_at, grace_at in final.values():
        key = inspection_key(slot, due_at, rule_id)
        needed.setdefault(key, OdometerAction(key, slot, rule_id, due_at, early_at, grace_at))
    if not needed:
        return ()
    have = set(
        OdometerObligation.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            scheduled_inspection_key__in=list(needed),
        ).values_list("scheduled_inspection_key", flat=True)
    )
    return tuple(needed[key] for key in sorted(needed) if key not in have)


def build_plan(organization_id, vehicle_id, due_at, now):
    """The writes needed for one (vehicle, due instant) unit, evaluated at instant `now`."""
    organization_id, vehicle_id = str(organization_id), str(vehicle_id)
    resolver = _AsRecorded(organization_id, vehicle_id, due_at)
    latest = resolver.at(None)
    tails = _tails(organization_id, vehicle_id, due_at)
    exempt = (
        is_exempt(organization_id, vehicle_id, due_at, now) if (latest.items or tails) else False
    )

    actions = []
    for key in sorted(set(latest.items) | set(tails)):
        action = _action_for(key, tails.get(key), resolver, now, exempt)
        if action is not None:
            actions.append(action)
    _refuse_revival(organization_id, vehicle_id, due_at, actions)
    odometers = _odometers_needed(organization_id, vehicle_id, due_at, tails, actions)

    fingerprint = {
        "organization": organization_id,
        "vehicle": vehicle_id,
        "due_at": due_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "actions": [a.fingerprint() for a in actions],
        "odometers": [o.key for o in odometers],
    }
    return Plan(
        organization_id, vehicle_id, due_at, tuple(actions), odometers, digest_of(fingerprint)
    )
