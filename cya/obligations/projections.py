"""Bounded, organization-scoped ORM adapter for immutable obligation projections."""

from datetime import UTC, timedelta
from itertools import islice

from django.db import connection
from django.db.models import F, Q

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
LATEST_SPAN = timedelta(days=2)  # the first window of the attention search (issue #63)


def _bounded(rows, limit=None):
    limit = MAX_ROWS if limit is None else limit  # read at call time, so tests can lower it
    result = tuple(islice(iter(rows), limit + 1))
    if len(result) > limit:
        raise ValueError("projection evidence exceeds 10000 rows; narrow the due interval")
    return result


def _per_vehicle(split):
    """Refuse a page whose rows for any one vehicle exceed MAX_ROWS (the per-vehicle bound)."""
    for found in split.values():
        _bounded(found)
    return split


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


def _key(vehicle_id, item_id, kit_id):
    return (str(vehicle_id), str(item_id), str(kit_id) if kit_id else NO_KIT)


def _exact_keys(keys):
    """Rows of exactly these (vehicle, item, kit) keys; each is one range of the check index."""
    condition = Q(pk__in=())
    for vehicle_id, item_id, kit_key in sorted(keys):
        condition |= Q(
            vehicle_id=vehicle_id,
            item_id=item_id,
            kit_id=None if kit_key == NO_KIT else kit_key,
        )
    return condition


LOOKUP_CHUNK = 200  # keys per statement, so a page of vehicles is not one enormous statement


def _instant(value):
    """A datetime as the raw cursor must receive it (naive UTC), as the ORM would send it."""
    return connection.ops.adapt_datetimefield_value(value)


def _aware(value):
    """A raw cursor returns naive UTC datetimes; the ORM path returns aware ones."""
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


def _newest_older_by_key(organization_id, keys, lower):
    """{key: newest recorded_at at or before `lower`} for each (vehicle, item, kit) key with a check.

    Each key's answer is one backward step on the (organization, vehicle, item, kit, time) index:
    ORDER BY recorded_at DESC LIMIT 1 reads one row per key whatever the history, where a MAX over
    the same index would read every row of the key. The statement names check_record once, as a
    correlated lookup over a derived table of the keys. The former form was one UNION ALL arm per
    key, and MariaDB opens a table instance for every reference in a statement: 150 instances per
    checklist read, each large because the table has 27 indexes, which filled the open-table cache
    and held gigabytes (issue #70).
    A key with no check at or before `lower` is absent. Only the time is returned, so rows that
    tie on recorded_at cannot change the answer (the former form's `-id` tie-break selected among
    rows with the same time and returned that same time). Key ids are bound as the hyphenated
    strings the char(36) id columns hold, so the correlated lookup uses the key index.
    Visibility: under READ COMMITTED each statement sees what is committed when it starts, so
    chunks (and the no-kit and kit statements) can see different commits, as the former union
    and the window reads that follow it already could. That cannot change an answer: a key's
    time comes from exactly one statement, a check belongs to exactly one key, and the roots
    that _attention_roots returns depend only on the evidence, never on how the lookup or the
    windows fell (tests: a qualifying check committed between two chunk statements).
    """
    table = CheckRecord._meta.db_table
    instant = _instant(lower)
    found = {}
    for kitted in (False, True):
        group = sorted(k for k in keys if (k[2] != NO_KIT) == kitted)
        for start in range(0, len(group), LOOKUP_CHUNK):
            chunk = group[start : start + LOOKUP_CHUNK]
            width = 3 if kitted else 2
            first = "SELECT %s AS vehicle_id, %s AS item_id" + (", %s AS kit_id" if kitted else "")
            rest = "SELECT %s, %s" + (", %s" if kitted else "")
            kit = "c.kit_id = k.kit_id" if kitted else "c.kit_id IS NULL"
            sql = (
                "SELECT k.vehicle_id, k.item_id, "
                + ("k.kit_id, " if kitted else "")
                + "(SELECT c.recorded_at FROM "
                + table
                + " c WHERE c.organization_id = %s AND c.vehicle_id = k.vehicle_id"
                + " AND c.item_id = k.item_id AND "
                + kit
                + " AND c.recorded_at <= %s ORDER BY c.recorded_at DESC LIMIT 1) AS recorded_at FROM ("
                + " UNION ALL ".join([first] + [rest] * (len(chunk) - 1))
                + ") k"
            )
            params = [organization_id, instant]
            for vehicle_id, item_id, kit_key in chunk:
                params += [vehicle_id, item_id] + ([kit_key] if kitted else [])
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                for row in cursor.fetchall():
                    newest = row[width]
                    if newest is not None:
                        found[_key(*row[:width], *([] if kitted else [None]))] = _aware(newest)
    return found


def _attention_roots(organization_id, keys, since, instant, limit):
    """Roots of the check chains attention needs at every instant in [since, instant].

    Attention uses each (vehicle, item, kit) key's latest effective check: a chain grows only by
    corrections, so its effective check is its newest row, and attention takes the greatest of
    those over chains not invalidated (attention.state.latest_checks). A history loaded at
    `instant` must replay exactly, without the database, at any earlier instant of its window, so
    this returns every chain with a row in (since, instant], and for each key the chains before
    `since`, newest first, down to and including the first one still valid at `instant` (that
    one is valid at every earlier instant too). The walk back reads (lower, upper] windows and
    jumps to each unresolved key's next older check, so a key's settled history before that
    chain is never read (issue #63). The result depends only on the evidence, never on how the
    windows fell, so a page and a single vehicle agree.
    """
    since = min(since, instant)  # never read a check recorded after the instant
    recent = _bounded(
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=sorted({k[0] for k in keys}),
            item_id__in=sorted({k[1] for k in keys}),
            recorded_at__gt=since,
            recorded_at__lte=instant,
        )
        .order_by("recorded_at", "id")
        .values_list("root_check_id", "vehicle_id", "item_id", "kit_id")[: limit + 1],
        limit,
    )
    roots = {str(root) for root, *key in recent if _key(*key) in keys}
    older_chains = {}  # key -> {root: (newest recorded_at, newest id)}, chains before `since`
    invalidated = set()
    unresolved = set(keys)
    upper = lower = since
    while unresolved:
        # Each key's own newest older check, from the (organization, vehicle, item, kit, time)
        # index alone. Grouped by the exact key: a kit instance with no checks of its own has
        # nothing older and stops here, never walking a sibling kit's history.
        newest_by_key = _newest_older_by_key(organization_id, unresolved, lower)
        older = {key: newest_by_key[key] for key in unresolved if key in newest_by_key}
        unresolved &= set(older)  # a key with nothing older has nothing more to find
        if not unresolved:
            break
        upper, lower = lower, max(older[k] for k in unresolved) - LATEST_SPAN
        window = _bounded(
            # Only the unresolved keys' own rows: a resolved sibling kit or another item can
            # have any amount of settled history inside a wide gap.
            CheckRecord.objects.filter(
                _exact_keys(unresolved),
                organization_id=organization_id,
                recorded_at__gt=lower,
                recorded_at__lte=upper,
            )
            .order_by("recorded_at", "id")
            .values_list("pk", "root_check_id", "vehicle_id", "item_id", "kit_id", "recorded_at")[
                : limit + 1
            ],
            limit,
        )
        seen = set()
        for pk, root, vehicle_id, item_id, kit_id, recorded_at in window:
            key = _key(vehicle_id, item_id, kit_id)
            if key not in unresolved:
                continue
            by_root = older_chains.setdefault(key, {})
            root, newest = str(root), (recorded_at, str(pk))
            if root not in by_root or newest > by_root[root]:
                by_root[root] = newest
            seen.add(root)
        if seen:
            invalidated.update(
                str(root)
                for root in _bounded(
                    CheckEvent.objects.filter(
                        organization_id=organization_id,
                        root_check_id__in=sorted(seen),
                        event_type="INVALIDATION",
                        recorded_at__lte=instant,
                    ).values_list("root_check_id", flat=True)[: limit + 1],
                    limit,
                )
            )
        unresolved = {
            k for k in unresolved if all(r in invalidated for r in older_chains.get(k, {}))
        }
    for by_root in older_chains.values():
        for root, _newest in sorted(by_root.items(), key=lambda item: item[1], reverse=True):
            roots.add(root)
            if root not in invalidated:
                break
    return roots


def latest_check_roots(organization_id, keys, instant):
    """Roots of each (vehicle, item, kit) key's chains, newest first, down to its latest check
    still valid at `instant`: what latest effective checks need, without old history (#63)."""
    return _attention_roots(organization_id, set(keys), instant, instant, MAX_ROWS)


def _history(organization_id, obligations, since, instant, limit, *, current_facts=False):
    """{vehicle id: check and deficiency evidence} for the obligations of one or more vehicles.

    Reads only the checks the projection of those obligations needs at `instant` (issue #63):
    the correction chains linked to them, the chains attention needs (_attention_roots), and the
    decisions and determination supports of those. A vehicle's whole check history is never
    read. Deficiencies are read in full, as before: states list resolved ones too.
    `limit` bounds each query; every vehicle's rows are also bounded by MAX_ROWS.
    """
    vehicle_ids = list(obligations)
    owner = {oid: vehicle_id for vehicle_id, by_id in obligations.items() for oid in by_id}
    keys = {
        _key(vehicle_id, o.item_id, o.kit_instance_id)
        for vehicle_id, by_id in obligations.items()
        for o in by_id.values()
    }
    links = _per_vehicle(
        _by_vehicle(
            _bounded(
                CheckObligationLink.objects.filter(
                    organization_id=organization_id,
                    obligation_id__in=sorted(owner),
                    check_record__vehicle_id__in=vehicle_ids,
                    recorded_at__lte=instant,
                )
                .annotate(record_vehicle_id=F("check_record__vehicle_id"))
                .order_by("recorded_at", "id")[: limit + 1],
                limit,
            ),
            # The obligation and the check must be on the same vehicle.
            lambda link: (
                owner[str(link.obligation_id)]
                if owner.get(str(link.obligation_id)) == str(link.record_vehicle_id)
                else None
            ),
            vehicle_ids,
        )
    )
    linked = sorted({str(link.check_record_id) for found in links.values() for link in found})
    roots = {
        str(root)
        for root in _bounded(
            CheckRecord.objects.filter(organization_id=organization_id, pk__in=linked).values_list(
                "root_check_id", flat=True
            )[: limit + 1],
            limit,
        )
    }
    if not current_facts:
        roots |= _attention_roots(organization_id, keys, since, instant, limit)
    records = _bounded(
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            root_check_id__in=sorted(roots),
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: limit + 1],
        limit,
    )
    rows = _per_vehicle(_by_vehicle(records, lambda r: str(r.vehicle_id), vehicle_ids))
    root_owner = {str(r.root_check_id): str(r.vehicle_id) for r in records}
    decisions = _per_vehicle(
        _by_vehicle(
            _bounded(
                CheckEvent.objects.filter(
                    organization_id=organization_id,
                    root_check_id__in=sorted(root_owner),
                    recorded_at__lte=instant,
                ).order_by("recorded_at", "id")[: limit + 1],
                limit,
            ),
            lambda e: root_owner.get(str(e.root_check_id)),
            vehicle_ids,
        )
    )
    supports = _per_vehicle(
        _by_vehicle(
            _bounded(
                DeterminationCheck.objects.filter(
                    organization_id=organization_id,
                    determination__obligation_id__in=sorted(owner),
                    recorded_at__lte=instant,
                )
                .annotate(for_obligation_id=F("determination__obligation_id"))
                .order_by("recorded_at", "id")[: limit + 1],
                limit,
            ),
            lambda s: owner.get(str(s.for_obligation_id)),
            vehicle_ids,
        )
    )
    if current_facts:
        return {
            v: _history_fields(rows[v], decisions[v], links[v], supports[v], (), {}, ())
            for v in vehicle_ids
        }
    # Every deficiency of the vehicle by the instant: projection states list each one that
    # applies to their item, resolved ones too. Deficiencies are rare next to checks, so this
    # stays far below the bound for years (issue #63 bounds the check reads).
    header_rows = _bounded(
        Deficiency.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")[: limit + 1],
        limit,
    )
    headers = _per_vehicle(_by_vehicle(header_rows, lambda d: str(d.vehicle_id), vehicle_ids))
    deficiency_owner = {str(d.pk): str(d.vehicle_id) for d in header_rows}
    scope = _per_vehicle(
        _by_vehicle(
            _bounded(
                DeficiencyAffectedItem.objects.filter(
                    organization_id=organization_id,
                    deficiency_id__in=sorted(deficiency_owner),
                    recorded_at__lte=instant,
                ).order_by("id")[: limit + 1],
                limit,
            ),
            lambda r: deficiency_owner.get(str(r.deficiency_id)),
            vehicle_ids,
        )
    )
    affected = {}
    for found in scope.values():
        for row in found:
            affected.setdefault(str(row.deficiency_id), set()).add(str(row.item_id))
    events = _per_vehicle(
        _by_vehicle(
            _bounded(
                DeficiencyEvent.objects.filter(
                    organization_id=organization_id,
                    deficiency_id__in=sorted(deficiency_owner),
                    recorded_at__lte=instant,
                ).order_by("sequence", "id")[: limit + 1],
                limit,
            ),
            lambda e: deficiency_owner.get(str(e.deficiency_id)),
            vehicle_ids,
        )
    )
    return {
        v: _history_fields(
            rows[v], decisions[v], links[v], supports[v], headers[v], affected, events[v]
        )
        for v in vehicle_ids
    }


def _database_history(organization_id, vehicle_id, obligations, since, instant):
    return _history(organization_id, {vehicle_id: obligations}, since, instant, MAX_ROWS)[
        vehicle_id
    ]


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
        **_database_history(organization_id, vehicle_id, obligations, start, instant_T),
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
    page = MAX_ROWS * len(vehicle_ids)
    wanted = _bounded(
        Obligation.objects.filter(
            organization_id=organization_id,
            vehicle_id__in=vehicle_ids,
            routine=True,
            pk__in=sorted(owner),
        )
        .order_by("id")
        .values_list("pk", "vehicle_id", "item_id", "kit_instance_id", "recorded_at", "grace_at")[
            : page + 1
        ],
        page,
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
        ).order_by("recorded_at", "id")[: page + 1],
        page,
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


def _page_database_history(organization_id, obligations, since, instant):
    """_database_history for every vehicle of a page, from one query per evidence kind."""
    limit = MAX_ROWS * max(1, len(obligations))
    return _history(organization_id, obligations, since, instant, limit)


def load_projection_histories(vehicle_ids, instant_T, *, organization_id, start=None, end=None):
    """load_projection_history for a page of vehicles, in a fixed number of queries (issue #33).

    Each query of the per-vehicle loader runs once for the page (vehicle_id__in, or the page's
    obligation, check and deficiency ids) and its rows are split by vehicle in the same order, so
    each vehicle's ProjectionHistory equals the per-vehicle loader's with its default adapters
    (stored checks and suspensions, ad hoc orders due in the interval). The row bound is per
    vehicle, as for the per-vehicle loader: a page-wide query reads at most MAX_ROWS rows for each
    vehicle of the page, and more than MAX_ROWS rows for any one vehicle is refused, never cut
    (issue #63; before, one bound for the whole page refused pages of large vehicles). At most MAX_PAGE_VEHICLES distinct vehicles; an unknown or
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
    page = MAX_ROWS * len(vehicle_ids)
    selections = _per_vehicle(
        _by_vehicle(
            _bounded(
                ObligationSelection.objects.filter(
                    organization_id=organization_id,
                    vehicle_id__in=vehicle_ids,
                    due_at__gte=start,
                    due_at__lte=end,
                    recorded_at__lte=instant_T,
                )
                .select_related("selected_obligation__requirement_version")
                .order_by("sequence", "id")[: page + 1],
                page,
            ),
            lambda e: str(e.vehicle_id),
            vehicle_ids,
        )
    )
    adhoc = _per_vehicle(
        _by_vehicle(
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
                .order_by("id")[: page + 1],
                page,
            ),
            lambda o: str(o.vehicle_id),
            vehicle_ids,
        )
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
    determinations = _per_vehicle(
        _by_vehicle(
            _bounded(
                ObligationDetermination.objects.filter(
                    organization_id=organization_id,
                    obligation_id__in=sorted(owner),
                    recorded_at__lte=instant_T,
                )
                .select_related("rule")
                .order_by("recorded_at", "id")[: page + 1],
                page,
            ),
            lambda d: owner.get(str(d.obligation_id)),
            vehicle_ids,
        )
    )
    statuses = _per_vehicle(
        _by_vehicle(
            _bounded(
                VehicleStatusEvent.objects.filter(
                    organization_id=organization_id,
                    vehicle_id__in=vehicle_ids,
                    recorded_at__lte=instant_T,
                ).order_by("sequence", "id")[: page + 1],
                page,
            ),
            lambda s: str(s.vehicle_id),
            vehicle_ids,
        )
    )
    database = _page_database_history(organization_id, obligations, start, instant_T)
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
