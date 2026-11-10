"""RPT-5a fleet-readiness Overdue and Missed counts (SPEC 1.29.0 and 1.30.0; issue #80 Step 2).

(a) A visible OVERDUE obligation leaves the Overdue count once a later-due obligation of the
exact vehicle, item and physical kit instance has qualifying satisfaction from an effective
linked check (OQ-63). The later obligation may be retired, far in the future, or displayed as
Exempt, Waived or Suspended: what counts is the satisfaction itself, judged by the same
evaluator helpers as every other read, not its display state.
(b) Missed counts visible obligations still OVERDUE after their pinned late limit L whose due
instant is no more than seven days before the read instant (inclusive).

The later-satisfaction source is bounded and independent of the display window and of current
selection: for each vehicle and key with a visible OVERDUE row it reads that key's obligations
recorded by T, whose early window opened by T and that are due after the key's earliest visible
OVERDUE row. Any qualifying later obligation is in that set, because its qualifying check was
recorded between its early instant and T. The read is page-wide, so its query count does not grow
with the number of vehicles. A current read uses the stored projection facts of those obligations;
a row that is missing, of another facts version or refreshed after T is replayed at T by the same
bounded replay that builds current facts (cya.obligations.current._source_facts). A historical
read replays every candidate at T, so it uses only records known at T.
A retirement's SUPERSEDED disposition does not revoke satisfaction for this count (owner decision,
issue #80 comment 6758233632): the immutable RETIRE selection names it, and only that node is
skipped when choosing the effective determination. Every query is bounded and refuses overflow.
"""

from datetime import timedelta

from django.db.models import Q

from cya.obligations.current import BATCH_SIZE, FACTS_VERSION, _restore, _source_facts
from cya.obligations.models import Obligation, ObligationProjection, ObligationSelection
from cya.obligations.projections import MAX_ROWS, _bounded
from cya.obligations.state import (
    NO_KIT,
    CheckEvidence,
    DeterminationEvidence,
    ObligationEvidence,
    State,
    _checks_for,
    _determination_for,
    _satisfaction,
)

MISSED_AGE = timedelta(days=7)


def _key(obligation):
    return (obligation.item_id, obligation.kit_instance_id or NO_KIT)


def retirement_dispositions(organization_id, obligation_ids, at):
    """{obligation id: ids of the SUPERSEDED determinations its RETIRE selections appended by `at`}.

    The relationship is validated: the disposition must belong to the obligation the RETIRE
    event replaced, or the read fails rather than guessing.
    """
    if not obligation_ids:
        return {}
    limit = MAX_ROWS * max(1, len(obligation_ids))
    rows = _bounded(
        ObligationSelection.objects.filter(
            organization_id=organization_id,
            selection_type="RETIRE",
            supersession_determination__isnull=False,
            recorded_at__lte=at,
            previous_selection__selected_obligation_id__in=sorted(obligation_ids),
        ).values_list(
            "previous_selection__selected_obligation_id",
            "supersession_determination_id",
            "supersession_determination__obligation_id",
            "supersession_determination__determination_type",
        )[: limit + 1],
        limit,
    )
    result = {}
    for retired, determination, owner, kind in rows:
        if str(owner) != str(retired) or kind != "SUPERSEDED":
            raise ValueError("retirement disposition does not belong to the retired obligation")
        result.setdefault(str(retired), set()).add(str(determination))
    return result


def clearing_determination(obligation, determinations, retired_ids, at):
    """The effective determination for this count: the leaf, skipping retirement dispositions.

    Walks prior pointers only while the leaf is one of the validated retirement dispositions, so
    a genuine INVALIDATED or check-correction SUPERSEDED determination is never bypassed.
    """
    leaf = _determination_for(obligation, determinations, at)
    by_id = {d.id: d for d in determinations}
    for _ in range(len(determinations) + 1):
        if leaf is None or leaf.id not in retired_ids:
            return leaf
        prior = leaf.prior_determination_id
        if prior is None:
            return None
        if prior not in by_id or by_id[prior].recorded_at > at:
            raise ValueError("retirement disposition's prior determination is missing")
        leaf = by_id[prior]
    raise ValueError("determination history has a cycle")


def _candidates(organization_id, at, floor):
    """Bounded page-wide read of the later obligations that could clear a visible OVERDUE row."""
    scope = Q()
    for (vehicle_id, item_id, kit_key), due_at in floor.items():
        scope |= Q(vehicle_id=vehicle_id, item_id=item_id, kit_key=kit_key, due_at__gt=due_at)
    limit = MAX_ROWS * max(1, len({key[0] for key in floor}))
    return _bounded(
        Obligation.objects.filter(
            scope, organization_id=organization_id, recorded_at__lte=at, early_at__lte=at
        )
        .select_related("requirement_version")
        .order_by("vehicle_id", "id")[: limit + 1],
        limit,
    )


def _candidate_facts(organization_id, at, rows, current):
    """{obligation id: facts at T}: stored current facts where usable, otherwise replayed at T."""
    facts = {}
    if current:
        limit = len(rows)
        for oid, stored, refreshed_at in _bounded(
            ObligationProjection.objects.filter(
                organization_id=organization_id, obligation_id__in=[r.pk for r in rows]
            ).values_list("obligation_id", "facts", "refreshed_at")[: limit + 1],
            limit,
        ):
            if isinstance(stored, dict) and stored.get("version") == FACTS_VERSION:
                if refreshed_at <= at:
                    facts[str(oid)] = stored
    by_vehicle = {}
    for row in rows:
        if str(row.pk) not in facts:
            by_vehicle.setdefault(str(row.vehicle_id), []).append(row)
    for vehicle_rows in by_vehicle.values():
        for offset in range(0, len(vehicle_rows), BATCH_SIZE):
            batch = vehicle_rows[offset : offset + BATCH_SIZE]
            facts.update(_source_facts(str(organization_id), batch, at))
    return facts


def latest_clearing_due(organization_id, at, overdue, *, current):
    """{(vehicle, item, kit key): latest due of a later obligation with qualifying satisfaction}.

    `overdue` is every visible OVERDUE obligation (ObligationEvidence) of the page.
    """
    floor = {}
    for obligation in overdue:
        key = (obligation.vehicle_id, *_key(obligation))
        floor[key] = min(floor.get(key, obligation.due_at), obligation.due_at)
    if not floor:
        return {}
    rows = _candidates(organization_id, at, floor)
    if not rows:
        return {}
    facts = _candidate_facts(organization_id, at, rows, current)
    retired = retirement_dispositions(organization_id, list(facts), at)
    result = {}
    for oid, fact in facts.items():
        obligation = _restore(ObligationEvidence, fact["obligation"])
        checks = tuple(_restore(CheckEvidence, c) for c in fact["checks"])
        determinations = tuple(_restore(DeterminationEvidence, d) for d in fact["determinations"])
        determination = clearing_determination(
            obligation, determinations, retired.get(oid, set()), at
        )
        if _satisfaction(obligation, _checks_for(obligation, checks, at), determination):
            key = (obligation.vehicle_id, *_key(obligation))
            result[key] = max(result.get(key, obligation.due_at), obligation.due_at)
    return result


def apply_rpt5a(organization_id, at, vehicles, *, current):
    """Adjust Overdue (a) and set Missed (b) for a page: `vehicles` is [(states, counts), ...]."""
    overdue = [s.obligation for states, _ in vehicles for s in states if s.state == State.OVERDUE]
    clearing = latest_clearing_due(organization_id, at, overdue, current=current)
    for states, counts in vehicles:
        suppressed = missed = 0
        for state in states:
            if state.state != State.OVERDUE:
                continue
            obligation = state.obligation
            later = clearing.get((obligation.vehicle_id, *_key(obligation)))
            if later is not None and later > obligation.due_at:
                suppressed += 1
            if at > obligation.late_limit() and obligation.due_at >= at - MISSED_AGE:
                missed += 1
        counts["OVERDUE"] -= suppressed
        counts["MISSED"] = missed
