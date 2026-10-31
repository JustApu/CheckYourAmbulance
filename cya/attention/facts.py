"""Load the evidence behind item attention (SPEC 19.3, 19.4); nothing here writes.

Checks: the latest effective check per (item, kit key) as of an instant. The newest rows of each
key are read with a window query, their whole correction chains and decisions are replayed by
cya.checks.state.effective_checks (which fails closed on a malformed chain), and invalidated
chains are dropped. Reading more rows per key than WINDOWS allows without finding a
non-invalidated chain raises rather than guessing.

Deficiencies: with no instant, the live deficiency_projection (disposable cache, fast); with an
instant, the append-only events up to it (authoritative). verify_attention compares the two
(SPEC 17.4).
"""

from datetime import datetime

from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from cya.attention.state import CheckFact, DeficiencyFact, attention_states
from cya.checks.models import CheckEvent, CheckRecord
from cya.checks.state import CheckDecision, CheckSnapshot, effective_checks
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyAffectedItem,
    DeficiencyEvent,
    DeficiencyProjection,
)
from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.obligations.state import NO_KIT

# Newest rows read per key, widened once when every row seen belongs to an invalidated chain.
WINDOWS = (25, 400)
MAX_ROWS = 10000


def _aware(value, field):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


def _newest_rows(organization_id, vehicle_id, item_ids, as_of, per_key):
    rows = (
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            item_id__in=item_ids,
            recorded_at__lte=as_of,
        )
        .annotate(
            rn=Window(
                RowNumber(),
                partition_by=[F("item_id"), F("kit_id")],
                order_by=[F("recorded_at").desc(), F("id").desc()],
            )
        )
        .filter(rn__lte=per_key)
    )
    return list(rows)


def _effective_by_root(organization_id, roots, as_of):
    chain = list(
        CheckRecord.objects.filter(
            organization_id=organization_id, root_check_id__in=roots, recorded_at__lte=as_of
        )
    )
    events = list(
        CheckEvent.objects.filter(
            organization_id=organization_id, root_check_id__in=roots, recorded_at__lte=as_of
        )
    )
    if len(chain) + len(events) > MAX_ROWS:
        raise ValueError("check history exceeds the row limit")
    by_id = {str(row.pk): row for row in chain}
    effective = effective_checks(
        [
            CheckSnapshot(
                str(row.pk),
                str(row.root_check_id),
                str(row.predecessor_check_id) if row.predecessor_check_id else None,
                row.recorded_at,
                row.outcome,
            )
            for row in chain
        ],
        [
            CheckDecision(
                str(event.root_check_id),
                str(event.selected_check_id),
                event.event_type,
                event.recorded_at,
                str(event.replacement_check_id) if event.replacement_check_id else None,
            )
            for event in events
        ],
        as_of=as_of,
    )
    return {e.root_id: (e, by_id[e.check_id]) for e in effective if not e.invalidated}


def latest_effective_checks(organization_id, vehicle_id, item_ids, as_of):
    """CheckFact per (item_id, kit_key) that has a non-invalidated chain recorded by as_of."""
    _aware(as_of, "as_of")
    item_ids = sorted(set(item_ids))
    if not item_ids:
        return []
    facts = {}
    pending = None  # (item, kit) keys still without a candidate after a full window
    for per_key in WINDOWS:
        rows = _newest_rows(organization_id, vehicle_id, item_ids, as_of, per_key)
        if len(rows) > MAX_ROWS:
            raise ValueError("check history exceeds the row limit")
        full = {}
        for row in rows:
            key = (str(row.item_id), str(row.kit_id) if row.kit_id else NO_KIT)
            full[key] = full.get(key, 0) + 1
        roots = {str(row.root_check_id) for row in rows}
        effective = _effective_by_root(organization_id, roots, as_of) if roots else {}
        facts = {}
        for _root, (_decision, row) in effective.items():
            key = (str(row.item_id), str(row.kit_id) if row.kit_id else NO_KIT)
            fact = CheckFact(str(row.pk), key[0], key[1], row.recorded_at, row.outcome)
            if key not in facts or (fact.recorded_at, fact.check_id) > (
                facts[key].recorded_at,
                facts[key].check_id,
            ):
                facts[key] = fact
        # A key is settled once it has a candidate, or its window held every row it has.
        pending = {key for key, count in full.items() if count >= per_key and key not in facts}
        if not pending:
            return list(facts.values())
    raise ValueError("no effective check found within the history window; narrow the request")


def _deficiency_rows_live(organization_id, vehicle_id):
    rows = DeficiencyProjection.objects.filter(
        organization_id=organization_id,
        deficiency__vehicle_id=vehicle_id,
        status__in=ACTIVE_STATUSES,
    ).values_list("deficiency_id", "status")
    return {str(pk): status for pk, status in rows}


def _deficiency_rows_as_of(organization_id, vehicle_id, as_of):
    latest = (
        DeficiencyEvent.objects.filter(
            organization_id=organization_id,
            deficiency__vehicle_id=vehicle_id,
            recorded_at__lte=as_of,
        )
        .annotate(
            rn=Window(
                RowNumber(),
                partition_by=[F("deficiency_id")],
                order_by=[F("sequence").desc()],
            )
        )
        .filter(rn=1)
    )
    statuses = {str(e.deficiency_id): e.to_status for e in latest[: MAX_ROWS + 1]}
    if len(statuses) > MAX_ROWS:
        raise ValueError("deficiency history exceeds the row limit")
    return {pk: status for pk, status in statuses.items() if status in ACTIVE_STATUSES}


def deficiency_facts(organization_id, vehicle_id, as_of=None):
    """Unresolved deficiencies of the vehicle: live projection, or events up to as_of."""
    if as_of is None:
        statuses = _deficiency_rows_live(organization_id, vehicle_id)
    else:
        statuses = _deficiency_rows_as_of(organization_id, vehicle_id, _aware(as_of, "as_of"))
    if not statuses:
        return []
    kits = dict(
        Deficiency.objects.filter(organization_id=organization_id, pk__in=statuses).values_list(
            "pk", "kit_id"
        )
    )
    items = {}
    scope = DeficiencyAffectedItem.objects.filter(
        organization_id=organization_id, deficiency_id__in=statuses
    ).values_list("deficiency_id", "item_id")
    for deficiency_id, item_id in scope:
        items.setdefault(str(deficiency_id), set()).add(str(item_id))
    return [
        DeficiencyFact(
            pk,
            status,
            frozenset(items.get(pk, ())),
            str(kits[pk]) if kits.get(pk) else NO_KIT,
        )
        for pk, status in sorted(statuses.items())
    ]


def item_attention(organization_id, vehicle_id, keys, as_of=None):
    """{(item_id, kit_key): Attention} for the requested keys, live or as of an instant.

    keys use string ids and NO_KIT for an item outside a kit. With as_of None the check
    evidence is read up to the present and deficiencies come from the live projection.
    """
    keys = list(keys)
    instant = timezone.now() if as_of is None else _aware(as_of, "as_of")
    checks = latest_effective_checks(
        organization_id, vehicle_id, {item_id for item_id, _kit in keys}, instant
    )
    return attention_states(keys, checks, deficiency_facts(organization_id, vehicle_id, as_of))


def verify_attention(organization_id, vehicle_id, keys, instant):
    """Recompute the live result from the events at instant and list every disagreement.

    Returns [] when the cached projection agrees with the authoritative events. A caller that
    runs this concurrently with writers may see a transient difference; run it on a quiet
    vehicle or retry.
    """
    live = item_attention(organization_id, vehicle_id, keys, None)
    replay = item_attention(organization_id, vehicle_id, keys, _aware(instant, "instant"))
    return [
        {"key": key, "live": live[key], "recomputed": replay[key]}
        for key in sorted(live)
        if live[key] != replay[key]
    ]
