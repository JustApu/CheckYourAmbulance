"""What a vehicle owes at a due instant: bindings, schedule occurrences and pinned requirements.

Everything here reads and nothing writes. A vehicle's routine inspections come from its
requirement bindings (which checklist template follows which schedule slot, effective-dated),
the schedule rule revision in force at each due instant (cya.schedules) and the checklist
resolved as of that instant (cya.checklists.overlay). The result for one due instant D is the
set of requirements that should have an effective obligation, each pinned to the exact rows it
was resolved from (SPEC 5.1, 5.7).

Resolution failures other than "no version is effective yet" propagate: a requirement that
cannot be determined is never silently treated as absent, so a broken configuration cannot
retire or skip an obligation.

Every reader takes an optional `recorded_before` instant. With it, only records made strictly
before that instant count, so the same question can be answered "as the records stood then".
That is how a change is judged against an early window (SPEC 5.7): an obligation may adopt any
change recorded before its window opened, whenever the repair happens to run.
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from cya.checklists.models import ChecklistPublication
from cya.checklists.overlay import NoEffectiveVersionError, resolve_checklist
from cya.conditions.models import ConditionalRule
from cya.kits.history import active_kits, slot_of_reference_kit
from cya.obligations.models import RequirementBinding
from cya.obligations.state import NO_KIT
from cya.operations.status import out_of_service_at
from cya.schedules.engine import windows
from cya.schedules.models import ScheduleRule

# Fixed namespace: a requirement version's id is a pure function of the rows it pins.
REQUIREMENT_NAMESPACE = UUID("c3a1d9d6-4f0e-5b0c-9d57-6f1e2a8b7c40")
CARRY_OVER = "CARRY_OVER_FROM_PRIOR_SHIFT"


@dataclass(frozen=True)
class Occurrence:
    """One scheduled routine inspection of a vehicle: a template following a slot at D."""

    template_id: str
    slot: str
    binding_id: str
    schedule_rule_id: str
    due_at: datetime
    early_at: datetime
    grace_at: datetime


@dataclass(frozen=True)
class DesiredItem:
    """A requirement as it should be pinned for one item at one due instant."""

    item_id: str
    requirement_id: str
    item_def_id: str
    publication_id: str
    override_version_id: str | None
    binding_id: str
    schedule_rule_id: str
    conditional_rule_id: str | None
    slot: str
    due_at: datetime
    early_at: datetime
    grace_at: datetime
    # The kit instance this requirement is about, and the first event of its current run on
    # this vehicle and slot (SPEC 1.8.0, TIME-1a); both None for an item outside a kit.
    kit_id: str | None = None
    kit_event_id: str | None = None

    @property
    def key(self):
        return (self.item_id, self.kit_id or NO_KIT)


@dataclass(frozen=True)
class Desired:
    """Requirements by anchor key (item id, kit key): one per kit instance for a kit item."""

    items: dict[tuple[str, str], DesiredItem]


def requirement_id(organization_id, vehicle_id, item_id, pin):
    """Deterministic id of a requirement version from every row it pins."""
    parts = [organization_id, vehicle_id, item_id, *(pin[k] or "-" for k in sorted(pin))]
    return str(uuid5(REQUIREMENT_NAMESPACE, "|".join(map(str, parts))))


def inspection_key(slot, due_at, schedule_rule_id):
    """Key of one scheduled inspection as one schedule revision defines it (odometer_obligation).

    Including the schedule row means a revision that changes the same inspection's window gets
    its own odometer obligation, while the old one simply stops being effective.
    """
    return f"{slot}@{due_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}#{schedule_rule_id}"


def digest_of(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _recorded(queryset, recorded_before):
    return queryset if recorded_before is None else queryset.filter(recorded_at__lt=recorded_before)


def _binding_history(organization_id, vehicle_id, recorded_before=None):
    history = defaultdict(list)
    rows = _recorded(
        RequirementBinding.objects.filter(organization_id=organization_id, vehicle_id=vehicle_id),
        recorded_before,
    ).order_by("template_id", "slot", "revision")
    for row in rows:
        history[(str(row.template_id), row.slot)].append(row)
    return history


def _bound_at(revisions, instant):
    """The BIND revision in force at instant, else None (an UNBIND, or none yet effective)."""
    in_force = None
    for row in revisions:
        if row.effective_at <= instant:
            in_force = row
    return in_force if in_force is not None and in_force.action == "BIND" else None


def bound_template_ids(organization_id, vehicle_id, instant):
    """Template ids with a BIND revision in force for the vehicle at instant, recorded by it.

    Only bindings recorded at or before instant count, so a past answer never changes when a
    binding is added later (SPEC 19.3). Sorted for a stable order.
    """
    history = _binding_history(
        organization_id, vehicle_id, recorded_before=instant + timedelta(microseconds=1)
    )
    return sorted(
        {template for (template, _slot), rows in history.items() if _bound_at(rows, instant)}
    )


def _schedule_revisions(organization_id, slot, recorded_before=None):
    return list(
        _recorded(
            ScheduleRule.objects.filter(organization_id=organization_id, slot=slot),
            recorded_before,
        ).order_by("effective_at", "revision")
    )


def occurrences(organization_id, vehicle_id, start, end, recorded_before=None):
    """Scheduled routine inspections with due instants in closed [start, end], ordered.

    Each schedule revision governs the due instants from its own effective instant up to,
    excluding, the next revision's, so a revision published for the future never reshapes
    earlier dates. A binding must be in force at the due instant itself.
    """
    found = []
    history = _binding_history(organization_id, vehicle_id, recorded_before)
    for (template_id, slot), revisions in history.items():
        rules = _schedule_revisions(organization_id, slot, recorded_before)
        for index, rule_row in enumerate(rules):
            successor = rules[index + 1].effective_at if index + 1 < len(rules) else None
            for window in windows(rule_row.as_rule(), start, end):
                if successor is not None and window.due_at >= successor:
                    continue
                binding = _bound_at(revisions, window.due_at)
                if binding is None:
                    continue
                found.append(
                    Occurrence(
                        template_id=template_id,
                        slot=slot,
                        binding_id=str(binding.pk),
                        schedule_rule_id=str(rule_row.pk),
                        due_at=window.due_at,
                        early_at=window.early_at,
                        grace_at=window.grace_at,
                    )
                )
    return sorted(found, key=lambda o: (o.due_at, o.slot, o.template_id))


def _conditional_rules(organization_id, due_at, recorded_before=None):
    """Item id -> the carry-over rule row in force at due_at (latest effective wins)."""
    rules = {}
    rows = _recorded(
        ConditionalRule.objects.filter(
            organization_id=organization_id, rule_type=CARRY_OVER, effective_at__lte=due_at
        ),
        recorded_before,
    ).order_by("effective_at", "revision")
    for row in rows:
        item_id = (row.typed_inputs or {}).get("item_id")
        if isinstance(item_id, str):
            rules[item_id] = str(row.pk)
    return rules


def _kit_instances(organization_id, vehicle_id, requirement, due_at, early_at, slots):
    """Kit instances (with their run pins) an item inside a kit is owed for, or [None] if not in a kit.

    Read from the kit assignment history as the records stood (REG-6), never from the mutable
    kit_instance row. A kit that is not on the vehicle at due_at yields nothing, so a vehicle
    with no instance owes no obligation. An instance whose current run began at or after the
    window opened (early_at) is not owed for this inspection, so an addition or return inside an
    open window waits for the next due instant; one removed stops being owed from its removal
    (TIME-1a). The assignment history is not cut off at the freeze instant: a removal retires
    obligations due after it, even in-window.
    """
    if requirement.kit_id is None:
        return [None]
    reference = str(requirement.kit_id)
    if reference not in slots:
        slots[reference] = slot_of_reference_kit(reference)
    if slots[reference] is None:
        return []
    return [
        kit
        for kit in active_kits(organization_id, vehicle_id, slots[reference], due_at)
        if kit.since_at < early_at
    ]


def desired_at(organization_id, vehicle_id, due_at, at_due, recorded_before=None):
    """The requirements owed for the occurrences sharing one due instant.

    An occurrence whose template has no version effective yet contributes nothing: there is
    nothing to inspect. If two occurrences name the same item (and kit instance), the lowest
    (slot, template) wins, so one anchor never carries two competing requirements. An item
    inside a kit is owed once per kit instance on the vehicle (SPEC 1.8.0). Whether a template is marked
    active is deliberately not an input: it is a mutable flag with no history, so the answer
    could not be reproduced. The database instead refuses to deactivate a bound template
    (migration 0002), so a template is stopped by unbinding it, an appended and dated revision.
    """
    items = {}
    conditional = None
    slots = {}
    for occurrence in sorted(at_due, key=lambda o: (o.slot, o.template_id)):
        try:
            resolved = resolve_checklist(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                template_id=occurrence.template_id,
                as_of=due_at,
                recorded_before=recorded_before,
            )
        except NoEffectiveVersionError:
            continue
        publication = ChecklistPublication.objects.get(
            organization_id=organization_id, checklist_version_id=resolved.template_version_id
        )
        if conditional is None:
            conditional = _conditional_rules(organization_id, due_at, recorded_before)
        for resolved_item in resolved.items:
            item_id = str(resolved_item.requirement.item_id)
            for kit in _kit_instances(
                organization_id,
                vehicle_id,
                resolved_item.requirement,
                due_at,
                occurrence.early_at,
                slots,
            ):
                key = (item_id, kit.kit_id if kit else NO_KIT)
                if key in items:
                    continue
                pin = {
                    "binding": occurrence.binding_id,
                    "conditional": conditional.get(item_id),
                    "item_def": str(resolved_item.requirement.definition_id),
                    "override": resolved.override_version_id,
                    "publication": str(publication.pk),
                    "schedule": occurrence.schedule_rule_id,
                }
                if kit is not None:
                    # Only a kit item carries these pins, so a non-kit requirement's id is unchanged.
                    pin["kit"] = kit.kit_id
                    pin["kit_event"] = kit.since_event_id
                items[key] = DesiredItem(
                    item_id=item_id,
                    requirement_id=requirement_id(organization_id, vehicle_id, item_id, pin),
                    item_def_id=pin["item_def"],
                    publication_id=pin["publication"],
                    override_version_id=pin["override"],
                    binding_id=pin["binding"],
                    schedule_rule_id=pin["schedule"],
                    conditional_rule_id=pin["conditional"],
                    slot=occurrence.slot,
                    due_at=due_at,
                    early_at=occurrence.early_at,
                    grace_at=occurrence.grace_at,
                    kit_id=kit.kit_id if kit else None,
                    kit_event_id=kit.since_event_id if kit else None,
                )
    return Desired(items)


def is_exempt(organization_id, vehicle_id, due_at, now):
    """Out of service when the obligation would be generated (SPEC 5.9, OQ-31).

    A due instant already reached is judged by the status at that instant, which never changes
    afterwards, so returning to service never backfills it. A future due instant is judged by
    the status now: if the vehicle is back in service before it, the next run generates it.
    """
    reference = due_at if due_at <= now else now
    return out_of_service_at(organization_id, vehicle_id, reference) is not None
