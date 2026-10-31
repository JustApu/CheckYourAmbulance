"""Read the effective obligations: the latest selection event per anchor (SPEC 19.2, 19.3).

A routine obligation row is effective only when the latest obligation_selection event for its anchor
(vehicle, item, due instant) names it. `as_of` keeps only events recorded by that instant, so a
past answer never changes when a later publication replaces or retires the obligation. Nothing
here writes. Explicit non-routine orders remain effective from creation. Bounds are required so a caller cannot ask for an unbounded history.
"""

from datetime import datetime

from cya.obligations.models import Obligation, ObligationSelection, OdometerObligation
from cya.obligations.projections import get_vehicle_obligations_as_of  # noqa: F401
from cya.obligations.state import NO_KIT


def _aware(value, field):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


def effective_selection(
    organization_id, vehicle_id, item_id, due_at, as_of=None, kit_instance_id=None
):
    """The latest selection event for the anchor recorded by as_of, or None.

    An item inside a kit has one anchor per kit instance (SPEC 1.8.0), so name the instance.
    """
    events = ObligationSelection.objects.filter(
        organization_id=organization_id,
        vehicle_id=vehicle_id,
        item_id=item_id,
        kit_key=kit_instance_id or NO_KIT,
        due_at=due_at,
    )
    if as_of is not None:
        events = events.filter(recorded_at__lte=_aware(as_of, "as_of"))
    return events.select_related("selected_obligation").order_by("-sequence").first()


def effective_obligations(organization_id, start, end, *, vehicle_id=None, as_of=None):
    """Obligations in effect with due instants in closed [start, end], ordered by due instant."""
    _aware(start, "start")
    _aware(end, "end")
    if start > end:
        raise ValueError("start must not exceed end")
    events = ObligationSelection.objects.filter(
        organization_id=organization_id, due_at__gte=start, due_at__lte=end
    )
    if vehicle_id is not None:
        events = events.filter(vehicle_id=vehicle_id)
    if as_of is not None:
        events = events.filter(recorded_at__lte=_aware(as_of, "as_of"))
    tails = {}
    for event in events.select_related("selected_obligation__requirement_version").order_by(
        "vehicle_id", "item_id", "kit_key", "due_at", "-sequence"
    ):
        tails.setdefault((event.vehicle_id, event.item_id, event.kit_key, event.due_at), event)
    chosen = [e.selected_obligation for e in tails.values() if e.selected_obligation is not None]
    adhoc = Obligation.objects.filter(
        organization_id=organization_id, routine=False, due_at__gte=start, due_at__lte=end
    )
    if vehicle_id is not None:
        adhoc = adhoc.filter(vehicle_id=vehicle_id)
    if as_of is not None:
        adhoc = adhoc.filter(recorded_at__lte=as_of)
    chosen.extend(adhoc.select_related("requirement_version"))
    return sorted(
        chosen,
        key=lambda o: (
            o.due_at,
            str(o.vehicle_id),
            str(o.item_id),
            str(o.kit_instance_id or ""),
            str(o.pk),
        ),
    )


def odometer_obligations(organization_id, start, end, *, vehicle_id=None, as_of=None):
    """Effective odometer obligations with due instants in closed [start, end].

    An odometer obligation has no selection stream of its own. It is effective while an
    effective item obligation of the same vehicle and due instant pins the schedule row it was
    created for, so it follows its inspection: a pre-window schedule revision that changes the
    window leaves exactly one effective odometer obligation (the new one), and an inspection
    that was retired leaves none. `as_of` applies to both the rows and the item selections.
    """
    _aware(start, "start")
    _aware(end, "end")
    in_effect = {
        (str(o.vehicle_id), o.due_at, str(o.requirement_version.schedule_rule_id))
        for o in effective_obligations(
            organization_id, start, end, vehicle_id=vehicle_id, as_of=as_of
        )
        if o.routine
    }
    rows = OdometerObligation.objects.filter(
        organization_id=organization_id, due_at__gte=start, due_at__lte=end
    )
    if vehicle_id is not None:
        rows = rows.filter(vehicle_id=vehicle_id)
    if as_of is not None:
        rows = rows.filter(recorded_at__lte=_aware(as_of, "as_of"))
    ordered = rows.order_by("due_at", "vehicle_id", "scheduled_inspection_key")
    return [
        r for r in ordered if (str(r.vehicle_id), r.due_at, str(r.schedule_rule_id)) in in_effect
    ]
