"""Bounded, organization-scoped ORM adapter for immutable obligation projections."""

from datetime import timedelta
from itertools import islice

from cya.checklists.models import Vehicle
from cya.checks.models import (
    CheckEvent,
    CheckObligationLink,
    CheckRecord,
    DeterminationCheck,
)
from cya.checks.state import CheckDecision, CheckSnapshot
from cya.conditions.models import ObligationDetermination
from cya.deficiencies.models import Deficiency, DeficiencyAffectedItem, DeficiencyEvent
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
    for row in result:
        if not isinstance(row, kind) or row.obligation_id not in ids:
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
        selections=tuple(
            SelectionEvidence(
                str(e.vehicle_id),
                str(e.item_id),
                e.due_at,
                e.sequence,
                e.recorded_at,
                str(e.selected_obligation_id) if e.selected_obligation_id else None,
                e.kit_key,
            )
            for e in events
        ),
        determinations=tuple(
            DeterminationEvidence(
                str(d.pk),
                str(d.obligation_id),
                d.recorded_at,
                d.determination_type,
                str(d.prior_determination_id) if d.prior_determination_id else None,
                RuleEvidence(str(d.rule_id), d.rule.recorded_at, d.rule.effective_at)
                if d.rule_id
                else None,
                str(d.actor_user_id) if d.actor_user_id else None,
            )
            for d in determinations
        ),
        statuses=tuple(
            StatusEvidence(
                str(s.pk),
                str(s.vehicle_id),
                s.sequence,
                s.recorded_at,
                s.status,
            )
            for s in statuses
        ),
        checks=(
            ()
            if check_lookup is None
            else _adapter_evidence(
                check_lookup, CheckEvidence, organization_id, vehicle_id, ids, instant_T
            )
        ),
        suspensions=_adapter_evidence(
            suspension_lookup,
            SuspensionEvidence,
            organization_id,
            vehicle_id,
            ids,
            instant_T,
        ),
    )


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
