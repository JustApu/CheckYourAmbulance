"""The per-vehicle checklist query behind the crew screen (SPEC 6.5 VW-1..VW-3, 7.5, 19.4).

vehicle_checklist returns one row per checklist item, and per kit instance for an item inside a
kit, with its obligation state, item attention state and the unresolved deficiencies that
concern it, plus the vehicle's operational status. Everything is derived from authoritative
events as of an instant, so the same call at the same instant gives the same answer after later
events (SPEC 19.3). Nothing here writes, and the stored view preference is not applied: the
caller passes the view and filter (the HTTP layer defaults them from the user's preference).

Views. Normal is the checklist's own order (template order, then display order). Priority puts
items with Attention first, then items that need action (Overdue, Due, Current), then the rest,
each group in Normal order.

Each row shows its most urgent obligation in the window. An obligation still unsatisfied after
its late-completion limit L (G plus the w_late pinned when it was created; SPEC SCH-2, issue
#80) is missed: no check can complete
it, so it stays in history and reports but no longer decides the row, which shows the item's
next obligation instead. Until L it shows Overdue and stays actionable.

Needs Attention Only keeps a row that is red, or whose obligation still needs action: Overdue,
Due or Current ("actionable due, overdue, incomplete"). Upcoming, Satisfied, Exempt and Waived
rows are not actionable. A row whose routine check a supervisor suspended for a known deficiency
(SPEC 8.9) shows obligation state SUSPENDED ("Known Deficiency — Check Suspended"): it is never
actionable and, while that suspension is in force, is left out of Needs Attention Only even though
its deficiency makes it red (VW-2, AC-18), but it stays in the full checklist with its deficiency
listed. An obligation that stays Suspended in history after its suspension ended ranks last, so the
row shows its current obligation again. A row still shows Overdue, Due or Current, and stays in the
filter, when another of its obligations needs action, such as one missed before the suspension
started and still within its late-completion limit. Suspensions are read from storage by the
obligation reader; ``suspension_lookup`` replaces that evidence when supplied.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from django.utils import timezone

from cya.attention.facts import deficiency_facts, latest_effective_checks
from cya.attention.state import Attention, applies, attention_states, latest_checks
from cya.authorization.backend import canonical_id
from cya.checklists.models import ChecklistTemplate, Compartment, Item, KitInstance, Vehicle
from cya.checklists.overlay import (
    NoEffectiveVersionError,
    ResolutionError,
    _active_deviations,
    resolve_checklist,
)
from cya.deficiencies.models import Deficiency, DeficiencyEvent
from cya.kits.history import active_kits, slot_of_reference_kit
from cya.kits.models import ChecklistKitPlacement, KitDefinition
from cya.kits.placements import placed_items
from cya.kits.presence import (
    INSTANCE_PRESENCE_SOURCE,
    PRESENCE_SOURCE,
    instance_presence_resolved_item,
    line_unit_keys,
    presence_resolved_item,
)
from cya.obligations.models import Obligation, RequirementVersion
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.requirements import bound_template_ids
from cya.obligations.state import NO_KIT, State
from cya.operations.status import status_at

NORMAL, PRIORITY = "NORMAL", "PRIORITY"
# Most urgent first; the first state found for a row is the one shown.
STATE_RANK = (
    State.OVERDUE,
    State.DUE,
    State.CURRENT,
    State.SUSPENDED,
    State.EXEMPT,
    State.WAIVED,
    State.SATISFIED,
    State.UPCOMING,
)
ACTIONABLE = (State.OVERDUE, State.DUE, State.CURRENT)


class ChecklistQueryError(ValueError):
    """The request cannot be answered; code is the stable wire code."""

    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class ChecklistRow:
    item_id: str
    item_label: str
    kit_id: str | None
    kit_label: str | None
    definition_id: str
    kind: str
    instructions: str
    compartment_id: str | None
    compartment_label: str | None
    system_id: str | None
    system_label: str | None
    display_order: int
    obligation_state: str | None
    due_at: datetime | None
    attention: Attention
    needs_action: bool
    deficiencies: tuple = field(default=())
    # What a crew screen needs to record a check without a second lookup (issue 85 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/85)).
    checklist_version_id: str = ""
    expected_quantity: int | None = None
    unit: str | None = None
    allowed_outcomes: tuple = ()
    # The newest effective check of this item (and kit instance), red or not: the basis a
    # client sends as basis_check_id (SPEC 7.4 CON-3). attention.check_id is set only when red.
    latest_check_id: str | None = None
    latest_check_outcome: str | None = None
    latest_check_at: datetime | None = None
    obligation_outcome: str | None = None
    # When the shown obligation was satisfied (its satisfying check or supervisor completion);
    # a newer check is in the same window (issue #140).
    obligation_completed_at: datetime | None = None
    # The shown obligation is under a supervisor check suspension still in force (SPEC 8.9).
    check_suspended: bool = False
    # Legacy kit-list presence row; current physical presence rows use kit_presence and kit_id.
    kit_requirement_id: str | None = None
    kit_presence: bool = False
    # SPEC 6.0.0 CHK-11: the item requirement of a loose item's row after the cutover (one
    # stock in one place); with the item it identifies the row.
    item_requirement_id: str | None = None
    # SPEC 5.0.0 CHK-9: a checklist item with no position. It is still due and checked, and is
    # listed under "Not placed", never under a compartment or system.
    not_placed: bool = False


@dataclass(frozen=True)
class VehicleChecklist:
    vehicle_id: str
    as_of: datetime
    view: str
    needs_attention_only: bool
    vehicle_status: dict
    rows: tuple
    group_progress: tuple


@dataclass(frozen=True)
class _Pin:
    """A session's checklist, resolved as the session opened with it (SPEC CHK-4, 5.7.1).

    resolved is the ResolvedChecklist a check recorded in the session is judged against
    (cya.checks.commands._resolved_item): as of the session's opening, with the publications
    recorded by then. Placed kit content uses the same instants; everything else about a row
    (obligations, attention, checks, kits present, kit list lines) is read as of the query.
    """

    resolved: object
    opened_at: datetime
    recorded_before: datetime


def _session_pin(organization_id, vehicle_id, session, instant):
    """The session's _Pin. Only an active stocking deviation is read as of instant, so a count's
    target is the one a check recorded now is judged against (cya.checks.commands._resolved_item).
    """
    if str(session.vehicle_id) != str(vehicle_id):
        raise ChecklistQueryError("NOT_FOUND", "session was not found", 404)
    recorded_before = session.recorded_at + timedelta(microseconds=1)
    try:
        resolved = resolve_checklist(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            template_id=session.checklist_version.template_id,
            as_of=session.opened_at,
            recorded_before=recorded_before,
        )
    except ResolutionError as error:
        raise ChecklistQueryError("CHECKLIST_UNAVAILABLE", str(error), 409) from None
    if str(resolved.template_version_id) != str(session.checklist_version_id):
        raise ChecklistQueryError(
            "CHECKLIST_UNAVAILABLE", "the session's checklist version cannot be resolved", 409
        )
    counted = [i.requirement.item_id for i in resolved.items if i.requirement.kind == "QUANTITY"]
    if counted:
        deviations = _active_deviations(organization_id, vehicle_id, counted, instant)

        def current_target(item):
            if item.requirement.kind != "QUANTITY":
                return item
            deviation = deviations.get(item.requirement.item_id)
            return replace(
                item,
                effective_quantity=(
                    deviation.alternate_quantity
                    if deviation
                    else item.requirement.expected_quantity
                ),
                deviation_id=deviation.pk if deviation else None,
            )

        resolved = replace(resolved, items=tuple(current_target(i) for i in resolved.items))
    return _Pin(resolved, session.opened_at, recorded_before)


def _items_of(order, resolved):
    """(order, item id, ResolvedItem, version id) per item of one resolved checklist."""
    chosen, seen = [], set()
    for item in resolved.items:
        item_id = str(item.requirement.item_id)
        if item_id not in seen:
            seen.add(item_id)
            chosen.append((order, item_id, item, str(resolved.template_version_id)))
    return chosen


def _resolved_items(organization_id, vehicle_id, as_of, pin=None):
    """(template order, item id, ResolvedItem, version id) per item of each bound checklist.

    Only templates with a BIND in force for this vehicle at as_of apply (a template published
    for another vehicle does not), and only publications and bindings recorded by as_of count,
    so a past answer is reproducible. An item two checklists both list appears once for each
    (SPEC 6.0.0 VW-5), in template order; _one_per_key keeps the first for a vehicle-wide read.
    With a pin, only the session's own checklist applies, whatever is bound now.
    """
    if pin is not None:
        return _items_of(0, pin.resolved)
    chosen = []
    bound = set(bound_template_ids(organization_id, vehicle_id, as_of))
    templates = ChecklistTemplate.objects.filter(organization_id=organization_id).order_by("pk")
    for order, template in enumerate(t for t in templates if str(t.pk) in bound):
        try:
            resolved = resolve_checklist(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                template_id=template.pk,
                as_of=as_of,
                recorded_before=as_of + timedelta(microseconds=1),
            )
        except NoEffectiveVersionError:
            continue
        except ResolutionError as error:
            raise ChecklistQueryError("CHECKLIST_UNAVAILABLE", str(error), 409) from None
        chosen.extend(_items_of(order, resolved))
    return chosen


def _first_resolved_items(organization_id, vehicle_id, as_of):
    """{item id: (template order, ResolvedItem, version id)}, the first template by id winning.

    The one-entry-per-item view of _resolved_items, for callers that pin an item to a single
    checklist version (ad hoc inspections, cya.obligations.adhoc).
    """
    chosen = {}
    for order, item_id, item, version_id in _resolved_items(organization_id, vehicle_id, as_of):
        chosen.setdefault(item_id, (order, item, version_id))
    return chosen


def _is_kit(unit, requirement):
    """Whether a row's unit is a kit instance (not a presence line or an item requirement)."""
    return bool(
        unit
        and requirement.source != PRESENCE_SOURCE
        and str(unit) != str(getattr(requirement, "item_requirement_id", None) or "")
    )


def _one_per_key(placed):
    """The first entry per (item, kit) key, in build order: one row per item vehicle-wide.

    _placements lists checklists in template order, so this keeps the first template by id for an
    item two checklists both list, as the vehicle-wide read always has.
    """
    kept, seen = [], set()
    for entry in placed:
        key = (entry[2], entry[3] or NO_KIT)
        if key not in seen:
            seen.add(key)
            kept.append(entry)
    return kept


def _placed_content(organization_id, vehicle_id, as_of, pin=None):
    """(template order, ResolvedItem, version id, kits) per placed kit content entry (KDEF-4).

    Like _resolved_items, only bound templates and records made by as_of count. Each entry is one
    instance of the placed definition on the vehicle at as_of, in its own compartment, from
    assignment history (SPEC 2.0.0 KDEF-4). With a pin, the session's checklist places them as a
    check in that session resolves them.
    """
    if pin is not None:
        version_id = str(pin.resolved.template_version_id)
        return [
            (0, item, version_id, list(item.kits))
            for item in placed_items(
                organization_id, pin.resolved, pin.opened_at, pin.recorded_before
            )
        ]
    found = []
    bound = set(bound_template_ids(organization_id, vehicle_id, as_of))
    templates = ChecklistTemplate.objects.filter(organization_id=organization_id).order_by("pk")
    recorded_before = as_of + timedelta(microseconds=1)
    for order, template in enumerate(t for t in templates if str(t.pk) in bound):
        try:
            resolved = resolve_checklist(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                template_id=template.pk,
                as_of=as_of,
                recorded_before=recorded_before,
            )
        except NoEffectiveVersionError:
            continue
        except ResolutionError as error:
            raise ChecklistQueryError("CHECKLIST_UNAVAILABLE", str(error), 409) from None
        # One item per kit present, in its own compartment, from the assignment history as of
        # as_of (resolve_checklist), so a past instant never reads the current kit rows.
        for item in placed_items(organization_id, resolved, as_of, recorded_before):
            kits = list(item.kits)
            found.append((order, item, str(resolved.template_version_id), kits))
    return found


def _pinned_presence_lines(placed, keys, obligations):
    """{key: (line id, required count)} for presence rows whose shown obligation pins a line
    other than the row's (KDEF-12).

    A presence row comes from the kit list in force, but an obligation created under an earlier
    revision of that list stays on the same stream (the unit key) and pins its own line. The
    count a crew member records must name that pinned line, or it could not satisfy it.
    """
    wanted = {}
    for entry, key in zip(placed, keys, strict=True):
        requirement = entry[5].requirement
        state = obligations.get(key)
        if requirement.source == PRESENCE_SOURCE and state is not None:
            wanted[key] = state.obligation.requirement_version_id
    if not wanted:
        return {}
    lines = {
        str(pk): (str(line), count)
        for pk, line, count in RequirementVersion.objects.filter(
            pk__in=set(wanted.values())
        ).values_list("pk", "kit_requirement_id", "kit_requirement__required_count")
    }
    found = {}
    for entry, key in zip(placed, keys, strict=True):
        if key in wanted and wanted[key] in lines:
            line, count = lines[wanted[key]]
            if line != str(entry[5].requirement.definition_id):
                found[key] = (line, count)
    return found


def _with_line(resolved, line_id, count):
    """A presence ResolvedItem retargeted to another line of the same position."""
    return replace(
        resolved,
        requirement=replace(resolved.requirement, definition_id=line_id, expected_quantity=count),
        effective_quantity=count,
    )


def _presence_of(organization_id, order, resolved, as_of):
    """_presence_rows' entries for one resolved checklist, with kit list lines as of as_of."""
    found = []
    definitions = {
        str(pk): str(item)
        for pk, item in KitDefinition.objects.filter(
            pk__in=[p.definition_id for p in resolved.placements]
        ).values_list("pk", "presence_item_id")
    }
    for placement in resolved.placements:
        for line in placement.lines:
            item = presence_resolved_item(
                organization_id,
                resolved,
                line.requirement_id,
                definitions[placement.definition_id],
                as_of,
            )
            if item is None:
                continue
            unit = line_unit_keys([line.requirement_id])[line.requirement_id]
            found.append(
                (order, item, str(resolved.template_version_id), unit, placement.display_order)
            )
    return found


def _presence_rows(organization_id, vehicle_id, as_of, pin=None):
    """(order, resolved presence item, version, kit id, label, placement order) per assigned unit."""
    if pin is not None:
        resolved_rows = [(0, pin.resolved, str(pin.resolved.template_version_id))]
    else:
        bound = set(bound_template_ids(organization_id, vehicle_id, as_of))
        templates = ChecklistTemplate.objects.filter(organization_id=organization_id).order_by("pk")
        resolved_rows = []
        for order, template in enumerate(t for t in templates if str(t.pk) in bound):
            try:
                resolved = resolve_checklist(
                    organization_id=organization_id,
                    vehicle_id=vehicle_id,
                    template_id=template.pk,
                    as_of=as_of,
                    recorded_before=as_of + timedelta(microseconds=1),
                )
            except NoEffectiveVersionError:
                continue
            except ResolutionError as error:
                raise ChecklistQueryError("CHECKLIST_UNAVAILABLE", str(error), 409) from None
            resolved_rows.append((order, resolved, str(resolved.template_version_id)))
    found = []
    presence_ids = {
        str(pk): str(item)
        for pk, item in KitDefinition.objects.filter(
            organization_id=organization_id,
            pk__in={
                p.definition_id for _, resolved, _ in resolved_rows for p in resolved.placements
            },
        ).values_list("pk", "presence_item_id")
    }
    from cya.kits.history import active_kits_on_vehicle

    assignment_events = {
        kit.kit_id: kit
        for kit in active_kits_on_vehicle(
            organization_id, vehicle_id, as_of, recorded_before=as_of + timedelta(microseconds=1)
        )
    }
    compartment_ids = {
        str(kit[2])
        for _, resolved, _ in resolved_rows
        for placement in resolved.placements
        for kit in placement.kits
        if kit[2]
    }
    compartment_labels = dict(
        Compartment.objects.filter(
            organization_id=organization_id, pk__in=compartment_ids
        ).values_list("pk", "label")
    )
    for order, resolved, version_id in resolved_rows:
        for placement in resolved.placements:
            item_id = presence_ids.get(str(placement.definition_id))
            if item_id is None:
                continue
            for kit_id, label, _compartment in placement.kits:
                row = instance_presence_resolved_item(
                    organization_id,
                    resolved,
                    placement,
                    kit_id,
                    item_id,
                    compartment_labels=compartment_labels,
                    as_of=as_of,
                    recorded_before=as_of + timedelta(microseconds=1),
                    assignment_events=assignment_events,
                )
                if row is not None:
                    found.append(
                        (order, row, version_id, str(kit_id), label, placement.display_order)
                    )
    return found


def _kits_at(organization_id, vehicle_id, reference_kit_id, as_of, slots):
    """(kit id, label) of the vehicle's kits in the reference kit's slot, from assignment history.

    The kit_instance row is only the current placement, so a past instant must not read it.
    """
    if reference_kit_id not in slots:
        slots[reference_kit_id] = slot_of_reference_kit(reference_kit_id)
    if slots[reference_kit_id] is None:
        return []
    active = active_kits(
        organization_id,
        vehicle_id,
        slots[reference_kit_id],
        as_of,
        recorded_before=as_of + timedelta(microseconds=1),
    )
    labels = dict(
        KitInstance.objects.filter(
            organization_id=organization_id, pk__in=[k.kit_id for k in active]
        ).values_list("pk", "label")
    )
    return sorted(
        ((k.kit_id, labels.get(k.kit_id, "")) for k in active), key=lambda k: (k[1], k[0])
    )


def _rank(state):
    """Urgency of one obligation state; an ended suspension is history and ranks last."""
    if state.state == State.SUSPENDED and not state.suspension_in_force:
        return len(STATE_RANK)
    return STATE_RANK.index(state.state)


def _missed(state, as_of):
    """Unsatisfied past its pinned late-completion limit L at as_of (SPEC SCH-2, issue #80).

    No check can complete it any more, so it stays Overdue (missed) in history and reports but
    no longer decides the row: only a later obligation of the item can be checked. The limit is
    inclusive, as in linking (a check at exactly L still completes it).
    """
    return state.state == State.OVERDUE and as_of > state.obligation.late_limit()


def _fact_item_id(facts):
    """The item of one stored projection row, read without evaluating it."""
    evidence = getattr(facts, "evidence", None)
    return str(evidence[0].item_id if evidence is not None else facts["obligation"]["item_id"])


MAX_ITEM_IDS = 20  # items one partial checklist read may name (issue #278)


def _obligation_by_key(
    organization_id, vehicle_id, as_of, suspension_lookup, *, current=False, item_ids=None
):
    """The most urgent obligation state per (item_id, kit_key), leaving out missed ones.

    With item_ids, only those items' obligations are evaluated (issue #278): each key's state
    depends on its own obligations alone, so the result for those keys is the same.
    """
    best = {}
    states = None
    if current and suspension_lookup is None:
        from cya.obligations.current import current_rows, evaluate_facts

        cached = current_rows(organization_id, [vehicle_id], as_of, include_all_ad_hoc=True)
        if cached is not None:
            facts = cached[vehicle_id]
            if item_ids is not None:
                facts = [f for f in facts if _fact_item_id(f) in item_ids]
            states = sorted(
                (evaluate_facts(f, as_of) for f in facts),
                key=lambda s: (
                    s.obligation.due_at,
                    s.obligation.item_id,
                    s.obligation.unit_key,
                    s.obligation.id,
                ),
            )
    if states is None:
        states = get_vehicle_obligations_as_of(
            vehicle_id,
            as_of,
            organization_id=organization_id,
            suspension_lookup=suspension_lookup,
            include_all_ad_hoc=True,
        )
    for state in states:
        if _missed(state, as_of):
            continue
        if item_ids is not None and str(state.obligation.item_id) not in item_ids:
            continue
        key = (state.obligation.item_id, state.obligation.unit_key)
        current = best.get(key)
        if current is None or (_rank(state), state.obligation.due_at) < (
            _rank(current),
            current.obligation.due_at,
        ):
            best[key] = state
    return best


def _vehicle_status(organization_id, vehicle_id, as_of):
    event = status_at(organization_id, vehicle_id, as_of)
    if event is None:
        return {"status": "IN_SERVICE", "restrictions": None, "since": None}
    return {
        "status": event.status,
        "restrictions": event.restrictions,
        "since": event.recorded_at,
    }


def _levels_as_of(organization_id, deficiency_ids, as_of):
    """Severity level per deficiency in force at as_of: the newest re-grade recorded by then, else
    the creation header's (absent from the result). Re-grades are append-only, so this is exact."""
    levels = {}
    events = (
        DeficiencyEvent.objects.filter(
            organization_id=organization_id,
            deficiency_id__in=deficiency_ids,
            event_type="SEVERITY_CHANGED",
            recorded_at__lte=as_of,
        )
        .select_related("severity_version")
        .order_by("sequence")
    )
    for event in events:
        levels[str(event.deficiency_id)] = event.severity_version
    return levels


def _deficiency_dtos(organization_id, facts, as_of):
    levels = _levels_as_of(organization_id, [f.deficiency_id for f in facts], as_of)
    headers = {
        str(d.pk): d
        for d in Deficiency.objects.filter(
            organization_id=organization_id, pk__in=[f.deficiency_id for f in facts]
        ).select_related("severity_version")
    }
    return {
        fact.deficiency_id: {
            "id": fact.deficiency_id,
            "status": fact.status,
            "summary": headers[fact.deficiency_id].summary,
            "severity_code": levels.get(
                fact.deficiency_id, headers[fact.deficiency_id].severity_version
            ).code,
            "severity_label": levels.get(
                fact.deficiency_id, headers[fact.deficiency_id].severity_version
            ).label,
            "reported_at": headers[fact.deficiency_id].reported_at,
        }
        for fact in facts
    }


def _ordered_rows(organization_id, vehicle_id, placed, obligations):
    """Retain explicit orders when publication or kit movement removes a current row."""
    keys = {(p[2], p[3] or NO_KIT) for p in placed}
    missing = [
        state.obligation
        for key, state in obligations.items()
        if key not in keys and not state.obligation.routine
    ]
    pins = RequirementVersion.objects.filter(
        organization_id=organization_id, pk__in=[o.requirement_version_id for o in missing]
    ).select_related("publication__checklist_version", "kit_instance")
    by_id = {str(pin.pk): pin for pin in pins}
    for obligation in missing:
        pin = by_id[obligation.requirement_version_id]
        resolved = resolve_checklist(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            template_id=pin.publication.checklist_version.template_id,
            as_of=obligation.due_at,
            recorded_before=obligation.recorded_at + timedelta(microseconds=1),
        )
        row = next(
            r for r in resolved.items if str(r.requirement.definition_id) == str(pin.item_def_id)
        )
        placed.append(
            (
                len(placed),
                (row.requirement.display_order,),
                obligation.item_id,
                obligation.kit_instance_id
                or (obligation.kit_key if pin.item_requirement_version_id else None),
                pin.kit_instance.label if pin.kit_instance_id else None,
                row,
                str(pin.publication.checklist_version_id),
            )
        )
    return placed


def _placements(organization_id, vehicle_id, as_of, pin=None):
    """(placed, labels): one entry per checklist row each bound template defines, before any
    obligation is consulted, and the item labels by id. Shared by the full and label-only paths.

    An item or kit two checklists both list has one entry per checklist (SPEC 6.0.0 VW-5);
    callers that read the vehicle as a whole keep one per key with _one_per_key. With a pin, the
    entries are the session's checklist's own (CHK-4).
    """
    items = _resolved_items(organization_id, vehicle_id, as_of, pin)
    labels = dict(
        Item.objects.filter(
            organization_id=organization_id, pk__in={entry[1] for entry in items}
        ).values_list("pk", "label")
    )
    # (order, display key, item_id, kit id, kit label, ResolvedItem, version id). The display key
    # is a tuple: (display_order,) for a checklist item; for a placed kit definition, its
    # placement's order, then 0 for a presence line or 1 for kit content, then the entry's order,
    # so each kit's presence line comes before its contents (KDEF-10).
    placed = []
    slots = {}
    for order, item_id, resolved, version_id in items:
        requirement = resolved.requirement
        if requirement.kit_id:  # one row per kit instance; none means nothing is owed
            kits = _kits_at(organization_id, vehicle_id, str(requirement.kit_id), as_of, slots)
            for kit_id, kit_label in kits:
                placed.append(
                    (
                        order,
                        (requirement.display_order,),
                        item_id,
                        kit_id,
                        kit_label,
                        resolved,
                        version_id,
                    )
                )
        else:
            # SPEC 6.0.0 CHK-11: after the cutover a loose item's row is keyed by its item
            # requirement, the unit of its obligation stream (None before it).
            unit = getattr(requirement, "item_requirement_id", None)
            placed.append(
                (order, (requirement.display_order,), item_id, unit, None, resolved, version_id)
            )
    # Content of kits placed by definition (issue #129): one row per kit of that definition, in
    # each checklist that places it.
    present = {(row[6], row[2], row[3]) for row in placed}
    content = _placed_content(organization_id, vehicle_id, as_of, pin)
    placement_order = dict(
        ChecklistKitPlacement.objects.filter(
            pk__in={entry[1].requirement.kit_id for entry in content}
        ).values_list("pk", "display_order")
    )
    placement_order = {str(pk): value for pk, value in placement_order.items()}
    for order, resolved, version_id, kits in content:
        item_id = str(resolved.requirement.item_id)
        for kit_id, kit_label in kits:
            if (version_id, item_id, kit_id) in present:
                continue
            present.add((version_id, item_id, kit_id))
            placed.append(
                (
                    order,
                    (
                        placement_order[str(resolved.requirement.kit_id)],
                        1,
                        resolved.requirement.display_order,
                    ),
                    item_id,
                    kit_id,
                    kit_label,
                    resolved,
                    version_id,
                )
            )
    # Physical-kit presence is one row per assigned instance, never per stock unit.
    for order, resolved, version_id, kit_id, kit_label, placement_display in _presence_rows(
        organization_id, vehicle_id, as_of, pin
    ):
        item_id = str(resolved.requirement.item_id)
        if (version_id, item_id, kit_id) not in present:
            present.add((version_id, item_id, kit_id))
            placed.append(
                (
                    order,
                    (placement_display, 0, resolved.requirement.display_order),
                    item_id,
                    kit_id,
                    kit_label,
                    resolved,
                    version_id,
                )
            )
    if any(item_id not in labels for _o, _d, item_id, *_rest in placed):
        labels.update(
            Item.objects.filter(
                organization_id=organization_id, pk__in=[row[2] for row in placed]
            ).values_list("pk", "label")
        )
    return placed, labels


def _rows(
    organization_id,
    vehicle_id,
    as_of,
    suspension_lookup,
    *,
    current=False,
    checklist_version_id=None,
    pin=None,
    item_ids=None,
):
    placed, labels = _placements(organization_id, vehicle_id, as_of, pin)
    if pin is not None:
        # A session's rows are its pinned version's (CHK-4), even after a newer one governs.
        checklist_version_id = str(pin.resolved.template_version_id)
    if checklist_version_id is None:
        placed = _one_per_key(placed)
    if item_ids is not None:
        # Issue #278: only these items' rows. Every later step is per (item, kit) key, so each
        # row comes out exactly as in the whole list.
        placed = [p for p in placed if p[2] in item_ids]
    obligations = _obligation_by_key(
        organization_id, vehicle_id, as_of, suspension_lookup, current=current, item_ids=item_ids
    )
    placed = _ordered_rows(organization_id, vehicle_id, placed, obligations)
    if checklist_version_id is not None:
        # One checklist's rows (VW-5): every item it lists, sharing the vehicle's obligation and
        # attention state by (item, kit) key with any other checklist that lists the same item.
        placed = [p for p in placed if p[6] == checklist_version_id]
    labels.update(
        Item.objects.filter(
            organization_id=organization_id, pk__in=[p[2] for p in placed]
        ).values_list("pk", "label")
    )
    placed.sort(key=lambda p: (p[0], p[1], p[2], str(p[3] or "")))
    keys = [(p[2], str(p[3]) if p[3] else NO_KIT) for p in placed]
    facts = deficiency_facts(organization_id, vehicle_id, as_of)
    checks = latest_effective_checks(organization_id, vehicle_id, keys, as_of)
    attention = attention_states(keys, checks, facts)
    newest = latest_checks(checks)
    dtos = _deficiency_dtos(organization_id, facts, as_of)
    pinned = _pinned_presence_lines(placed, keys, obligations)
    rows = []
    for (_order, _display, item_id, kit_id, kit_label, resolved, version_id), key in zip(
        placed, keys, strict=True
    ):
        state = obligations.get(key)
        if key in pinned:
            # The shown obligation pins an earlier line of this position: count against it.
            resolved = _with_line(resolved, *pinned[key])
        requirement = resolved.requirement
        att = attention[key]
        last = newest.get(key)
        rows.append(
            ChecklistRow(
                item_id=item_id,
                item_label=labels.get(item_id, ""),
                # Physical presence rows retain the assigned unit id; legacy line rows do not.
                # A loose item's is its item requirement (SPEC 6.0.0 CHK-11).
                kit_id=str(kit_id) if _is_kit(kit_id, requirement) else None,
                item_requirement_id=getattr(requirement, "item_requirement_id", None),
                kit_label=kit_label,
                definition_id=str(requirement.definition_id),
                kind=requirement.kind,
                instructions=requirement.instructions,
                compartment_id=str(resolved.compartment_id) if resolved.compartment_id else None,
                compartment_label=resolved.compartment_label,
                system_id=str(resolved.system_id) if resolved.system_id else None,
                system_label=resolved.system_label,
                not_placed=_not_placed(requirement, resolved),
                display_order=requirement.display_order,
                obligation_state=state.state.value if state else None,
                due_at=state.obligation.due_at if state else None,
                attention=att,
                needs_action=state is not None and state.state in ACTIONABLE,
                deficiencies=tuple(
                    dtos[f.deficiency_id] for f in facts if applies(f, item_id, key[1])
                ),
                checklist_version_id=version_id,
                expected_quantity=resolved.effective_quantity,
                unit=requirement.unit,
                allowed_outcomes=tuple(requirement.allowed_outcomes),
                obligation_outcome=state.outcome if state else None,
                obligation_completed_at=state.completed_at if state else None,
                kit_requirement_id=(
                    str(requirement.definition_id)
                    if requirement.source == PRESENCE_SOURCE
                    else None
                ),
                kit_presence=requirement.source == INSTANCE_PRESENCE_SOURCE,
                check_suspended=state is not None and state.suspension_in_force,
                latest_check_id=last.check_id if last else None,
                latest_check_outcome=last.outcome if last else None,
                latest_check_at=last.recorded_at if last else None,
            )
        )
    return rows


def _priority_group(row):
    if row.attention.red:
        return 0
    return 1 if row.needs_action else 2


def _not_placed(requirement, resolved):
    """A checklist item row with no position (SPEC 5.0.0 CHK-9), not a kit or presence row."""
    return (
        requirement.source in ("TEMPLATE", "OVERRIDE")
        and not requirement.kit_id
        and not resolved.compartment_id
        and not resolved.system_id
    )


def _group_progress(rows):
    groups, by_key = [], {}
    for row in rows:
        if row.not_placed:
            key, label = "not-placed", "Not placed"
        elif row.compartment_id:
            key, label = f"compartment:{row.compartment_id}", row.compartment_label
        elif row.system_id:
            key, label = f"system:{row.system_id}", f"Vehicle system: {row.system_label}"
        elif row.kit_id:
            key, label = f"kit:{row.kit_id}", f"Kit: {row.kit_label}"
        else:
            key, label = "vehicle", "Vehicle-wide checks"
        group = by_key.get(key)
        if group is None:
            group = {
                "key": key,
                "label": label or "Vehicle-wide checks",
                "total": 0,
                "checked": 0,
                "due": 0,
                "due_soon": 0,
                "overdue": 0,
                "not_due": 0,
                "no_obligation": 0,
                "exceptions": 0,
                "deficiency_ids": set(),
            }
            by_key[key] = group
            groups.append(group)
        group["total"] += 1
        state = row.obligation_state
        if state == State.SATISFIED:
            group["checked"] += 1
        elif state == State.DUE:
            group["due"] += 1
        elif state == State.CURRENT:
            group["due_soon"] += 1
        elif state == State.OVERDUE:
            group["overdue"] += 1
        elif state == State.UPCOMING:
            group["not_due"] += 1
        elif state is None:
            group["no_obligation"] += 1
        elif state in (State.SUSPENDED, State.EXEMPT, State.WAIVED):
            group["exceptions"] += 1
        group["deficiency_ids"].update(d["id"] for d in row.deficiencies if d.get("id"))
    return tuple(
        {
            **{key: value for key, value in group.items() if key != "deficiency_ids"},
            "open_deficiencies": len(group["deficiency_ids"]),
        }
        for group in groups
    )


def vehicle_checklist(
    organization_id,
    vehicle_id,
    *,
    view=NORMAL,
    needs_attention_only=False,
    as_of=None,
    suspension_lookup=None,
    checklist_version_id=None,
    session=None,
    item_ids=None,
):
    """The vehicle's checklist rows for a view and filter, as of an instant (default now).

    With item_ids (at most MAX_ITEM_IDS identifiers), only those items' rows, each exactly as
    in the whole list, and no group progress (None): a page patches the rows it shows after a
    live update names the items (issue #278).

    Without checklist_version_id there is one row per item (and kit) for the whole vehicle. With
    it, the rows are that checklist version's own, including items another checklist also lists
    (SPEC 6.0.0 VW-5), and group progress counts those rows; an unknown version lists nothing.

    With session (an InspectionSession on this vehicle the caller has authorized), the rows are
    those of the version the session pins, resolved as a check in the session is (SPEC CHK-4,
    5.7.1): an item a later version removes is still listed and one it adds is not. Each row
    still shows the vehicle's state for its item as of the instant. Not with checklist_version_id.
    """
    if session is not None and checklist_version_id is not None:
        raise ChecklistQueryError(
            "INVALID_CHECKLIST_VERSION", "give a session or a checklist version, not both", 400
        )
    if view not in (NORMAL, PRIORITY):
        raise ChecklistQueryError("INVALID_VIEW", "view must be NORMAL or PRIORITY", 400)
    if checklist_version_id is not None:
        try:
            checklist_version_id = canonical_id(checklist_version_id)
        except (ValueError, TypeError, AttributeError):
            raise ChecklistQueryError(
                "INVALID_CHECKLIST_VERSION", "checklist_version_id must be an identifier", 400
            ) from None
    if type(needs_attention_only) is not bool:
        raise ChecklistQueryError("INVALID_FILTER", "needs_attention_only must be a boolean", 400)
    if item_ids is not None:
        try:
            item_ids = frozenset(canonical_id(value) for value in item_ids)
        except (ValueError, TypeError, AttributeError):
            raise ChecklistQueryError(
                "INVALID_ITEMS", "item_ids must be identifiers", 400
            ) from None
        if not item_ids or len(item_ids) > MAX_ITEM_IDS:
            raise ChecklistQueryError("INVALID_ITEMS", f"name 1 to {MAX_ITEM_IDS} items", 400)
    instant = timezone.now() if as_of is None else as_of
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ChecklistQueryError("INVALID_AS_OF", "as_of must be timezone-aware", 400)
    if not Vehicle.objects.filter(organization_id=organization_id, pk=vehicle_id).exists():
        raise ChecklistQueryError("NOT_FOUND", "vehicle was not found", 404)
    pin = (
        None
        if session is None
        else _session_pin(organization_id, str(vehicle_id), session, instant)
    )
    rows = _rows(
        organization_id,
        str(vehicle_id),
        instant,
        suspension_lookup,
        current=as_of is None,
        checklist_version_id=checklist_version_id,
        pin=pin,
        item_ids=item_ids,
    )
    group_progress = None if item_ids is not None else _group_progress(rows)
    if needs_attention_only:
        rows = [
            row
            for row in rows
            if not row.check_suspended and (row.attention.red or row.needs_action)
        ]
    if view == PRIORITY:
        # sorted() is stable, so each group keeps the Normal order.
        rows = sorted(rows, key=_priority_group)
    return VehicleChecklist(
        vehicle_id=str(vehicle_id),
        as_of=instant,
        view=view,
        needs_attention_only=needs_attention_only,
        vehicle_status=_vehicle_status(organization_id, vehicle_id, instant),
        rows=tuple(rows),
        group_progress=group_progress,
    )


@dataclass(frozen=True)
class LabelRow:
    """The label fields of one checklist row, in the order vehicle_checklist lists the rows."""

    item_id: str
    item_label: str
    kit_id: str | None
    kit_label: str | None
    kind: str
    compartment_id: str | None
    compartment_label: str | None
    system_id: str | None
    system_label: str | None
    # The item requirement (place) of a row outside a kit after the cutover (SPEC 6.0.0 CHK-11).
    item_requirement_id: str | None = None


ADHOC_KEY_LIMIT = 2000  # distinct (item, kit) pairs read before the full path decides instead


def _label_row(row):
    return LabelRow(
        item_id=row.item_id,
        item_label=row.item_label,
        kit_id=row.kit_id,
        kit_label=row.kit_label,
        kind=row.kind,
        compartment_id=row.compartment_id,
        compartment_label=row.compartment_label,
        system_id=row.system_id,
        system_label=row.system_label,
        item_requirement_id=getattr(row, "item_requirement_id", None),
    )


def _adhoc_keys_missing(organization_id, vehicle_id, as_of, placed):
    """True when a one-off order names an (item, kit) that is not a current checklist row.

    The full path keeps such a row (its immutable order) while the order is not missed, so only
    the full path can answer. This reads keys only and over-approximates: a missed or unselected
    order also triggers it, which is safe because the full path is always exact. Routine
    obligations never add a row, so they are not read.
    """
    keys = {(p[2], p[3] or NO_KIT) for p in placed}
    found = list(
        Obligation.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            routine=False,
            recorded_at__lte=as_of,
        )
        .values_list(
            "item_id",
            "kit_instance_id",
            "kit_key",
            "requirement_version__item_requirement_version_id",
        )
        .distinct()[: ADHOC_KEY_LIMIT + 1]
    )
    if len(found) > ADHOC_KEY_LIMIT:
        return True
    return any(
        (str(item), str(kit) if kit else (str(unit) if version else NO_KIT)) not in keys
        for item, kit, unit, version in found
    )


def vehicle_label_rows(organization_id, vehicle_id):
    """The label fields of the vehicle's current checklist rows, without evaluating obligations.

    Returns exactly the label fields, in exactly the order, that vehicle_checklist(...).rows
    gives for a current read. It answers from the template structure alone when no one-off order
    names an item or kit that is not a current row; otherwise it falls back to the full path.
    For callers that show or choose by label only: anything that needs a state, a check, a
    deficiency or an attention flag must call vehicle_checklist.
    """
    instant = timezone.now()
    if not Vehicle.objects.filter(organization_id=organization_id, pk=vehicle_id).exists():
        raise ChecklistQueryError("NOT_FOUND", "vehicle was not found", 404)
    vehicle_id = str(vehicle_id)
    placed, labels = _placements(organization_id, vehicle_id, instant)
    placed = _one_per_key(placed)
    if _adhoc_keys_missing(organization_id, vehicle_id, instant, placed):
        rows = _rows(organization_id, vehicle_id, instant, None, current=True)
        return tuple(_label_row(row) for row in rows)
    placed.sort(key=lambda p: (p[0], p[1], p[2], str(p[3] or "")))
    return tuple(
        LabelRow(
            item_id=item_id,
            item_label=labels.get(item_id, ""),
            kit_id=str(kit_id) if _is_kit(kit_id, resolved.requirement) else None,
            kit_label=kit_label,
            kind=resolved.requirement.kind,
            compartment_id=str(resolved.compartment_id) if resolved.compartment_id else None,
            compartment_label=resolved.compartment_label,
            system_id=str(resolved.system_id) if resolved.system_id else None,
            system_label=resolved.system_label,
            item_requirement_id=getattr(resolved.requirement, "item_requirement_id", None),
        )
        for (_order, _display, item_id, kit_id, kit_label, resolved, _version) in placed
    )
