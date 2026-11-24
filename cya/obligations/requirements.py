"""What a vehicle owes at a due instant: bindings, schedule occurrences and pinned requirements.

Everything here reads and nothing writes. A vehicle's routine inspections come from its
requirement bindings (which checklist follows which schedule slot, effective-dated; since SPEC
5.0.0 a binding of a vehicle checklist is its schedule assignment, and a legacy template's
bindings govern only before the organization's cutover),
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
from cya.checklists.ownership import cutover_at, governs, owners, precedence_key
from cya.conditions.models import ConditionalRule
from cya.kits.history import active_kits, active_kits_on_vehicle, slot_of_reference_kit
from cya.kits.kit_lists import kit_list_effective_at
from cya.kits.models import (
    ChecklistKitPlacement,
    KitDefinitionItem,
    KitDefinitionPublication,
    KitInstanceDefinition,
    VehicleKitRequirement,
)
from cya.kits.presence import line_unit_key
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
    # SPEC 5.0.0 CHK-10: a vehicle checklist's stable precedence ordinal; None for a legacy
    # template, which is ordered by its identity (template_id) as before.
    precedence: int | None = None

    @property
    def order(self):
        """CHK-10: lowest slot text value (code-point order), then checklist precedence."""
        return (self.slot, precedence_key(self.template_id, self.precedence))


@dataclass(frozen=True)
class DesiredItem:
    """A requirement as it should be pinned for one item at one due instant."""

    item_id: str
    requirement_id: str
    item_def_id: str | None  # None for content from a placed kit definition (issue #129)
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
    # Content from a kit definition placed by the checklist (SPEC 4.6 KDEF-4); both None for a
    # requirement from a checklist item definition.
    kit_definition_item_id: str | None = None
    placement_id: str | None = None
    # A kit presence requirement (SPEC 2.0.0 KDEF-10 to KDEF-12): its kit list line and that
    # line's unit key, which stands in for the kit key of the obligation stream.
    kit_requirement_id: str | None = None
    kit_presence: bool = False
    unit_key: str | None = None
    # SPEC 6.0.0 CHK-11: an item outside a kit after the item-requirement cutover is owed per
    # vehicle item requirement. Its requirement id is the unit key (one stream per stock, which
    # a move keeps), and the requirement version in force at the due instant is pinned.
    item_requirement_version_id: str | None = None

    @property
    def key(self):
        return (self.item_id, self.kit_id or self.unit_key or NO_KIT)


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


class _Governing:
    """Which bindings may govern a due instant, as the records stood (SPEC 5.0.0 18.2).

    A legacy template's binding governs only before the organization's cutover and a vehicle
    checklist's assignment only from it on (cya.checklists.ownership.governs). The cutover is
    read with the same recorded_before bound as the bindings, so a question answered as the
    records stood before the cutover still gets the legacy answer.
    """

    def __init__(self, organization_id, template_ids, recorded_before=None):
        self.cutover = cutover_at(organization_id, recorded_before)
        self.owners = owners(template_ids)

    def allows(self, template_id, due_at):
        owner, _precedence = self.owners.get(str(template_id), (None, None))
        return governs(owner, due_at, self.cutover)

    def precedence(self, template_id):
        return self.owners.get(str(template_id), (None, None))[1]


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
    recorded_before = instant + timedelta(microseconds=1)
    history = _binding_history(organization_id, vehicle_id, recorded_before=recorded_before)
    governing = _Governing(
        organization_id, {template for template, _slot in history}, recorded_before
    )
    return sorted(
        {
            template
            for (template, _slot), rows in history.items()
            if _bound_at(rows, instant) and governing.allows(template, instant)
        }
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
    earlier dates. A binding must be in force at the due instant itself, and allowed to govern
    it on its side of the organization's cutover (SPEC 5.0.0 18.2).
    """
    found = []
    history = _binding_history(organization_id, vehicle_id, recorded_before)
    governing = _Governing(
        organization_id, {template for template, _slot in history}, recorded_before
    )
    for (template_id, slot), revisions in history.items():
        rules = _schedule_revisions(organization_id, slot, recorded_before)
        for index, rule_row in enumerate(rules):
            successor = rules[index + 1].effective_at if index + 1 < len(rules) else None
            for window in windows(rule_row.as_rule(), start, end):
                if successor is not None and window.due_at >= successor:
                    continue
                binding = _bound_at(revisions, window.due_at)
                if binding is None or not governing.allows(template_id, window.due_at):
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
                        precedence=governing.precedence(template_id),
                    )
                )
    return sorted(found, key=lambda o: (o.due_at, o.order))


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


def _definition_version(organization_id, definition_id, due_at, recorded_before):
    """The kit definition version in force at due_at, as the records stood (KDEF-4, §5.7)."""
    publications = KitDefinitionPublication.objects.filter(
        organization_id=organization_id,
        version__definition_id=definition_id,
        effective_at__lte=due_at,
    )
    if recorded_before is not None:
        publications = publications.filter(recorded_at__lt=recorded_before)
    latest = publications.order_by("-effective_at").values_list("version_id", flat=True).first()
    return str(latest) if latest else None


def _placed_kits(organization_id, vehicle_id, placement, due_at, early_at, recorded_before=None):
    """Instances of the placed definition owed for this inspection (TIME-1a, KDEF-3, KDEF-4).

    Every instance with the placed definition that is on the vehicle at D, in any compartment
    (SPEC 2.0.0 KDEF-4), read from the assignment history like any kit item (not cut off at the
    freeze instant). An instance whose stay on the vehicle began after the window opened waits
    for the next due instant.
    """
    kits = [
        kit
        for kit in active_kits_on_vehicle(organization_id, vehicle_id, due_at)
        if kit.since_at < early_at
    ]
    if not kits:
        return []
    having = set(
        KitInstanceDefinition.objects.filter(
            kit_id__in=[k.kit_id for k in kits], definition_id=placement.definition_id
        ).values_list("kit_id", flat=True)
    )
    return [kit for kit in kits if kit.kit_id in {str(k) for k in having}]


def _kit_list_lines(organization_id, vehicle_id, due_at, recorded_before):
    """No new inspection expectations come from the legacy expected-count list.

    Historical list-based obligations and checks remain stored and readable. Future kit contents
    are generated per physical unit from assignment history in _placed_kits.
    """
    return []


def _presence_items(
    organization_id, vehicle_id, occurrence, publication, resolved, placement, _lines, context
):
    """One expected presence check for each assigned physical kit in a placed definition.

    The requirement stream follows the kit instance and its uninterrupted assignment event. Stock
    kits are not on the vehicle history and therefore create no expectation.
    """
    due_at, recorded_before, conditional = context
    contents = active_kits_on_vehicle(
        organization_id, vehicle_id, due_at, recorded_before=recorded_before
    )
    assigned = _placed_kits(
        organization_id, vehicle_id, placement, due_at, occurrence.early_at, recorded_before
    )
    for kit in assigned:
        item_id = str(placement.definition.presence_item_id)
        pin = {
            "binding": occurrence.binding_id,
            "conditional": conditional.get(item_id),
            "kit_presence": kit.kit_id,
            "kit_assignment": kit.event_id,
            "placement": str(placement.pk),
            "override": resolved.override_version_id,
            "publication": str(publication.pk),
            "schedule": occurrence.schedule_rule_id,
        }
        yield (
            (item_id, kit.kit_id),
            DesiredItem(
                item_id=item_id,
                requirement_id=requirement_id(organization_id, vehicle_id, item_id, pin),
                item_def_id=None,
                publication_id=pin["publication"],
                override_version_id=pin["override"],
                binding_id=pin["binding"],
                schedule_rule_id=pin["schedule"],
                conditional_rule_id=pin["conditional"],
                slot=occurrence.slot,
                due_at=due_at,
                early_at=occurrence.early_at,
                grace_at=occurrence.grace_at,
                kit_id=kit.kit_id,
                kit_event_id=kit.event_id,
                kit_presence=True,
                placement_id=pin["placement"],
            ),
        )


def _placement_items(organization_id, vehicle_id, occurrence, publication, resolved, context):
    """Requirements for the content of kits placed by definition (SPEC 4.6 KDEF-4, KDEF-7).

    One per content entry of the definition version in force at D, per instance of that
    definition on the vehicle. A version published later never changes these pins.
    """
    due_at, recorded_before, conditional = context
    placements = ChecklistKitPlacement.objects.filter(
        checklist_version_id=resolved.template_version_id
    ).order_by("display_order", "id")
    lines = _kit_list_lines(organization_id, vehicle_id, due_at, recorded_before)
    for placement in placements:
        yield from _presence_items(
            organization_id,
            vehicle_id,
            occurrence,
            publication,
            resolved,
            placement,
            lines,
            context,
        )
        version_id = _definition_version(
            organization_id, placement.definition_id, due_at, recorded_before
        )
        if version_id is None:
            continue  # nothing published yet for D: nothing to inspect
        kits = _placed_kits(organization_id, vehicle_id, placement, due_at, occurrence.early_at, recorded_before)
        if not kits:
            continue
        contents = KitDefinitionItem.objects.filter(version_id=version_id).order_by(
            "display_order", "id"
        )
        for content in contents:
            item_id = str(content.item_id)
            for kit in kits:
                pin = {
                    "binding": occurrence.binding_id,
                    "conditional": conditional.get(item_id),
                    "kit_def_item": str(content.pk),
                    "placement": str(placement.pk),
                    "override": resolved.override_version_id,
                    "publication": str(publication.pk),
                    "schedule": occurrence.schedule_rule_id,
                    "kit": kit.kit_id,
                    "kit_event": kit.since_event_id,
                }
                yield (
                    (item_id, kit.kit_id),
                    DesiredItem(
                        item_id=item_id,
                        requirement_id=requirement_id(organization_id, vehicle_id, item_id, pin),
                        item_def_id=None,
                        publication_id=pin["publication"],
                        override_version_id=pin["override"],
                        binding_id=pin["binding"],
                        schedule_rule_id=pin["schedule"],
                        conditional_rule_id=pin["conditional"],
                        slot=occurrence.slot,
                        due_at=due_at,
                        early_at=occurrence.early_at,
                        grace_at=occurrence.grace_at,
                        kit_id=kit.kit_id,
                        kit_event_id=kit.since_event_id,
                        kit_definition_item_id=pin["kit_def_item"],
                        placement_id=pin["placement"],
                    ),
                )


def desired_at(organization_id, vehicle_id, due_at, at_due, recorded_before=None):
    """The requirements owed for the occurrences sharing one due instant.

    An occurrence whose template has no version effective yet contributes nothing: there is
    nothing to inspect. If two occurrences name the same item (and kit instance), the lowest
    (slot, checklist precedence) wins, so one anchor never carries two competing requirements
    (SPEC 5.0.0 CHK-10: slot text in code-point order, then the vehicle checklist's precedence
    ordinal, or a legacy template's identity). An item
    inside a kit is owed once per kit instance on the vehicle (SPEC 1.8.0). Whether a template is marked
    active is deliberately not an input: it is a mutable flag with no history, so the answer
    could not be reproduced. The database instead refuses to deactivate a bound template
    (migration 0002), so a template is stopped by unbinding it, an appended and dated revision.
    """
    items = {}
    conditional = None
    slots = {}
    for occurrence in sorted(at_due, key=lambda o: o.order):
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
                requirement = resolved_item.requirement
                unit = requirement.item_requirement_id if kit is None else None
                key = (item_id, kit.kit_id if kit else (unit or NO_KIT))
                if key in items:
                    continue  # CHK-10: per (item requirement or item, kit) and due instant
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
                if unit:
                    # Only a row of an item requirement carries this pin (SPEC 6.0.0 CHK-11).
                    pin["item_requirement"] = requirement.item_requirement_version_id
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
                    unit_key=unit,
                    item_requirement_version_id=pin.get("item_requirement"),
                )
        placed = _placement_items(
            organization_id,
            vehicle_id,
            occurrence,
            publication,
            resolved,
            (due_at, recorded_before, conditional),
        )
        for key, desired in placed:
            items.setdefault(key, desired)  # an earlier (slot, template) or item wins
    return Desired(items)


def is_exempt(organization_id, vehicle_id, due_at, now):
    """Out of service or inactive when the obligation would be generated (SPEC 5.9, 4.8 CFG-1).

    A due instant already reached is judged by the status at that instant, which never changes
    afterwards, so returning to service never backfills it. A future due instant is judged by
    the status now: if the vehicle is back in service before it, the next run generates it.
    """
    reference = due_at if due_at <= now else now
    if out_of_service_at(organization_id, vehicle_id, reference) is not None:
        return True
    # SPEC 4.8 CFG-1: an inactive vehicle is exempt the same way, so no unit is selected while
    # it is inactive and reactivation never backfills one.
    from cya.operations.vehicles import inactive_at

    return inactive_at(organization_id, vehicle_id, reference) is not None
