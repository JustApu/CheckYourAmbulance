"""Fleet readiness for supervisors (SPEC 14 RPT-5 and RPT-7; issue 17).

One read-only picture per vehicle, now or as of an instant (AC-27): operational status, the
obligations in the standard window (7 days back to 48 hours ahead) with every state kept apart,
inspection progress, the deficiencies being worked with their deadlines, age, dependency and
acknowledgement, and the check suspensions in force. Nothing here writes or decides anything new:
each fact comes from an existing reader, replayed at the instant.

RPT-7: states are counted separately and never folded into one "compliant" flag. Passed, Failed,
Not Applicable (rule), Carried over (rule), other waivers, Suspended, Exempt (out of service),
supervisor-completed Unable and the open states come from the obligation evaluator; Unable (still
open), Invalidated, Superseded and Completed late come from the underlying check, correction and
determination histories, not from the checklist's one-row-per-item summary.

Bounds (owner decision 2026-11-01: a small fleet first; issue 33 batches the obligation reads):
at most VEHICLE_LIMIT vehicles and DEFICIENCY_LIMIT deficiencies per vehicle, each with an
explicit `truncated` flag. Obligation state is read per vehicle through the existing loader; the
deficiency list and suspensions are read once for the listed vehicles.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import OuterRef, Subquery

from cya.checklists.models import Item, KitInstance, Vehicle
from cya.checks.state import effective_checks
from cya.conditions.models import ConditionalRule
from cya.deficiencies import service
from cya.deficiencies.acknowledgement import acknowledgement_state
from cya.deficiencies.models import CheckSuspension, Deficiency, DeficiencyEvent
from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.obligations.history import replay_checks
from cya.obligations.projections import load_projection_history
from cya.obligations.requirements import CARRY_OVER
from cya.obligations.state import State, aware, rebuild_vehicle_projection
from cya.operations.status import status_at

VEHICLE_LIMIT = 25
DEFICIENCY_LIMIT = 100  # per vehicle
WINDOW_BACK = timedelta(days=7)
WINDOW_AHEAD = timedelta(hours=48)

# RPT-7 categories in display order. Open states are listed after the recorded outcomes.
CATEGORIES = (
    ("PASSED", "Passed"),
    ("FAILED", "Failed"),
    ("NOT_APPLICABLE", "Not Applicable (rule)"),
    ("CARRIED_OVER", "Carried over (rule)"),
    ("WAIVED", "Waived (other rule)"),
    ("SUSPENDED", "Suspended"),
    ("EXEMPT", "Exempt (out of service)"),
    ("UNABLE", "Unable to inspect"),
    ("UNABLE_COMPLETED", "Completed by supervisor — unable to inspect"),
    ("INVALIDATED", "Invalidated"),
    ("SUPERSEDED", "Superseded"),
    ("COMPLETED_LATE", "Completed late"),
    ("OVERDUE", "Overdue"),
    ("DUE", "Due"),
    ("CURRENT", "Current"),
    ("UPCOMING", "Upcoming"),
)
OPEN = (State.CURRENT, State.DUE, State.OVERDUE)


@dataclass
class VehicleReadiness:
    vehicle_id: str
    display_name: str
    active: bool
    status: str
    restrictions: str | None
    counts: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    deficiencies: list = field(default_factory=list)
    deficiencies_truncated: bool = False
    by_severity: dict = field(default_factory=dict)
    by_status: dict = field(default_factory=dict)
    suspensions: list = field(default_factory=list)


@dataclass
class FleetReadiness:
    as_of: object
    window_start: object
    window_end: object
    vehicles: list
    truncated: bool


def _carry_over_rules(organization_id, rule_ids):
    """The ids among these conditional rules that are carry-over rules (a waiver's kind)."""
    if not rule_ids:
        return set()
    return {
        str(pk)
        for pk in ConditionalRule.objects.filter(
            organization_id=organization_id, pk__in=rule_ids, rule_type=CARRY_OVER
        ).values_list("pk", flat=True)
    }


def _obligations(organization_id, vehicle_id, at):
    """RPT-7 counts and inspection progress for one vehicle's window, from the histories."""
    start, end = at - WINDOW_BACK, at + WINDOW_AHEAD
    history = load_projection_history(
        vehicle_id, at, organization_id=organization_id, start=start, end=end
    )
    states = rebuild_vehicle_projection(history, vehicle_id, at, start, end)
    counts = dict.fromkeys((key for key, _label in CATEGORIES), 0)
    window = {s.obligation.id for s in states}

    waived = {s.evidence_id for s in states if s.state == State.WAIVED}
    rule_of = {d.id: d.rule.id for d in history.determinations if d.id in waived and d.rule}
    carry = _carry_over_rules(organization_id, set(rule_of.values()))
    linked, _facts = replay_checks(history, at)
    unable = {c.obligation_id for c in linked if c.outcome == "UNABLE_TO_INSPECT"}

    for state in states:
        if state.state == State.SATISFIED:
            key = {
                "PASSED": "PASSED",
                "FAILED": "FAILED",
                "NOT_APPLICABLE": "NOT_APPLICABLE",
                "SUPERVISOR_COMPLETED": "UNABLE_COMPLETED",
            }[state.outcome]
            counts[key] += 1
            if state.late_seconds > 0:
                counts["COMPLETED_LATE"] += 1
        elif state.state == State.WAIVED:
            counts["CARRIED_OVER" if rule_of.get(state.evidence_id) in carry else "WAIVED"] += 1
        elif state.state in OPEN and state.obligation.id in unable:
            counts["UNABLE"] += 1
            counts[state.state.value] += 1
        else:
            counts[state.state.value] += 1

    # Invalidated and superseded checks linked to this window's obligations (a whole chain
    # withdrawn, or entries a correction replaced), and withdrawn or replaced determinations.
    root_of = {r.snapshot.id: r.snapshot.root_id for r in history.check_records}
    roots = {
        root_of[link.check_id]
        for link in history.check_links
        if link.obligation_id in window and aware(link.recorded_at) <= at
    }
    for chain in effective_checks(
        (r.snapshot for r in history.check_records), history.check_decisions, as_of=at
    ):
        if chain.root_id in roots:
            counts["INVALIDATED"] += int(chain.invalidated)
            counts["SUPERSEDED"] += len(chain.superseded_ids)
    for determination in history.determinations:
        if determination.obligation_id in window and determination.recorded_at <= at:
            if determination.determination_type == "INVALIDATED":
                counts["INVALIDATED"] += 1
            elif determination.determination_type == "SUPERSEDED":
                counts["SUPERSEDED"] += 1

    satisfied = sum(1 for s in states if s.state == State.SATISFIED)
    owed = satisfied + sum(1 for s in states if s.state in OPEN)
    return counts, {"checked": satisfied, "owed": owed}


def _active_deficiencies(organization_id, vehicle_ids, at):
    """Deficiencies of these vehicles being worked at `at`, by their status then (AC-27)."""
    newest = DeficiencyEvent.objects.filter(
        deficiency_id=OuterRef("pk"), recorded_at__lte=at
    ).order_by("-sequence")
    rows = (
        Deficiency.objects.filter(
            organization_id=organization_id, vehicle_id__in=vehicle_ids, recorded_at__lte=at
        )
        .annotate(status_then=Subquery(newest.values("to_status")[:1]))
        .filter(status_then__in=ACTIVE_STATUSES)
        .order_by("vehicle_id", "recorded_at", "id")
    )
    by_vehicle = {}
    for row in rows.iterator():
        by_vehicle.setdefault(str(row.vehicle_id), []).append(row)
    return by_vehicle


def _deficiency(organization_id, row, at):
    deficiency_id = str(row.pk)
    deadline = service.extension_state(organization_id, deficiency_id, as_of=at)
    grades = [
        g
        for g in service.severity_history(organization_id, deficiency_id)["history"]
        if g["recorded_at"] <= at
    ]
    dependency = service.dependency_state(organization_id, deficiency_id, as_of=at)["dependency"]
    acknowledged = acknowledgement_state(organization_id, deficiency_id, as_of=at)[
        "acknowledgement"
    ]
    return {
        "deficiency_id": deficiency_id,
        "summary": row.summary,
        "status": row.status_then,
        "severity": grades[-1]["severity"],
        "governing_deadline": deadline["governing_deadline"],
        "overdue": deadline["overdue"],
        "age_days": deadline["age_seconds"] // 86400,
        "age_level": deadline["age_level"],
        "long_running": deadline["long_running"],
        "extension_count": deadline["extension_count"],
        "follow_up_at": dependency["follow_up_at"] if dependency else None,
        "awaiting_dependency": dependency is not None,
        "acknowledged_until": acknowledged["acknowledgement_until"]
        if acknowledged and acknowledged["active"]
        else None,
    }


def _suspensions(organization_id, vehicle_ids, at):
    """Check suspensions in force at `at` for these vehicles, with item and kit labels."""
    rows = list(
        CheckSuspension.objects.filter(
            organization_id=organization_id, vehicle_id__in=vehicle_ids, recorded_at__lte=at
        ).order_by("recorded_at", "id")
    )
    ended = {str(r.prior_suspension_id) for r in rows if r.action == "END"}
    starts = [r for r in rows if r.action == "START" and str(r.pk) not in ended]
    items = dict(
        Item.objects.filter(
            organization_id=organization_id, pk__in={r.item_id for r in starts}
        ).values_list("pk", "label")
    )
    kits = dict(
        KitInstance.objects.filter(
            organization_id=organization_id,
            pk__in={r.kit_instance_id for r in starts if r.kit_instance_id},
        ).values_list("pk", "label")
    )
    by_vehicle = {}
    for row in starts:
        label = items.get(row.item_id, "Item")
        if row.kit_instance_id:
            label += f" — {kits.get(row.kit_instance_id, 'Kit')}"
        by_vehicle.setdefault(str(row.vehicle_id), []).append(
            {
                "suspension_id": str(row.pk),
                "label": label,
                "deficiency_id": str(row.deficiency_id),
                "started_at": row.recorded_at,
            }
        )
    return by_vehicle


def fleet_readiness(organization_id, *, as_of=None, now):
    """The readiness picture at `as_of` (or `now`); as_of must not be in the future."""
    at = aware(as_of if as_of is not None else now, "as_of")
    if at > now:
        raise ValueError("as_of must not be in the future")
    organization_id = str(organization_id)
    vehicles = Vehicle.objects.filter(organization_id=organization_id)
    if as_of is None:
        # Now: the active fleet. As of an instant: every vehicle, since `active` has no history.
        vehicles = vehicles.filter(active=True)
    listed = list(vehicles.order_by("display_name", "id")[: VEHICLE_LIMIT + 1])
    truncated = len(listed) > VEHICLE_LIMIT
    listed = listed[:VEHICLE_LIMIT]
    ids = [str(v.pk) for v in listed]
    deficiencies = _active_deficiencies(organization_id, ids, at)
    suspensions = _suspensions(organization_id, ids, at)
    result = []
    for vehicle in listed:
        vehicle_id = str(vehicle.pk)
        status = status_at(organization_id, vehicle_id, at)
        counts, progress = _obligations(organization_id, vehicle_id, at)
        rows = deficiencies.get(vehicle_id, [])
        shown = [_deficiency(organization_id, row, at) for row in rows[:DEFICIENCY_LIMIT]]
        by_severity, by_status = {}, {}
        for item in shown:
            label = item["severity"]["label"]
            by_severity[label] = by_severity.get(label, 0) + 1
            by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        result.append(
            VehicleReadiness(
                vehicle_id=vehicle_id,
                display_name=vehicle.display_name,
                active=vehicle.active,
                status=status.status if status else "IN_SERVICE",
                restrictions=status.restrictions if status else None,
                counts=counts,
                progress=progress,
                deficiencies=shown,
                deficiencies_truncated=len(rows) > DEFICIENCY_LIMIT,
                by_severity=by_severity,
                by_status=by_status,
                suspensions=suspensions.get(vehicle_id, []),
            )
        )
    return FleetReadiness(at, at - WINDOW_BACK, at + WINDOW_AHEAD, result, truncated)
