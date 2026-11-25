"""Load the evidence behind item attention (SPEC 19.3, 19.4); nothing here writes.

Checks: the latest effective check per (item, kit key) as of an instant. Each key's chains are
found newest first, down to its latest one not invalidated, without reading its older history
(cya.obligations.projections.latest_check_roots, issue #63); their whole correction chains and
decisions are replayed by cya.checks.state.effective_checks (which fails closed on a malformed
chain), and invalidated chains are dropped. More rows than the bound raises rather than guessing.

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
from cya.deficiencies.place_units import affected_unit, with_place
from cya.deficiencies.transitions import ACTIVE_STATUSES
from cya.kits.presence import line_unit_keys
from cya.obligations.state import NO_KIT

MAX_ROWS = 10000


def _aware(value, field):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


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


def latest_effective_checks(organization_id, vehicle_id, keys, as_of):
    """CheckFact per requested (item_id, kit_key) that has a non-invalidated chain by as_of.

    Reads only each key's chains from its newest down to its latest valid one, never its older
    history (issue #63; projections.latest_check_roots).
    """
    from cya.kits.presence import line_unit_keys
    from cya.obligations.projections import check_unit_key, latest_check_roots

    _aware(as_of, "as_of")
    wanted = {(str(vehicle_id), str(item_id), str(kit_key)) for item_id, kit_key in keys}
    if not wanted:
        return []
    roots = latest_check_roots(organization_id, wanted, as_of)
    effective = _effective_by_root(organization_id, sorted(roots), as_of) if roots else {}
    facts = {}
    rows = [row for _decision, row in effective.values()]
    units = line_unit_keys({r.kit_requirement_id for r in rows if r.kit_requirement_id})
    for row in rows:
        key = (str(row.item_id), check_unit_key(row, units))
        if (str(vehicle_id), *key) not in wanted:
            continue
        fact = CheckFact(str(row.pk), key[0], key[1], row.recorded_at, row.outcome)
        if key not in facts or (fact.recorded_at, fact.check_id) > (
            facts[key].recorded_at,
            facts[key].check_id,
        ):
            facts[key] = fact
    return list(facts.values())


def _affecting(organization_id, item_ids):
    """Ids of the deficiencies with an affected-item row for one of item_ids.

    A deficiency applies to a row only through those rows (attention.state.applies), so
    one-row reads need no other deficiency (issue #278 Part H).
    """
    return DeficiencyAffectedItem.objects.filter(
        organization_id=organization_id, item_id__in=sorted(item_ids)
    ).values("deficiency_id")


def _deficiency_rows_live(organization_id, vehicle_id, item_ids=None):
    rows = DeficiencyProjection.objects.filter(
        organization_id=organization_id,
        deficiency__vehicle_id=vehicle_id,
        status__in=ACTIVE_STATUSES,
    )
    if item_ids is not None:
        rows = rows.filter(deficiency_id__in=_affecting(organization_id, item_ids))
    rows = rows.values_list("deficiency_id", "status")
    return {str(pk): status for pk, status in rows}


def _deficiency_rows_as_of(organization_id, vehicle_id, as_of, item_ids=None):
    events = DeficiencyEvent.objects.filter(
        organization_id=organization_id,
        deficiency__vehicle_id=vehicle_id,
        recorded_at__lte=as_of,
    )
    if item_ids is not None:
        events = events.filter(deficiency_id__in=_affecting(organization_id, item_ids))
    latest = events.annotate(
        rn=Window(
            RowNumber(),
            partition_by=[F("deficiency_id")],
            order_by=[F("sequence").desc()],
        )
    ).filter(rn=1)
    statuses = {str(e.deficiency_id): e.to_status for e in latest[: MAX_ROWS + 1]}
    if len(statuses) > MAX_ROWS:
        raise ValueError("deficiency history exceeds the row limit")
    return {pk: status for pk, status in statuses.items() if status in ACTIVE_STATUSES}


def deficiency_facts(organization_id, vehicle_id, as_of=None, item_ids=None):
    """Unresolved deficiencies of the vehicle: live projection, or events up to as_of.

    With item_ids, only the deficiencies that can apply to those items (_affecting).
    """
    if as_of is None:
        statuses = _deficiency_rows_live(organization_id, vehicle_id, item_ids)
    else:
        statuses = _deficiency_rows_as_of(
            organization_id, vehicle_id, _aware(as_of, "as_of"), item_ids
        )
    if not statuses:
        return []
    kits, places = {}, {}
    for pk, kit_id, item_id, place in with_place(
        Deficiency.objects.filter(organization_id=organization_id, pk__in=statuses),
        None if as_of is None else _aware(as_of, "as_of"),
    ).values_list("pk", "kit_id", "item_id", "place_requirement_id"):
        kits[pk] = kit_id
        if place:
            places[str(pk)] = (str(item_id), str(place))
    items, units, presence_lines = {}, {}, {}
    scope = DeficiencyAffectedItem.objects.filter(
        organization_id=organization_id, deficiency_id__in=statuses
    ).values_list(
        "deficiency_id", "item_id", "kit_requirement_id", "item_requirement_version__requirement_id"
    )
    for deficiency_id, item_id, line, requirement_id in scope:
        unit = affected_unit(places.get(str(deficiency_id)), str(item_id), line, requirement_id)
        if unit:
            units.setdefault(str(deficiency_id), set()).add((str(item_id), unit))
        else:
            items.setdefault(str(deficiency_id), set()).add(str(item_id))
        if line:
            presence_lines[str(deficiency_id)] = str(line)
    # A deficiency for a kit presence line applies to that line only (KDEF-12).
    line_units = line_unit_keys(set(presence_lines.values()))
    return [
        DeficiencyFact(
            pk,
            status,
            frozenset(items.get(pk, ())),
            str(kits[pk]) if kits.get(pk) else line_units.get(presence_lines.get(pk, ""), NO_KIT),
            frozenset(units.get(pk, ())),
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
    checks = latest_effective_checks(organization_id, vehicle_id, keys, instant)
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
