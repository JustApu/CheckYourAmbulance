"""Bounded read models for the crew/supervisor workflows (issue 86 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/86)). No writes."""

from datetime import timedelta

from django.db.models import Max, Q
from django.urls import reverse

from cya.authorization.policy import ResourceContext, permitted
from cya.checks.models import CheckEvent, CheckObligationLink, CheckRecord
from cya.checks.state import CheckDecision, CheckSnapshot, effective_checks
from cya.conditions.models import ObligationDetermination
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyCategory,
    DeficiencyEvent,
    DeficiencyLink,
    DeficiencySeverity,
)

MAX_EVIDENCE = 10000
MAX_RECENT_CHECKS = 200
OUTCOME_LABELS = {
    "PASSED": "Passed",
    "FAILED": "Inspected — failed",
    "UNABLE_TO_INSPECT": "Unable to inspect",
    "NOT_APPLICABLE": "Not applicable",
}


def bounded(query, limit=MAX_EVIDENCE):
    rows = list(query[: limit + 1])
    if len(rows) > limit:
        raise ValueError("workflow evidence exceeds the row limit")
    return rows


def report_options(organization_id, instant):
    """Use the same effective-revision ordering as the report command; never invent defaults."""
    categories = {}
    for row in bounded(
        DeficiencyCategory.objects.filter(
            organization_id=organization_id,
            effective_at__lte=instant,
        ).order_by("stable_category_id", "-revision"),
        1000,
    ):
        categories.setdefault(
            str(row.stable_category_id), {"id": str(row.stable_category_id), "label": row.label}
        )
    levels = {}
    for row in bounded(
        DeficiencySeverity.objects.filter(
            organization_id=organization_id,
            effective_at__lte=instant,
        ).order_by("code", "-revision"),
        1000,
    ):
        levels.setdefault(row.code, row)
    return {
        "categories": sorted(categories.values(), key=lambda c: (c["label"], c["id"])),
        "severities": [
            {"code": r.code, "label": r.label, "description": r.description}
            for r in sorted(levels.values(), key=lambda r: (r.rank, r.code))
            if r.active
        ],
    }


def deficiency_history(deficiency):
    org = deficiency.organization_id
    events = bounded(
        DeficiencyEvent.objects.filter(
            organization_id=org,
            deficiency=deficiency,
        )
        .select_related("actor_user", "assignee")
        .order_by("sequence"),
        1000,
    )
    links = bounded(
        DeficiencyLink.objects.filter(organization_id=org)
        .filter(
            Q(lower=deficiency) | Q(upper=deficiency),
        )
        .order_by("recorded_at", "id"),
        1000,
    )
    current = {}
    for event in links:
        other = event.upper_id if event.lower_id == deficiency.pk else event.lower_id
        current[str(other)] = event.link_type == "ADD"
    related = list(
        Deficiency.objects.filter(
            organization_id=org,
            pk__in=[pk for pk, linked in current.items() if linked],
        ).order_by("reported_at", "id")
    )
    return events, related


def _chains(organization_id, vehicle_id, instant):
    recent = list(
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            recorded_at__lte=instant,
            recorded_at__gte=instant - timedelta(days=14),
        ).order_by("-recorded_at", "-id")[:MAX_RECENT_CHECKS]
    )
    roots = {r.root_check_id for r in recent}
    rows = bounded(
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            root_check_id__in=roots,
            recorded_at__lte=instant,
        )
        .select_related("actor_user", "item", "item_def", "kit")
        .order_by("-recorded_at", "-id")
    )
    events = bounded(
        CheckEvent.objects.filter(
            organization_id=organization_id,
            root_check_id__in=roots,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id")
    )
    states = effective_checks(
        [
            CheckSnapshot(
                str(r.pk),
                str(r.root_check_id),
                str(r.predecessor_check_id) if r.predecessor_check_id else None,
                r.recorded_at,
                r.outcome,
            )
            for r in rows
        ],
        [
            CheckDecision(
                str(e.root_check_id),
                str(e.selected_check_id),
                e.event_type,
                e.recorded_at,
                str(e.replacement_check_id) if e.replacement_check_id else None,
            )
            for e in events
        ],
        as_of=instant,
    )
    return rows, {s.root_id: s for s in states}


def _linked_obligations(organization_id, rows, instant):
    links = bounded(
        CheckObligationLink.objects.filter(
            organization_id=organization_id,
            check_record_id__in=[r.pk for r in rows],
            recorded_at__lte=instant,
        )
        .select_related("obligation")
        .order_by("obligation__due_at", "id")
    )
    determinations = bounded(
        ObligationDetermination.objects.filter(
            organization_id=organization_id,
            obligation_id__in={link.obligation_id for link in links},
            recorded_at__lte=instant,
        )
        .select_related("actor_user")
        .order_by("recorded_at", "id")
    )
    latest = _determination_leaves(determinations)
    histories = {}
    labels = {
        "SUPERVISOR_COMPLETED": "Completed by supervisor — unable to inspect",
        "INVALIDATED": "Invalidated",
        "SUPERSEDED": "Superseded",
        "WAIVED": "Waived",
        "REINSTATED": "Reinstated",
    }
    for determination in determinations:
        determination.status_label = labels[determination.determination_type]
        histories.setdefault(str(determination.obligation_id), []).append(determination)
    grouped = {}
    for link in links:
        grouped.setdefault(str(link.check_record_id), []).append(
            (
                link.obligation,
                latest.get(str(link.obligation_id)),
                histories.get(str(link.obligation_id), []),
            )
        )
    return grouped


def _determination_leaves(rows):
    """Follow supersession, including equal-time events; UUID order is not chronology."""
    grouped = {}
    for row in rows:
        grouped.setdefault(str(row.obligation_id), []).append(row)
    result = {}
    for obligation_id, events in grouped.items():
        identities = {row.pk for row in events}
        parents = {row.prior_determination_id for row in events if row.prior_determination_id}
        leaves = [row for row in events if row.pk not in parents]
        if not parents <= identities or len(leaves) != 1:
            raise ValueError("incomplete or ambiguous determination history")
        result[obligation_id] = leaves[0]
    return result


def check_history(user, vehicle, roles, instant):
    rows, states = _chains(user.organization_id, vehicle.pk, instant)
    linked = _linked_obligations(user.organization_id, rows, instant)
    result = []
    for row in rows:
        state = states[str(row.root_check_id)]
        effective = not state.invalidated and str(row.pk) == state.check_id
        context = ResourceContext(author_id=str(row.actor_user_id))
        own = permitted("correct_check", str(user.pk), roles, context=context)
        correction_outcomes = [
            outcome
            for outcome in ("PASSED", "FAILED")
            if outcome in row.item_def.allowed_outcomes
            and (outcome == "PASSED" or row.outcome == "UNABLE_TO_INSPECT")
        ]
        result.append(
            {
                "record": row,
                "label": OUTCOME_LABELS[row.outcome],
                "state": "Invalidated"
                if state.invalidated
                else "Effective"
                if effective
                else "Superseded",
                "can_correct": effective and own and bool(correction_outcomes),
                "can_invalidate": effective
                and permitted("invalidate_others_check", str(user.pk), roles, context=context),
                "correction_outcomes": correction_outcomes,
                "correction_url": reverse("api-check-corrections", args=[row.pk]),
                "invalidation_url": reverse("api-check-invalidations", args=[row.pk]),
                "obligations": [
                    _obligation_control(
                        user,
                        roles,
                        row,
                        obligation,
                        determination,
                        history,
                        effective and row.outcome == "UNABLE_TO_INSPECT",
                    )
                    for obligation, determination, history in linked.get(str(row.pk), ())
                ],
            }
        )
    return result


def _obligation_control(user, roles, check, obligation, determination, history, active):
    completed = (
        determination is not None and determination.determination_type == "SUPERVISOR_COMPLETED"
    )
    return {
        "id": str(obligation.pk),
        "due_at": obligation.due_at,
        "completed": completed,
        "self_completed": completed and determination.actor_user_id == check.actor_user_id,
        "history": history,
        "can_complete": active
        and not completed
        and permitted("complete_unable_inspect", str(user.pk), roles),
        "complete_url": reverse("api-obligation-unable-completions", args=[obligation.pk]),
        "can_invalidate": active
        and completed
        and permitted("invalidate_unable_completion", str(user.pk), roles),
        "invalidate_url": reverse(
            "api-obligation-determination-invalidations", args=[determination.pk]
        )
        if completed
        else None,
    }


def verification_scope(deficiency):
    """Newest materialized scope and effective Passes; fail closed on missing/large evidence."""
    from cya.deficiencies.effects import satisfied_scope_rows
    from cya.deficiencies.models import VerificationScopeRow

    query = VerificationScopeRow.objects.filter(
        organization_id=deficiency.organization_id, deficiency=deficiency
    )
    revision = query.aggregate(newest=Max("scope_revision"))["newest"]
    rows = bounded(
        query.filter(scope_revision=revision)
        .select_related("item", "kit_instance")
        .order_by("item_id", "instance_key"),
        2000,
    )
    if not rows:
        raise ValueError("verification scope unavailable")
    satisfied = satisfied_scope_rows(str(deficiency.organization_id), str(deficiency.pk), rows)
    for row in rows:
        row.satisfied = row.pk in satisfied
    return rows
