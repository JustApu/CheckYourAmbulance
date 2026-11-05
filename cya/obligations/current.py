"""Disposable current evidence for checklist/readiness reads (SPEC 17.4).

Immutable events remain authoritative. Writers call refresh_obligations inside their own
transaction, after domain writes. Projection rows are the final lock group, acquired by
obligation ID, and replay occurs AFTER acquiring them. No vehicle/item/deficiency lock is
acquired here. Under the configured READ COMMITTED isolation a waiting refresh sees the
preceding commit, so concurrent source writers leave a serially reproducible projection.
"""

import json
from bisect import bisect_right
from dataclasses import asdict
from datetime import datetime, timedelta

from django.db import connection
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from cya.checks.state import effective_checks
from cya.conditions.models import ObligationDetermination
from cya.obligations.history import replay_checks
from cya.obligations.models import Obligation, ObligationProjection, ObligationSelection
from cya.obligations.projections import (
    MAX_ROWS,
    _bounded,
    _determination,
    _history,
    _obligation,
    _status,
    stored_suspensions,
)
from cya.obligations.state import (
    CheckEvidence,
    DeterminationEvidence,
    ObligationEvidence,
    ProjectionHistory,
    RuleEvidence,
    StatusEvidence,
    SuspensionEvidence,
    aware,
    evaluate_obligation,
)
from cya.operations.models import VehicleStatusEvent

BATCH_SIZE = 250
DATE_FIELDS = frozenset(
    ("recorded_at", "early_at", "due_at", "grace_at", "voided_at", "effective_at", "ended_at")
)


def _json(value):
    # DjangoJSONEncoder rounds datetimes to milliseconds; keep all six digits for boundaries.
    return json.loads(json.dumps(value, default=lambda v: v.isoformat(), sort_keys=True))


def _restore(kind, values):
    values = dict(values)
    for key in DATE_FIELDS & values.keys():
        if values[key] is not None:
            values[key] = datetime.fromisoformat(values[key])
    for key in ("rule", "na_rule"):
        if values.get(key) is not None:
            values[key] = _restore(RuleEvidence, values[key])
    return kind(**values)


def evaluate_facts(facts, instant):
    """Use the SAME pure evaluator as historical replay, without replaying check chains."""
    if facts.get("version") != 1:
        raise ValueError("unsupported obligation projection; rebuild required")
    if not any(facts[key] for key in ("checks", "determinations", "statuses", "suspensions")):
        return evaluate_obligation(_restore(ObligationEvidence, facts["obligation"]), instant)
    return evaluate_obligation(
        _restore(ObligationEvidence, facts["obligation"]),
        instant,
        checks=tuple(_restore(CheckEvidence, r) for r in facts["checks"]),
        determinations=tuple(_restore(DeterminationEvidence, r) for r in facts["determinations"]),
        status_events=tuple(_restore(StatusEvidence, r) for r in facts["statuses"]),
        suspensions=tuple(_restore(SuspensionEvidence, r) for r in facts["suspensions"]),
    )


def selected_ids(organization_id, ids, instant):
    later = ObligationSelection.objects.filter(
        organization_id=organization_id,
        vehicle_id=OuterRef("vehicle_id"),
        item_id=OuterRef("item_id"),
        kit_key=OuterRef("kit_key"),
        due_at=OuterRef("due_at"),
        sequence__gt=OuterRef("sequence"),
        recorded_at__lte=instant,
    )
    return set(
        ObligationSelection.objects.filter(
            organization_id=organization_id,
            selected_obligation_id__in=ids,
            recorded_at__lte=instant,
        )
        .exclude(Exists(later))
        .values_list("selected_obligation_id", flat=True)
    )


def compile_history(history, instant):
    """Reduce source evidence to effective facts; preserve all RPT-7 counting semantics."""
    linked, _ = replay_checks(history, instant)
    by_check = {}
    for check in linked:
        by_check.setdefault(check.obligation_id, []).append(check)
    determinations, suspensions, roots = {}, {}, {}
    for row in history.determinations:
        determinations.setdefault(row.obligation_id, []).append(row)
    for row in history.suspensions:
        suspensions.setdefault(row.obligation_id, []).append(row)
    root_of = {r.snapshot.id: r.snapshot.root_id for r in history.check_records}
    chains = {
        c.root_id: [int(c.invalidated), len(c.superseded_ids)]
        for c in effective_checks(
            (r.snapshot for r in history.check_records), history.check_decisions, as_of=instant
        )
    }
    for link in history.check_links:
        if link.recorded_at <= instant:
            root = root_of[link.check_id]
            roots.setdefault(link.obligation_id, {})[root] = chains[root]
    result = {}
    for obligation in history.obligations:
        status = max(
            (s for s in history.statuses if s.recorded_at <= min(instant, obligation.due_at)),
            key=lambda s: s.sequence,
            default=None,
        )
        result[obligation.id] = _json(
            {
                "version": 1,
                "obligation": asdict(obligation),
                "checks": [asdict(c) for c in by_check.get(obligation.id, ())],
                "determinations": [asdict(d) for d in determinations.get(obligation.id, ())],
                "statuses": [asdict(status)] if status else [],
                "suspensions": [asdict(s) for s in suspensions.get(obligation.id, ())],
                "roots": roots.get(obligation.id, {}),
            }
        )
    return result


def _source_facts(organization_id, rows, instant):
    """Bounded replay of exactly the affected obligations; no attention/deficiency history."""
    vehicle_id = str(rows[0].vehicle_id)
    obligations = {str(r.pk): _obligation(r) for r in rows}
    ids = tuple(obligations)
    database = _history(
        organization_id,
        {vehicle_id: obligations},
        instant,
        instant,
        MAX_ROWS,
        current_facts=True,
    )[vehicle_id]
    determinations = _bounded(
        ObligationDetermination.objects.filter(
            organization_id=organization_id,
            obligation_id__in=ids,
            recorded_at__lte=instant,
        )
        .select_related("rule")
        .order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    history = ProjectionHistory(
        organization_id=organization_id,
        obligations=tuple(obligations.values()),
        determinations=tuple(_determination(d) for d in determinations),
        # Status is joined at read time. Persisting it would make a single status change
        # refresh every future obligation while the command holds the vehicle lock.
        statuses=(),
        suspensions=stored_suspensions(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            obligation_ids=ids,
            as_of=instant,
        ),
        **database,
    )
    return compile_history(history, instant)


def refresh_obligations(organization_id, obligation_ids, *, dry_run=False):
    """Compare/rebuild affected rows in the caller's transaction; return differing IDs.

    A dry run locks existing rows but never creates or updates anything. Missing rows and
    retired selections are differences too. The caller must supply organization scope.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("projection refresh requires a transaction")
    organization_id = str(organization_id)
    ids = sorted(set(str(i) for i in obligation_ids if i is not None))
    differences = []
    # Lock the entire requested set in global ID order before replaying any batch. Missing
    # row creation uses the same order, so first-use and rebuild races serialize too.
    cached = {}
    for oid in ids:
        row = (
            ObligationProjection.objects.select_for_update()
            .filter(
                organization_id=organization_id,
                obligation_id=oid,
            )
            .first()
        )
        if row is None and not dry_run:
            source = Obligation.objects.get(organization_id=organization_id, pk=oid)
            ObligationProjection.objects.get_or_create(
                obligation=source,
                defaults={
                    "organization_id": organization_id,
                    "vehicle_id": source.vehicle_id,
                    "due_at": source.due_at,
                    "routine": source.routine,
                    "refreshed_at": timezone.now(),
                },
            )
            row = ObligationProjection.objects.select_for_update().get(
                organization_id=organization_id,
                obligation_id=oid,
            )
        if row is not None:
            cached[oid] = row
    instant = timezone.now()  # after lock waits; never replay with a stale receipt timestamp
    for offset in range(0, len(ids), BATCH_SIZE):
        rows = list(
            Obligation.objects.filter(
                organization_id=organization_id,
                pk__in=ids[offset : offset + BATCH_SIZE],
            )
            .select_related("requirement_version")
            .order_by("vehicle_id", "id")
        )
        by_vehicle = {}
        for row in rows:
            by_vehicle.setdefault(str(row.vehicle_id), []).append(row)
        facts = {}
        for vehicle_rows in by_vehicle.values():
            facts.update(_source_facts(organization_id, vehicle_rows, instant))
        selected = selected_ids(organization_id, [r.pk for r in rows], instant)
        changed = []
        for source in rows:
            expected_selected = not source.routine or source.pk in selected
            row = cached.get(source.pk)
            expected = (
                facts[source.pk],
                expected_selected,
                source.vehicle_id,
                source.due_at,
                source.routine,
            )
            if (
                row is None
                or (row.facts, row.selected, row.vehicle_id, row.due_at, row.routine) != expected
            ):
                differences.append(str(source.pk))
                if not dry_run:
                    row.facts, row.selected, row.vehicle_id, row.due_at, row.routine = expected
                    row.refreshed_at = instant
                    changed.append(row)
        if changed:
            ObligationProjection.objects.bulk_update(
                changed, ("facts", "selected", "vehicle_id", "due_at", "routine", "refreshed_at")
            )
    return differences


def status_timeline(events):
    """Build a searchable, sequence-correct status timeline for current reads/rebuild checks."""
    grouped = {}
    for row in events:
        grouped.setdefault(str(row.vehicle_id), []).append(_status(row))
    result = {}
    for vehicle, timeline in grouped.items():
        timeline.sort(key=lambda event: (event.recorded_at, event.sequence))
        # Current reads attach a status to many obligations. Serialize each immutable event
        # once per read instead of repeating the dataclass/JSON conversion per obligation.
        facts_by_event = {event.id: _json(asdict(event)) for event in timeline}
        times, newest_by_sequence = [], []
        newest = None
        for event in timeline:
            times.append(event.recorded_at)
            if newest is None or event.sequence > newest.sequence:
                newest = event
            newest_by_sequence.append(facts_by_event[newest.id])
        result[vehicle] = (times, newest_by_sequence)
    return result


def attach_status_fact(facts, vehicle_id, due_at, instant, timelines):
    """Return projection facts with the immutable status event applicable to this read."""
    timeline = timelines.get(str(vehicle_id))
    status_facts = []
    if timeline:
        times, newest_by_sequence = timeline
        index = bisect_right(times, min(instant, due_at))
        if index:
            status_facts = [newest_by_sequence[index - 1]]
    result = dict(facts)
    result["statuses"] = status_facts
    return result


def current_rows(organization_id, vehicle_ids, instant, *, include_all_ad_hoc=False):
    """Read current facts, or return None for a missing/partially selected cache.

    Comparing selected source IDs protects first deployment/recovery from a partial cache.
    Rows refreshed after the captured instant are replayed individually at that instant.
    Historical requests never call this function. No cache population occurs in a reader.
    """
    aware(instant)
    vehicle_ids = [str(v) for v in vehicle_ids]
    if len(vehicle_ids) > 50:
        raise ValueError("current projection accepts at most 50 vehicles")
    start, end = instant - timedelta(days=7), instant + timedelta(hours=48)
    scope = Q(due_at__gte=start, due_at__lte=end)
    if include_all_ad_hoc:
        scope |= Q(routine=False)
    sources = Obligation.objects.filter(
        scope,
        organization_id=organization_id,
        vehicle_id__in=vehicle_ids,
        recorded_at__lte=instant,
    )
    page_limit = MAX_ROWS * max(1, len(vehicle_ids))
    # Limit in SQL before QuerySet iteration, which otherwise buffers every matching row.
    source_rows = _bounded(sources.values_list("pk", "routine")[: page_limit + 1], page_limit)
    routine_ids = [oid for oid, routine in source_rows if routine]
    selected = selected_ids(organization_id, routine_ids, instant)
    selected.update(oid for oid, routine in source_rows if not routine)
    rows = _bounded(
        ObligationProjection.objects.filter(
            scope,
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            selected=True,
        ).values_list("obligation_id", "vehicle_id", "facts", "refreshed_at", "due_at")[
            : page_limit + 1
        ],
        page_limit,
    )
    if {r[0] for r in rows} != selected:
        return None
    # A save can commit between the reader capturing T and loading this table. Replay ONLY
    # those newer rows at T, rather than making a busy vehicle replay its whole nine-day
    # window on every live-update reload. Explicit as_of readers never enter this adapter.
    newer = {r[0] for r in rows if r[3] > instant}
    replayed = {}
    for vehicle in vehicle_ids:
        wanted = sorted(r[0] for r in rows if str(r[1]) == vehicle and r[0] in newer)
        for offset in range(0, len(wanted), BATCH_SIZE):
            sources = list(
                Obligation.objects.filter(
                    organization_id=organization_id,
                    vehicle_id=vehicle,
                    pk__in=wanted[offset : offset + BATCH_SIZE],
                )
                .select_related("requirement_version")
                .order_by("id")
            )
            replayed.update(_source_facts(str(organization_id), sources, instant))
    statuses = _bounded(
        VehicleStatusEvent.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            recorded_at__lte=instant,
        ).order_by("vehicle_id", "recorded_at", "sequence")[: page_limit + 1],
        page_limit,
    )
    timelines = status_timeline(statuses)
    result = {v: [] for v in vehicle_ids}
    for oid, vehicle, facts, _stamp, due_at in rows:
        facts = replayed.get(oid, facts)
        result[str(vehicle)].append(attach_status_fact(facts, vehicle, due_at, instant, timelines))
    for found in result.values():
        _bounded(found)
    return result
