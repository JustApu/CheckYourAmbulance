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

Needs Attention Only keeps a row that is red, or whose obligation still needs action: Overdue,
Due or Current ("actionable due, overdue, incomplete"). Upcoming, Satisfied, Exempt and Waived
rows are not actionable. A row whose routine check a supervisor suspended for a known deficiency
(SPEC 8.9) shows obligation state SUSPENDED ("Known Deficiency — Check Suspended"): it is never
actionable and, while that suspension is in force, is left out of Needs Attention Only even though
its deficiency makes it red (VW-2, AC-18), but it stays in the full checklist with its deficiency
listed. An obligation that stays Suspended in history after its suspension ended ranks last, so the
row shows its current obligation again. A row still shows Overdue, Due or Current, and stays in the
filter, when another of its obligations needs action, such as one missed before the suspension
started. Suspensions are read from storage by the obligation reader; ``suspension_lookup``
replaces that evidence when supplied.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from cya.attention.facts import deficiency_facts, latest_effective_checks
from cya.attention.state import Attention, applies, attention_states, latest_checks
from cya.checklists.models import ChecklistTemplate, Item, KitInstance, Vehicle
from cya.checklists.overlay import NoEffectiveVersionError, ResolutionError, resolve_checklist
from cya.deficiencies.models import Deficiency, DeficiencyEvent
from cya.kits.history import active_kits, slot_of_reference_kit
from cya.obligations.models import RequirementVersion
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
    # The shown obligation is under a supervisor check suspension still in force (SPEC 8.9).
    check_suspended: bool = False


@dataclass(frozen=True)
class VehicleChecklist:
    vehicle_id: str
    as_of: datetime
    view: str
    needs_attention_only: bool
    vehicle_status: dict
    rows: tuple


def _resolved_items(organization_id, vehicle_id, as_of):
    """(template order, ResolvedItem, version id) per item the vehicle's bound checklists require.

    Only templates with a BIND in force for this vehicle at as_of apply (a template published
    for another vehicle does not), and only publications and bindings recorded by as_of count,
    so a past answer is reproducible. The first template by id wins an item both define.
    """
    chosen = {}
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
        for item in resolved.items:
            chosen.setdefault(
                str(item.requirement.item_id), (order, item, str(resolved.template_version_id))
            )
    return chosen


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


def _obligation_by_key(organization_id, vehicle_id, as_of, suspension_lookup):
    """The most urgent obligation state per (item_id, kit_key)."""
    best = {}
    for state in get_vehicle_obligations_as_of(
        vehicle_id,
        as_of,
        organization_id=organization_id,
        suspension_lookup=suspension_lookup,
        include_all_ad_hoc=True,
    ):
        key = (state.obligation.item_id, state.obligation.kit_instance_id or NO_KIT)
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
                row.requirement.display_order,
                obligation.item_id,
                obligation.kit_instance_id,
                pin.kit_instance.label if pin.kit_instance_id else None,
                row,
                str(pin.publication.checklist_version_id),
            )
        )
    return placed


def _rows(organization_id, vehicle_id, as_of, suspension_lookup):
    items = _resolved_items(organization_id, vehicle_id, as_of)
    labels = dict(
        Item.objects.filter(organization_id=organization_id, pk__in=list(items)).values_list(
            "pk", "label"
        )
    )
    placed = []  # (order, display_order, item_id, kit id, kit label, ResolvedItem, version id)
    slots = {}
    for item_id, (order, resolved, version_id) in items.items():
        requirement = resolved.requirement
        if requirement.kit_id:  # one row per kit instance; none means nothing is owed
            kits = _kits_at(organization_id, vehicle_id, str(requirement.kit_id), as_of, slots)
            for kit_id, kit_label in kits:
                placed.append(
                    (
                        order,
                        requirement.display_order,
                        item_id,
                        kit_id,
                        kit_label,
                        resolved,
                        version_id,
                    )
                )
        else:
            placed.append(
                (order, requirement.display_order, item_id, None, None, resolved, version_id)
            )
    obligations = _obligation_by_key(organization_id, vehicle_id, as_of, suspension_lookup)
    placed = _ordered_rows(organization_id, vehicle_id, placed, obligations)
    labels.update(
        Item.objects.filter(
            organization_id=organization_id, pk__in=[p[2] for p in placed]
        ).values_list("pk", "label")
    )
    placed.sort(key=lambda p: (p[0], p[1], p[2], str(p[3] or "")))
    keys = [(p[2], str(p[3]) if p[3] else NO_KIT) for p in placed]
    facts = deficiency_facts(organization_id, vehicle_id, as_of)
    checks = latest_effective_checks(organization_id, vehicle_id, {k[0] for k in keys}, as_of)
    attention = attention_states(keys, checks, facts)
    newest = latest_checks(checks)
    dtos = _deficiency_dtos(organization_id, facts, as_of)
    rows = []
    for (_order, _display, item_id, kit_id, kit_label, resolved, version_id), key in zip(
        placed, keys, strict=True
    ):
        requirement = resolved.requirement
        state = obligations.get(key)
        att = attention[key]
        last = newest.get(key)
        rows.append(
            ChecklistRow(
                item_id=item_id,
                item_label=labels.get(item_id, ""),
                kit_id=str(kit_id) if kit_id else None,
                kit_label=kit_label,
                definition_id=str(requirement.definition_id),
                kind=requirement.kind,
                instructions=requirement.instructions,
                compartment_id=str(resolved.compartment_id) if resolved.compartment_id else None,
                compartment_label=resolved.compartment_label,
                system_id=str(resolved.system_id) if resolved.system_id else None,
                system_label=resolved.system_label,
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


def vehicle_checklist(
    organization_id,
    vehicle_id,
    *,
    view=NORMAL,
    needs_attention_only=False,
    as_of=None,
    suspension_lookup=None,
):
    """The vehicle's checklist rows for a view and filter, as of an instant (default now)."""
    if view not in (NORMAL, PRIORITY):
        raise ChecklistQueryError("INVALID_VIEW", "view must be NORMAL or PRIORITY", 400)
    if type(needs_attention_only) is not bool:
        raise ChecklistQueryError("INVALID_FILTER", "needs_attention_only must be a boolean", 400)
    instant = timezone.now() if as_of is None else as_of
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ChecklistQueryError("INVALID_AS_OF", "as_of must be timezone-aware", 400)
    if not Vehicle.objects.filter(organization_id=organization_id, pk=vehicle_id).exists():
        raise ChecklistQueryError("NOT_FOUND", "vehicle was not found", 404)
    rows = _rows(organization_id, str(vehicle_id), instant, suspension_lookup)
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
    )
