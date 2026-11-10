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


CANDIDATE_ROUND = 3  # later obligations evaluated per key per round, newest first


def _candidates(organization_id, at, floor):
    """{key: [(due_at, obligation id), ...] newest first}: later obligations that could clear.

    One bounded range read for the page's vehicles (no per-key OR clause and no model
    instances): due after the page's earliest visible OVERDUE row, early window opened by T,
    recorded by T. Rows of other keys or not after their key's floor are dropped here.
    """
    vehicles = sorted({key[0] for key in floor})
    limit = MAX_ROWS * len(vehicles)
    rows = _bounded(
        Obligation.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicles,
            due_at__gt=min(floor.values()),
            early_at__lte=at,
            recorded_at__lte=at,
        ).values_list("pk", "vehicle_id", "item_id", "kit_key", "due_at")[: limit + 1],
        limit,
    )
    result = {}
    for oid, vehicle_id, item_id, kit_key, due_at in rows:
        key = (str(vehicle_id), str(item_id), str(kit_key))
        if key in floor and due_at > floor[key]:
            result.setdefault(key, []).append((due_at, str(oid)))
    for found in result.values():
        found.sort(reverse=True)
    return result


def _candidate_facts(organization_id, at, ids, current):
    """{obligation id: facts at T}: stored current facts where usable, otherwise replayed at T."""
    facts = {}
    if current:
        limit = len(ids)
        for oid, stored, refreshed_at in _bounded(
            ObligationProjection.objects.filter(
                organization_id=organization_id, obligation_id__in=ids
            ).values_list("obligation_id", "facts", "refreshed_at")[: limit + 1],
            limit,
        ):
            if isinstance(stored, dict) and stored.get("version") == FACTS_VERSION:
                if refreshed_at <= at:
                    facts[str(oid)] = stored
    missing = [oid for oid in ids if oid not in facts]
    if missing:
        rows = _bounded(
            Obligation.objects.filter(organization_id=organization_id, pk__in=missing)
            .select_related("requirement_version")
            .order_by("vehicle_id", "id")[: len(missing) + 1],
            len(missing),
        )
        by_vehicle = {}
        for row in rows:
            by_vehicle.setdefault(str(row.vehicle_id), []).append(row)
        for vehicle_rows in by_vehicle.values():
            for offset in range(0, len(vehicle_rows), BATCH_SIZE):
                batch = vehicle_rows[offset : offset + BATCH_SIZE]
                facts.update(_source_facts(str(organization_id), batch, at))
    return facts


def _qualifies(fact, retired_ids, at):
    obligation = _restore(ObligationEvidence, fact["obligation"])
    checks = tuple(_restore(CheckEvidence, c) for c in fact["checks"])
    determinations = tuple(_restore(DeterminationEvidence, d) for d in fact["determinations"])
    determination = clearing_determination(obligation, determinations, retired_ids, at)
    return _satisfaction(obligation, _checks_for(obligation, checks, at), determination) is not None


def latest_clearing_due(organization_id, at, overdue, *, current):
    """{(vehicle, item, kit key): latest due of a later obligation with qualifying satisfaction}.

    `overdue` is every visible OVERDUE obligation (ObligationEvidence) of the page. Each key's
    candidates are evaluated newest first in rounds of CANDIDATE_ROUND, and a key stops at its
    first qualifying one: only the latest qualifying due instant matters (strictly later than a
    row's own due instant suppresses it), so older candidates of a resolved key are never read.
    """
    floor = {}
    for obligation in overdue:
        key = (obligation.vehicle_id, *_key(obligation))
        floor[key] = min(floor.get(key, obligation.due_at), obligation.due_at)
    if not floor:
        return {}
    pending = _candidates(organization_id, at, floor)
    result = {}
    while pending:  # bounded: every round consumes up to CANDIDATE_ROUND candidates per key
        batch = {key: found[:CANDIDATE_ROUND] for key, found in pending.items()}
        ids = [oid for found in batch.values() for _due, oid in found]
        facts = _candidate_facts(organization_id, at, ids, current)
        retired = retirement_dispositions(organization_id, ids, at)
        absent = [oid for oid in ids if oid not in facts]
        if absent:  # fail closed: never treat a candidate without evidence as unsatisfied
            raise ValueError(f"{len(absent)} candidate obligations have no evidence at T")
        for key, found in batch.items():
            for due_at, oid in found:
                if _qualifies(facts[oid], retired.get(oid, set()), at):
                    result[key] = due_at
                    break
        pending = {
            key: found[CANDIDATE_ROUND:]
            for key, found in pending.items()
            if key not in result and len(found) > CANDIDATE_ROUND
        }
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
