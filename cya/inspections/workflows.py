"""Bounded read models for the crew/supervisor workflows (issue 86 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/86)). No writes."""

from datetime import timedelta

from django.db.models import Count, Exists, Max, OuterRef, Q
from django.urls import reverse

from cya.authorization.policy import ResourceContext, permitted
from cya.checks.commands import MISTAKEN_NORMAL_ACTION
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
from cya.paper.invalidation import ACTION as PAPER_BATCH_ACTION
from cya.paper.models import PaperEntryBatch, PaperEntryInvalidationEvent, PaperEntryPerformer

MAX_EVIDENCE = 10000
MAX_RECENT_CHECKS = 200
# Paper entries listed on the check-history page, and replacement batches a mistaken-Normal
# invalidation offers (SPEC 8.0.0 LATE-6, INV-8; issue #9 B6b).
RECENT_PAPER_DAYS, MAX_PAPER_BATCHES = 14, 20
MAX_REPLACEMENT_CANDIDATES = 20
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


def _chains(organization_id, vehicle_id, instant, *, root_ids=None):
    if root_ids is None:
        recent = list(
            CheckRecord.objects.filter(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                recorded_at__lte=instant,
                recorded_at__gte=instant - timedelta(days=14),
            ).order_by("-recorded_at", "-id")[:MAX_RECENT_CHECKS]
        )
        roots = {r.root_check_id for r in recent}
    else:
        roots = set(root_ids)
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


def check_history(user, vehicle, roles, instant, *, root_ids=None, include_obligations=True):
    rows, states = _chains(user.organization_id, vehicle.pk, instant, root_ids=root_ids)
    if root_ids is not None:
        wanted = {str(root_id) for root_id in root_ids}
        rows = [row for row in rows if str(row.root_check_id) in wanted]
    linked = _linked_obligations(user.organization_id, rows, instant) if include_obligations else {}
    batch_ids = sorted({str(r.paper_entry_batch_id) for r in rows if r.paper_entry_batch_id})
    performers = _performer_names(user.organization_id, batch_ids)
    invalidated_batches = set(_batch_invalidations(user.organization_id, batch_ids, instant))
    roots = {str(r.pk): r for r in rows if r.pk == r.root_check_id}
    result = []
    for row in rows:
        state = states[str(row.root_check_id)]
        effective = not state.invalidated and str(row.pk) == state.check_id
        context = ResourceContext(author_id=str(row.actor_user_id))
        paper = (
            {
                "batch_id": str(row.paper_entry_batch_id),
                "anchor": f"paper-{row.paper_entry_batch_id}",
                "performed_at": row.performed_at,
                # SPEC 8.0.0 LATE-3, RPT-4: the performers are credited with the work; the
                # entry actor is shown only as the person who entered it.
                "performers": performers.get(str(row.paper_entry_batch_id), []),
                "entered_by": row.actor_user.display_name,
                "entered_at": row.recorded_at,
                "batch_invalidated": str(row.paper_entry_batch_id) in invalidated_batches,
            }
            if row.paper_entry_batch_id
            else None
        )
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
                "paper": paper,
                # Offered on the detail view only: it reads the vehicle's paper entries.
                "mistaken_normal": _mistaken_normal(
                    user, roles, row, roots.get(str(row.root_check_id)), instant
                )
                if include_obligations and effective
                else None,
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
                ]
                if include_obligations
                else [],
            }
        )
    return result


def _obligation_control(user, roles, check, obligation, determination, history, active):
    completed = (
        determination is not None and determination.determination_type == "SUPERVISOR_COMPLETED"
    )
    # SPEC 8.0.0 SCH-1, SCH-2: timing is judged at the check's qualifying instant t_c (a paper
    # check's performance instant, otherwise its server instant) against the grace deadline G.
    # It is shown apart from the source: a paper entry can be On time.
    qualifying = check.performed_at or check.recorded_at
    return {
        "id": str(obligation.pk),
        "due_at": obligation.due_at,
        "timing": "On time" if qualifying <= obligation.grace_at else "Late completion",
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


def _performer_names(organization_id, batch_ids):
    """{batch id: performer display names in name order} in one read (LATE-3)."""
    names = {}
    if not batch_ids:
        return names
    for batch_id, name in (
        PaperEntryPerformer.objects.filter(organization_id=organization_id, batch_id__in=batch_ids)
        .order_by("performer__display_name", "performer_id")
        .values_list("batch_id", "performer__display_name")
    ):
        names.setdefault(str(batch_id), []).append(name)
    return names


def _batch_invalidations(organization_id, batch_ids, instant):
    """{batch id: its invalidation event} among batch_ids, as known at instant (INV-8)."""
    if not batch_ids:
        return {}
    return {
        str(event.batch_id): event
        for event in PaperEntryInvalidationEvent.objects.filter(
            organization_id=organization_id, batch_id__in=batch_ids, recorded_at__lte=instant
        ).select_related("actor_user")
    }


def _batch_entry(batch, performers):
    return {
        "batch_id": str(batch.pk),
        "template_label": batch.checklist_version.template.label,
        "revision": batch.checklist_version.revision,
        "performed_at": batch.performed_at,
        "recorded_at": batch.recorded_at,
        "performers": performers.get(str(batch.pk), []),
        "entered_by": batch.actor_user.display_name,
    }


def paper_batches(user, vehicle, roles, instant):
    """The vehicle's paper entries for the check-history page (SPEC 8.0.0 LATE-6, INV-8).

    Batches entered in the last RECENT_PAPER_DAYS days, newest entry first, at most
    MAX_PAPER_BATCHES. Only the entry actor continues copying a batch (Part B2). A Supervisor or
    Administrator other than the entry actor may invalidate it (the matrix's other_only rule
    with the entry actor as author), so the entry actor is offered no form: the server would
    answer SELF_INVALIDATION_FORBIDDEN.
    """
    organization_id = user.organization_id
    batches = list(
        PaperEntryBatch.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle.pk,
            recorded_at__lte=instant,
            recorded_at__gte=instant - timedelta(days=RECENT_PAPER_DAYS),
        )
        .select_related("checklist_version__template", "actor_user")
        .order_by("-recorded_at", "-pk")[:MAX_PAPER_BATCHES]
    )
    ids = [str(batch.pk) for batch in batches]
    performers = _performer_names(organization_id, ids)
    invalidations = _batch_invalidations(organization_id, ids, instant)
    counts = (
        {
            str(batch_id): count
            for batch_id, count in CheckRecord.objects.filter(
                organization_id=organization_id,
                paper_entry_batch_id__in=ids,
                recorded_at__lte=instant,
            )
            .order_by()
            .values_list("paper_entry_batch_id")
            .annotate(count=Count("pk"))
        }
        if ids
        else {}
    )
    supervising = bool({"SUPERVISOR", "ADMINISTRATOR"} & set(roles))
    result = []
    for batch in batches:
        invalidation = invalidations.get(str(batch.pk))
        own = str(batch.actor_user_id) == str(user.pk)
        result.append(
            {
                **_batch_entry(batch, performers),
                "anchor": f"paper-{batch.pk}",
                "check_entries": counts.get(str(batch.pk), 0),
                "invalidation": invalidation,
                "own": own,
                "continue_url": reverse("inspection-paper-entry", args=[batch.pk])
                if own and invalidation is None
                else None,
                "can_invalidate": invalidation is None
                and permitted(
                    PAPER_BATCH_ACTION,
                    str(user.pk),
                    roles,
                    context=ResourceContext(author_id=str(batch.actor_user_id)),
                ),
                "own_needs_another": invalidation is None and own and supervising,
            }
        )
    return result


def mistaken_normal_candidates(organization_id, root, instant):
    """Paper entries that may replace the routine check `root` mistakenly saved in Normal mode.

    Mirrors the command's rules (cya.checks.commands._replacement_refusal; SPEC 8.0.0 INV-8,
    LATE-6): a batch of the same vehicle, not invalidated, performed no later than root was
    saved, holding a paper check of the same work (item, kit instance, kit line or presence and
    item requirement) whose chain is not invalidated. Newest performance first, at most
    MAX_REPLACEMENT_CANDIDATES. The command judges the choice again when it is sent.
    """
    same_work = CheckRecord.objects.filter(
        organization_id=organization_id,
        vehicle_id=root.vehicle_id,
        paper_entry_batch__isnull=False,
        item_id=root.item_id,
        kit_id=root.kit_id,
        kit_requirement_id=root.kit_requirement_id,
        kit_presence=root.kit_presence,
        recorded_at__lte=instant,
    ).exclude(
        Exists(
            CheckEvent.objects.filter(
                root_check_id=OuterRef("root_check_id"),
                event_type="INVALIDATION",
                recorded_at__lte=instant,
            )
        )
    )
    if root.item_requirement_version_id is None:
        same_work = same_work.filter(item_requirement_version__isnull=True)
    else:
        same_work = same_work.filter(
            item_requirement_version__requirement_id=root.item_requirement_version.requirement_id
        )
    batches = list(
        PaperEntryBatch.objects.filter(
            organization_id=organization_id,
            vehicle_id=root.vehicle_id,
            performed_at__lte=root.recorded_at,
            recorded_at__lte=instant,
            pk__in=same_work.values("paper_entry_batch_id"),
        )
        .exclude(
            pk__in=PaperEntryInvalidationEvent.objects.filter(
                organization_id=organization_id, recorded_at__lte=instant
            ).values("batch_id")
        )
        .select_related("checklist_version__template", "actor_user")
        .order_by("-performed_at", "-recorded_at", "-pk")[:MAX_REPLACEMENT_CANDIDATES]
    )
    performers = _performer_names(organization_id, [str(b.pk) for b in batches])
    return [_batch_entry(batch, performers) for batch in batches]


def _mistaken_normal(user, roles, row, root, instant):
    """The mistaken-Normal invalidation offer for an effective check, or None (INV-8).

    Only a routine check saved in Normal mode qualifies, and only a Supervisor or
    Administrator other than its author (invalidate_mistaken_normal_paper_check).
    """
    if root is None or root.paper_entry_batch_id is not None or root.purpose != "ROUTINE":
        return None
    if not permitted(
        MISTAKEN_NORMAL_ACTION,
        str(user.pk),
        roles,
        context=ResourceContext(author_id=str(row.actor_user_id)),
    ):
        return None
    return {"candidates": mistaken_normal_candidates(user.organization_id, root, instant)}


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
        .select_related(
            "item",
            "kit_instance",
            "item_requirement_version__compartment",
            "item_requirement_version__system",
        )
        .order_by("item_id", "instance_key"),
        2000,
    )
    if not rows:
        raise ValueError("verification scope unavailable")
    satisfied = satisfied_scope_rows(str(deficiency.organization_id), str(deficiency.pk), rows)
    for row in rows:
        row.satisfied = row.pk in satisfied
    return rows
