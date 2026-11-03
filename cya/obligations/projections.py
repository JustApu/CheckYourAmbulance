"""Bounded, organization-scoped ORM adapter for immutable obligation projections."""

from datetime import timedelta
from itertools import islice

from django.db.models import F

from cya.checklists.models import Vehicle
from cya.checks.models import (
    CheckEvent,
    CheckObligationLink,
    CheckRecord,
    DeterminationCheck,
)
from cya.checks.state import CheckDecision, CheckSnapshot
from cya.conditions.models import ObligationDetermination
from cya.deficiencies.models import (
    CheckSuspension,
    Deficiency,
    DeficiencyAffectedItem,
    DeficiencyEvent,
)
from cya.obligations.history import (
    RecordedCheck,
    RecordedDeficiency,
    RecordedDeficiencyEvent,
    RecordedLink,
    RecordedSupport,
)
from cya.obligations.models import Obligation, ObligationSelection
from cya.obligations.state import (
    NO_KIT,
    CheckEvidence,
    DeterminationEvidence,
    ObligationEvidence,
    ProjectionHistory,
    RuleEvidence,
    SelectionEvidence,
    StatusEvidence,
    SuspensionEvidence,
    aware,
    rebuild_vehicle_projection,
)
from cya.operations.models import VehicleStatusEvent

MAX_ROWS = 10000
MAX_PAGE_VEHICLES = 50  # vehicles one batch read accepts (issue #33)
MAX_INTERVAL = timedelta(days=31)


def _bounded(rows):
    result = tuple(islice(iter(rows), MAX_ROWS + 1))
    if len(result) > MAX_ROWS:
        raise ValueError("projection evidence exceeds 10000 rows; narrow the due interval")
    return result


def _bounds(instant, start, end):
    aware(instant)
    start = instant - timedelta(days=7) if start is None else aware(start, "start")
    end = instant + timedelta(hours=48) if end is None else aware(end, "end")
    if not timedelta(0) <= end - start <= MAX_INTERVAL:
        raise ValueError("due interval must be ordered and at most 31 days")
    return start, end


def _obligation(row):
    version = row.requirement_version
    return ObligationEvidence(
        str(row.pk),
        str(row.organization_id),
        str(row.vehicle_id),
        str(row.item_id),
        str(version.pk),
        str(version.publication_id),
        str(version.schedule_rule_id) if version.schedule_rule_id else None,
        str(version.conditional_rule_id) if version.conditional_rule_id else None,
        row.recorded_at,
        row.early_at,
        row.due_at,
        row.grace_at,
        row.routine,
        str(row.kit_instance_id) if row.kit_instance_id else None,
        row.explanation,
        str(row.actor_user_id) if row.actor_user_id else None,
        str(row.command_id),
    )


def _selection(row):
    return SelectionEvidence(
        str(row.vehicle_id),
        str(row.item_id),
        row.due_at,
        row.sequence,
        row.recorded_at,
        str(row.selected_obligation_id) if row.selected_obligation_id else None,
        row.kit_key,
    )


def _determination(row):
    return DeterminationEvidence(
        str(row.pk),
        str(row.obligation_id),
        row.recorded_at,
        row.determination_type,
        str(row.prior_determination_id) if row.prior_determination_id else None,
        RuleEvidence(str(row.rule_id), row.rule.recorded_at, row.rule.effective_at)
        if row.rule_id
        else None,
        str(row.actor_user_id) if row.actor_user_id else None,
    )


def _status(row):
    return StatusEvidence(
        str(row.pk), str(row.vehicle_id), row.sequence, row.recorded_at, row.status
    )


def stored_suspensions(*, organization_id, vehicle_id, obligation_ids, as_of):
    """Supervisor check suspensions known at as_of for the requested obligations (SPEC 8.9).

    The default suspension adapter. A suspension names an item (and physical kit instance) of the
    vehicle; it covers each routine obligation of that item and instance whose window it can
    touch. Its end is the END row recorded by as_of, so a later closure never changes an earlier
    answer. The evaluator decides whether it is in force at min(T, G). Bounded like every other
    evidence collection: too much evidence raises rather than returning a partial answer.
    """
    wanted = _bounded(
        Obligation.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            routine=True,
            pk__in=obligation_ids,
        )
        .order_by("id")
        .values_list("pk", "item_id", "kit_instance_id", "recorded_at", "grace_at")[: MAX_ROWS + 1]
    )
    by_key = {}
    for pk, item_id, kit_id, recorded_at, grace_at in wanted:
        key = (str(item_id), str(kit_id) if kit_id else NO_KIT)
        by_key.setdefault(key, []).append((str(pk), recorded_at, grace_at))
    if not by_key:
        return ()
    rows = _bounded(
        CheckSuspension.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            item_id__in={key[0] for key in by_key},
            recorded_at__lte=as_of,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    ended = {str(r.prior_suspension_id): r.recorded_at for r in rows if r.action == "END"}
    result = []
    for row in rows:
        if row.action != "START":
            continue
        end = ended.get(str(row.pk))
        for obligation_id, recorded_at, grace_at in by_key.get(
            (str(row.item_id), row.instance_key), ()
        ):
            # In force at some min(T, G) with T at or after the obligation was recorded.
            if row.recorded_at <= grace_at and (end is None or end > min(recorded_at, grace_at)):
                result.append(
                    SuspensionEvidence(
                        obligation_id,
                        row.recorded_at,
                        end,
                        str(row.pk),
                        str(row.deficiency_id),
                    )
                )
    return tuple(result)


def _adapter_evidence(lookup, kind, organization_id, vehicle_id, ids, instant):
    if lookup is None or not ids:
        return ()
    result = _bounded(
        lookup(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            obligation_ids=ids,
            as_of=instant,
        )
    )
    requested = set(ids)
    for row in result:
        if not isinstance(row, kind) or row.obligation_id not in requested:
            raise ValueError("adapter returned unsupported or unrequested evidence")
        if isinstance(row, CheckEvidence) and (
            row.organization_id != organization_id or row.vehicle_id != vehicle_id
        ):
            raise ValueError("check adapter returned evidence outside the requested scope")
    return result


def _database_history(organization_id, vehicle_id, obligations, ids, instant):
    item_ids = {o.item_id for o in obligations.values()}
    rows = _bounded(
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            item_id__in=item_ids,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    roots = {r.root_check_id for r in rows}
    decisions = _bounded(
        CheckEvent.objects.filter(
            organization_id=organization_id,
            root_check_id__in=roots,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    links = _bounded(
        CheckObligationLink.objects.filter(
            organization_id=organization_id,
            obligation_id__in=ids,
            check_record__vehicle_id=vehicle_id,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    supports = _bounded(
        DeterminationCheck.objects.filter(
            organization_id=organization_id,
            determination__obligation_id__in=ids,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    headers = _bounded(
        Deficiency.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    deficiency_ids = [d.pk for d in headers]
    scope = _bounded(
        DeficiencyAffectedItem.objects.filter(
            organization_id=organization_id,
            deficiency_id__in=deficiency_ids,
            recorded_at__lte=instant,
        ).order_by("id")[: MAX_ROWS + 1]
    )
    items = {}
    for row in scope:
        items.setdefault(str(row.deficiency_id), set()).add(str(row.item_id))
    events = _bounded(
        DeficiencyEvent.objects.filter(
            organization_id=organization_id,
            deficiency_id__in=deficiency_ids,
            recorded_at__lte=instant,
        ).order_by("sequence", "id")[: MAX_ROWS + 1]
    )
    return _history_fields(rows, decisions, links, supports, headers, items, events)


def _history_fields(rows, decisions, links, supports, headers, items, events):
    """The check and deficiency evidence of one vehicle, from its database rows."""
    return dict(
        check_records=tuple(
            RecordedCheck(
                CheckSnapshot(
                    str(r.pk),
                    str(r.root_check_id),
                    str(r.predecessor_check_id) if r.predecessor_check_id else None,
                    r.recorded_at,
                    r.outcome,
                ),
                str(r.item_id),
                str(r.kit_id) if r.kit_id else NO_KIT,
                str(r.actor_user_id) if r.actor_user_id else None,
            )
            for r in rows
        ),
        check_decisions=tuple(
            CheckDecision(
                str(e.root_check_id),
                str(e.selected_check_id),
                e.event_type,
                e.recorded_at,
                str(e.replacement_check_id) if e.replacement_check_id else None,
            )
            for e in decisions
        ),
        check_links=tuple(
            RecordedLink(str(link.check_record_id), str(link.obligation_id), link.recorded_at)
            for link in links
        ),
        check_supports=tuple(
            RecordedSupport(str(s.check_record_id), str(s.determination_id), s.recorded_at)
            for s in supports
        ),
        deficiency_headers=tuple(
            RecordedDeficiency(
                str(d.pk),
                d.recorded_at,
                frozenset(items.get(str(d.pk), ())),
                str(d.kit_id) if d.kit_id else NO_KIT,
            )
            for d in headers
        ),
        deficiency_events=tuple(
            RecordedDeficiencyEvent(
                str(e.pk),
                str(e.deficiency_id),
                e.sequence,
                str(e.predecessor_event_id) if e.predecessor_event_id else None,
                e.recorded_at,
                e.from_status,
                e.to_status,
            )
            for e in events
        ),
    )


def load_projection_history(
    vehicle_id,
    instant_T,
    *,
    organization_id,
    start=None,
    end=None,
    check_lookup=None,
    suspension_lookup=None,
    include_all_ad_hoc=False,
):
    """Read evidence recorded by T. Empty adapters fail closed to unsatisfied, never Pass.

    Supervisor check suspensions are read from storage (stored_suspensions) unless a
    suspension_lookup adapter is supplied in its place.
    No mutable Vehicle.active filter is used: deactivation cannot erase historical answers.
    The caller supplies trusted organization scope and performs request authorization.
    """
    start, end = _bounds(instant_T, start, end)
    organization_id, vehicle_id = str(organization_id), str(vehicle_id)
    Vehicle.objects.get(pk=vehicle_id, organization_id=organization_id)
    events = _bounded(
        ObligationSelection.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            due_at__gte=start,
            due_at__lte=end,
            recorded_at__lte=instant_T,
        )
        .select_related("selected_obligation__requirement_version")
        .order_by("sequence", "id")[: MAX_ROWS + 1]
    )
    obligations = {
        str(e.selected_obligation_id): _obligation(e.selected_obligation)
        for e in events
        if e.selected_obligation_id is not None
    }
    adhoc_rows = Obligation.objects.filter(
        organization_id=organization_id,
        vehicle_id=vehicle_id,
        routine=False,
        recorded_at__lte=instant_T,
    )
    if not include_all_ad_hoc:
        adhoc_rows = adhoc_rows.filter(due_at__gte=start, due_at__lte=end)
    adhoc = _bounded(
        adhoc_rows.select_related("requirement_version").order_by("id")[: MAX_ROWS + 1]
    )
    obligations.update({str(o.pk): _obligation(o) for o in adhoc})
    _bounded(obligations.values())
    ids = tuple(sorted(obligations))
    determinations = _bounded(
        ObligationDetermination.objects.filter(
            organization_id=organization_id,
            obligation_id__in=ids,
            recorded_at__lte=instant_T,
        )
        .select_related("rule")
        .order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    statuses = _bounded(
        VehicleStatusEvent.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            recorded_at__lte=instant_T,
        ).order_by("sequence")[: MAX_ROWS + 1]
    )
    return ProjectionHistory(
        **_database_history(organization_id, vehicle_id, obligations, ids, instant_T),
        organization_id=organization_id,
        use_stored_checks=check_lookup is None,
        obligations=tuple(obligations.values()),
        selections=tuple(_selection(e) for e in events),
        determinations=tuple(_determination(d) for d in determinations),
        statuses=tuple(_status(s) for s in statuses),
        checks=(
            ()
            if check_lookup is None
            else _adapter_evidence(
                check_lookup, CheckEvidence, organization_id, vehicle_id, ids, instant_T
            )
        ),
        suspensions=_adapter_evidence(
            stored_suspensions if suspension_lookup is None else suspension_lookup,
            SuspensionEvidence,
            organization_id,
            vehicle_id,
            ids,
            instant_T,
        ),
    )


def _by_vehicle(rows, vehicle_of, vehicle_ids):
    """Split rows into {vehicle id: tuple}, keeping their order; vehicle_of None drops a row.

    A page-wide query ordered like the per-vehicle one yields, for each vehicle, the same rows in
    the same order as that vehicle's own query.
    """
    split = {vehicle_id: [] for vehicle_id in vehicle_ids}
    for row in rows:
        vehicle_id = vehicle_of(row)
        if vehicle_id in split:
            split[vehicle_id].append(row)
    return {vehicle_id: tuple(found) for vehicle_id, found in split.items()}


def _page_suspensions(organization_id, obligations, instant):
    """stored_suspensions for every vehicle of a page, from two queries."""
    vehicle_ids = list(obligations)
    owner = {oid: vehicle_id for vehicle_id, by_id in obligations.items() for oid in by_id}
    result = {vehicle_id: [] for vehicle_id in vehicle_ids}
    if not owner:
        return {vehicle_id: () for vehicle_id in vehicle_ids}
    wanted = _bounded(
        Obligation.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            routine=True,
            pk__in=sorted(owner),
        )
        .order_by("id")
        .values_list("pk", "vehicle_id", "item_id", "kit_instance_id", "recorded_at", "grace_at")[
            : MAX_ROWS + 1
        ]
    )
    by_key = {}
    for pk, vehicle_id, item_id, kit_id, recorded_at, grace_at in wanted:
        key = (str(vehicle_id), str(item_id), str(kit_id) if kit_id else NO_KIT)
        by_key.setdefault(key, []).append((str(pk), recorded_at, grace_at))
    if not by_key:
        return {vehicle_id: () for vehicle_id in vehicle_ids}
    rows = _bounded(
        CheckSuspension.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            item_id__in={key[1] for key in by_key},
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    ended = {str(r.prior_suspension_id): r.recorded_at for r in rows if r.action == "END"}
    for row in rows:
        if row.action != "START":
            continue
        end = ended.get(str(row.pk))
        vehicle_id = str(row.vehicle_id)
        for obligation_id, recorded_at, grace_at in by_key.get(
            (vehicle_id, str(row.item_id), row.instance_key), ()
        ):
            # The same in-force test as stored_suspensions.
            if row.recorded_at <= grace_at and (end is None or end > min(recorded_at, grace_at)):
                result[vehicle_id].append(
                    SuspensionEvidence(
                        obligation_id, row.recorded_at, end, str(row.pk), str(row.deficiency_id)
                    )
                )
    return {vehicle_id: _bounded(found) for vehicle_id, found in result.items()}


def _page_database_history(organization_id, obligations, instant):
    """_database_history for every vehicle of a page, from one query per evidence kind."""
    vehicle_ids = list(obligations)
    owner = {oid: vehicle_id for vehicle_id, by_id in obligations.items() for oid in by_id}
    items = {
        vehicle_id: {o.item_id for o in by_id.values()} for vehicle_id, by_id in obligations.items()
    }
    records = _bounded(
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            item_id__in=set().union(*items.values()),
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    # Vehicles share checklist items: keep a record only for an item of its own vehicle's window.
    rows = _by_vehicle(
        records,
        lambda r: str(r.vehicle_id) if str(r.item_id) in items[str(r.vehicle_id)] else None,
        vehicle_ids,
    )
    root_owner = {str(r.root_check_id): v for v, found in rows.items() for r in found}
    decisions = _by_vehicle(
        _bounded(
            CheckEvent.objects.filter(
                organization_id=organization_id,
                root_check_id__in=sorted(root_owner),
                recorded_at__lte=instant,
            ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
        ),
        lambda e: root_owner.get(str(e.root_check_id)),
        vehicle_ids,
    )
    links = _by_vehicle(
        _bounded(
            CheckObligationLink.objects.filter(
                organization_id=organization_id,
                obligation_id__in=sorted(owner),
                check_record__vehicle_id__in=vehicle_ids,
                recorded_at__lte=instant,
            )
            .annotate(record_vehicle_id=F("check_record__vehicle_id"))
            .order_by("recorded_at", "id")[: MAX_ROWS + 1]
        ),
        # The per-vehicle query needs the obligation and the check on the same vehicle.
        lambda link: (
            owner[str(link.obligation_id)]
            if owner.get(str(link.obligation_id)) == str(link.record_vehicle_id)
            else None
        ),
        vehicle_ids,
    )
    supports = _by_vehicle(
        _bounded(
            DeterminationCheck.objects.filter(
                organization_id=organization_id,
                determination__obligation_id__in=sorted(owner),
                recorded_at__lte=instant,
            )
            .annotate(for_obligation_id=F("determination__obligation_id"))
            .order_by("recorded_at", "id")[: MAX_ROWS + 1]
        ),
        lambda s: owner.get(str(s.for_obligation_id)),
        vehicle_ids,
    )
    header_rows = _bounded(
        Deficiency.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: MAX_ROWS + 1]
    )
    headers = _by_vehicle(header_rows, lambda d: str(d.vehicle_id), vehicle_ids)
    deficiency_owner = {str(d.pk): str(d.vehicle_id) for d in header_rows}
    scope = _bounded(
        DeficiencyAffectedItem.objects.filter(
            organization_id=organization_id,
            deficiency_id__in=sorted(deficiency_owner),
            recorded_at__lte=instant,
        ).order_by("id")[: MAX_ROWS + 1]
    )
    affected = {}
    for row in scope:
        affected.setdefault(str(row.deficiency_id), set()).add(str(row.item_id))
    events = _by_vehicle(
        _bounded(
            DeficiencyEvent.objects.filter(
                organization_id=organization_id,
                deficiency_id__in=sorted(deficiency_owner),
                recorded_at__lte=instant,
            ).order_by("sequence", "id")[: MAX_ROWS + 1]
        ),
        lambda e: deficiency_owner.get(str(e.deficiency_id)),
        vehicle_ids,
    )
    return {
        v: _history_fields(
            rows[v], decisions[v], links[v], supports[v], headers[v], affected, events[v]
        )
        for v in vehicle_ids
    }


def load_projection_histories(vehicle_ids, instant_T, *, organization_id, start=None, end=None):
    """load_projection_history for a page of vehicles, in a fixed number of queries (issue #33).

    Each query of the per-vehicle loader runs once for the page (vehicle_id__in, or the page's
    obligation, check and deficiency ids) and its rows are split by vehicle in the same order, so
    each vehicle's ProjectionHistory equals the per-vehicle loader's with its default adapters
    (stored checks and suspensions, ad hoc orders due in the interval). The row bound applies to
    each page-wide query: more than MAX_ROWS rows is refused, never cut, and a caller that hits
    it reads fewer vehicles at a time. At most MAX_PAGE_VEHICLES distinct vehicles; an unknown or
    other organization's vehicle raises Vehicle.DoesNotExist, as the per-vehicle loader does. The
    caller supplies trusted organization scope and performs request authorization.
    """
    start, end = _bounds(instant_T, start, end)
    organization_id = str(organization_id)
    vehicle_ids = tuple(dict.fromkeys(str(v) for v in vehicle_ids))
    if len(vehicle_ids) > MAX_PAGE_VEHICLES:
        raise ValueError(f"a batch read takes at most {MAX_PAGE_VEHICLES} vehicles")
    if not vehicle_ids:
        return {}
    known = Vehicle.objects.filter(organization_id=organization_id, pk__in=vehicle_ids)
    if {str(pk) for pk in known.values_list("pk", flat=True)} != set(vehicle_ids):
        raise Vehicle.DoesNotExist("a requested vehicle does not exist in this organization")
    selections = _by_vehicle(
        _bounded(
            ObligationSelection.objects.filter(
                organization_id=organization_id,
                vehicle_id__in=vehicle_ids,
                due_at__gte=start,
                due_at__lte=end,
                recorded_at__lte=instant_T,
            )
            .select_related("selected_obligation__requirement_version")
            .order_by("sequence", "id")[: MAX_ROWS + 1]
        ),
        lambda e: str(e.vehicle_id),
        vehicle_ids,
    )
    adhoc = _by_vehicle(
        _bounded(
            Obligation.objects.filter(
                organization_id=organization_id,
                vehicle_id__in=vehicle_ids,
                routine=False,
                recorded_at__lte=instant_T,
                due_at__gte=start,
                due_at__lte=end,
            )
            .select_related("requirement_version")
            .order_by("id")[: MAX_ROWS + 1]
        ),
        lambda o: str(o.vehicle_id),
        vehicle_ids,
    )
    obligations = {}
    for vehicle_id in vehicle_ids:
        # Built exactly as load_projection_history builds it, so the order matches too.
        by_id = {
            str(e.selected_obligation_id): _obligation(e.selected_obligation)
            for e in selections[vehicle_id]
            if e.selected_obligation_id is not None
        }
        by_id.update({str(o.pk): _obligation(o) for o in adhoc[vehicle_id]})
        _bounded(by_id.values())
        obligations[vehicle_id] = by_id
    owner = {oid: vehicle_id for vehicle_id, by_id in obligations.items() for oid in by_id}
    determinations = _by_vehicle(
        _bounded(
            ObligationDetermination.objects.filter(
                organization_id=organization_id,
                obligation_id__in=sorted(owner),
                recorded_at__lte=instant_T,
            )
            .select_related("rule")
            .order_by("recorded_at", "id")[: MAX_ROWS + 1]
        ),
        lambda d: owner.get(str(d.obligation_id)),
        vehicle_ids,
    )
    statuses = _by_vehicle(
        _bounded(
            VehicleStatusEvent.objects.filter(
                organization_id=organization_id,
                vehicle_id__in=vehicle_ids,
                recorded_at__lte=instant_T,
            ).order_by("sequence", "id")[: MAX_ROWS + 1]
        ),
        lambda s: str(s.vehicle_id),
        vehicle_ids,
    )
    database = _page_database_history(organization_id, obligations, instant_T)
    suspensions = _page_suspensions(organization_id, obligations, instant_T)
    return {
        vehicle_id: ProjectionHistory(
            **database[vehicle_id],
            organization_id=organization_id,
            use_stored_checks=True,
            obligations=tuple(obligations[vehicle_id].values()),
            selections=tuple(_selection(e) for e in selections[vehicle_id]),
            determinations=tuple(_determination(d) for d in determinations[vehicle_id]),
            statuses=tuple(_status(s) for s in statuses[vehicle_id]),
            checks=(),
            suspensions=suspensions[vehicle_id],
        )
        for vehicle_id in vehicle_ids
    }


def get_vehicle_obligations_as_of(
    vehicle_id,
    instant_T,
    *,
    organization_id,
    start=None,
    end=None,
    check_lookup=None,
    suspension_lookup=None,
    include_all_ad_hoc=False,
):
    """Derive states at T, using immutable selection/requirement pins and evidence known at T.

    Defaults to due instants T-7 days through T+48 hours. Supply bounds for older history.
    Does not regenerate obligations or evaluate/write fresh carry-over determinations.
    """
    start, end = _bounds(instant_T, start, end)
    history = load_projection_history(
        vehicle_id,
        instant_T,
        organization_id=organization_id,
        start=start,
        end=end,
        check_lookup=check_lookup,
        suspension_lookup=suspension_lookup,
        include_all_ad_hoc=include_all_ad_hoc,
    )
    return rebuild_vehicle_projection(
        history, str(vehicle_id), instant_T, start, end, include_all_ad_hoc=include_all_ad_hoc
    )
